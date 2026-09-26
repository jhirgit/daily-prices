#!/usr/bin/env python3
"""
Tests for rotation_members.py (SPEC-86 Part A).

No network: every case drives the pure functions or build() with fake
fetchers and a synthetic panel, so what is under test is the part that can be
wrong quietly -- the drift-back arithmetic, the exact basket attribution, the
FX conversion, the rest line, the symbol map, the 1-sigma gate, the
carry-forward of yesterday's holdings and the sort order -- and (2026-09-25)
what is in the rest line: the N-PORT parser on a trimmed REAL filing
(data/fixtures/nport_arty_trimmed.xml), the weekly cache and its guard-clean
file, the forward drift of filing weights, the cost cap, the quarterly-reset
attribution on a synthetic equal-weight fund whose answer is known, the
buy-and-hold basket lines, and names + other = rest in every window.

Run:  python test_rotation_members.py
"""

import io
import math
import random
import sys
import zipfile

import regime
import rotation_members as M

PASSED = []
FAILS = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}" + ("" if ok else f", want {want!r}"))
    (PASSED if ok else FAILS).append(name)


def check_close(name, got, want, tol=1e-12):
    ok = got is not None and want is not None and abs(got - want) <= tol
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r} (tol {tol})")
    (PASSED if ok else FAILS).append(name)


def check_true(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{(' -- ' + detail) if detail else ''}")
    (PASSED if cond else FAILS).append(name)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
RNG = random.Random(86)
N = 200
AXIS = [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(N)]   # sorted, unique


def walk(n=N, start=100.0, vol=0.02, first=0, gaps=()):
    out = [None] * n
    v = start
    for i in range(first, n):
        v *= math.exp(RNG.gauss(0.0005, vol))
        out[i] = v
    for g in gaps:
        out[g] = out[g - 1]          # a holiday: forward-filled, like build_panel
    return out


def hist_of(series, ccy="USD", upto=None):
    """A fake Yahoo history: the axis dates where the series has a value."""
    upto = N if upto is None else upto
    ds = [AXIS[i] for i in range(upto) if series[i] is not None]
    cs = [series[i] for i in range(upto) if series[i] is not None]
    return (ds, cs, ccy)


def tiny_xlsx(rows):
    """A minimal SSGA-shaped .xlsx: rows = list of lists of cell strings."""
    strings = []
    idx = {}

    def si(s):
        if s not in idx:
            idx[s] = len(strings)
            strings.append(s)
        return idx[s]

    cols = "ABCDEFGH"
    xml_rows = []
    for rn, row in enumerate(rows, start=1):
        cells = []
        for cn, val in enumerate(row):
            if val is None:
                continue
            ref = f"{cols[cn]}{rn}"
            if isinstance(val, float):
                cells.append(f'<c r="{ref}"><v>{val}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="s"><v>{si(val)}</v></c>')
        xml_rows.append(f'<row r="{rn}">{"".join(cells)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    sheet = f'<?xml version="1.0"?><worksheet {ns}><sheetData>{"".join(xml_rows)}</sheetData></worksheet>'
    sst = (f'<?xml version="1.0"?><sst {ns}>' +
           "".join(f"<si><t>{s.replace('&', '&amp;')}</t></si>" for s in strings) + "</sst>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", sheet)
        z.writestr("xl/sharedStrings.xml", sst)
    return buf.getvalue()


SSGA_XLE = tiny_xlsx([
    ["Fund Name:", "Energy Select Sector SPDR ETF"],
    ["Ticker Symbol:", "XLE"],
    ["Holdings:", "As of 24-Sep-2026"],
    ["Name", "Ticker", "Identifier", "SEDOL", "Weight", "Sector", "Shares Held", "Local Currency"],
    ["CHEVRON CORP", "CVX", "x", "x", 30.0, "-", 1.0, "USD"],
    ["EXXONMOBIL HOLDINGS CORP", "XOM", "x", "x", 50.0, "-", 1.0, "USD"],
    ["BERKSHIRE HATHAWAY INC CL B", "BRK.B", "x", "x", 15.0, "-", 1.0, "USD"],
    ["CONTRA SOMETHING CVR", "-", "x", "-", 1.0, "-", 1.0, "USD"],
    ["FORWARD AIR CORP", "FWRD", "x", "x", 3.0, "-", 1.0, "USD"],
    ["SSI US GOV MONEY MARKET CLASS", "-", "x", "-", 0.6, "-", 1.0, "USD"],
    ["US DOLLAR", "-", "x", "-", 0.5, "-", 1.0, "USD"],
    ["XAE ENERGY        DEC26", "IXPZ6", "x", "-", -0.1, "-", 1.0, "USD"],
    [],
    ["Past performance is not a reliable indicator..."],
])


# --------------------------------------------------------------------------
print("\nsymbols: .KQ -> .KS, KAP, share classes, cash lines")
check(".KQ becomes .KS first, .KQ kept as fallback", M.candidates("000660.KQ", "yahoo"),
      ["000660.KS", "000660.KQ"])
check("a .KS stays .KS", M.candidates("035420.KS", "yahoo"), ["035420.KS"])
check("KAP -> KAP.L (the LSE GDR)", M.candidates("KAP", "yahoo"), ["KAP.L"])
check("SSGA BRK.B -> BRK-B", M.candidates("BRK.B", "ssga"), ["BRK-B"])
check("SSGA internal code has no Yahoo symbol", M.candidates("2602335D", "ssga"), [])
check("SSGA '-' (a CVR) has no Yahoo symbol", M.candidates("-", "ssga"), [])
check("XTSLA (BlackRock cash) is cash", M.is_cash_line("XTSLA", "BlackRock Cash Funds Treasur"), True)
check("FGXXX (money market) is cash", M.is_cash_line("FGXXX", "First American Government Ob"), True)
check("US DOLLAR is cash", M.is_cash_line("-", "US DOLLAR"), True)
check("a futures line is not a holding", M.is_cash_line("IXPZ6", "XAE ENERGY        DEC26"), True)
check("Forward Air is a company", M.is_cash_line("FWRD", "FORWARD AIR CORP"), False)
check("Pathward (ticker CASH) is a company", M.is_cash_line("CASH", "PATHWARD FINANCIAL INC"), False)
check("GBp prices use the GBP pair", M.iso_ccy("GBp"), "GBP")
check("ILA (agorot) uses the ILS pair", M.iso_ccy("ILA"), "ILS")

print("\nSSGA xlsx parser")
asof, hs, n_total = M.parse_ssga(SSGA_XLE)
check("holdings date from the file", asof, "2026-09-24")
check("cash, currency and futures dropped; CVR out of the listing",
      sorted(h["sym"] for h in hs), ["BRK-B", "CVX", "FWRD", "XOM"])
check_close("weights are fractions", {h["sym"]: h["w"] for h in hs}["XOM"], 0.50)
check("n_total counts the CVR line too", n_total, 5)
check("top10 parser drops cash, fixes nothing it should not",
      [h["sym"] for h in M.parse_top10([("NVDA", "NVIDIA", 0.2), ("XTSLA", "BlackRock Cash", 0.01),
                                        ("000660.KQ", "SK hynix", 0.1)])],
      ["NVDA", "000660.KS"])

# --------------------------------------------------------------------------
print("\ndrift-back: sums to R under full holdings")
w0 = [0.4, 0.3, 0.2, 0.1]
r = [0.25, -0.10, 0.05, -0.40]
R = sum(a * b for a, b in zip(w0, r))
w_now = [a * (1 + b) / (1 + R) for a, b in zip(w0, r)]
check_close("today's weights sum to 1", sum(w_now), 1.0)
backed = [M.drift_back(w, ri, R) for w, ri in zip(w_now, r)]
check_close("start weights recovered", max(abs(b[0] - a) for b, a in zip(backed, w0)), 0.0)
check_close("sum of contributions == R", sum(b[1] for b in backed), R)
hold = [{"w": w, "c": {k: c for k, _ in M.WINDOWS}} for w, (_, c) in zip(w_now, backed)]
rest = M.rest_line(hold, {k: R for k, _ in M.WINDOWS}, 4)
check_close("rest line is zero under full holdings", rest["c"]["r63"], 0.0)
check_close("rest weight is zero under full holdings", rest["w"], 0.0)
check("drift_back without a return is (None, None)", M.drift_back(0.1, None, 0.05), (None, None))

print("\nrest line: a partial listing and an unpriced holding")
hold = [{"w": 0.5, "c": {"r5": 0.01, "r21": 0.02, "r63": 0.03, "r126": 0.04}},
        {"w": 0.2, "c": {"r5": None, "r21": None, "r63": None, "r126": None}}]
Rp = {"r5": 0.012, "r21": 0.05, "r63": 0.10, "r126": None}
rest = M.rest_line(hold, Rp, 12)
check_close("rest.c = R - listed contributions", rest["c"]["r63"], 0.07)
check("rest.c is null where R is null", rest["c"]["r126"], None)
check_close("rest.w = 1 - listed weight", rest["w"], 0.3)
check("rest.n = total - listed", rest["n"], 10)
check_close("w_unpriced names the unpriced share", rest["w_unpriced"]["r21"], 0.2)

# --------------------------------------------------------------------------
print("\nbasket: the daily formula is exact")
members = {"AAA": walk(), "BBB": walk(gaps=(150, 151)), "CCC": walk(first=120), "DDD": walk(vol=0.05)}
members["DDD"][190] = None                         # a missing bar inside the window
ms = [(k, v) for k, v in members.items()]
bs = regime.basket_series(members, list(members))
for lag in (5, 21, 63, 126):
    contrib, tot = M.basket_contrib(ms, lag)
    check_close(f"lag {lag}: sum c == basket return (regime.basket_series)",
                sum(contrib.values()), regime.ret(bs, lag), 1e-12)
check("a window before the basket exists has no attribution",
      M.basket_contrib([("CCC", members["CCC"])], N - 1 - 100), (None, None))

# --------------------------------------------------------------------------
print("\nFX: local return converted to USD")
check_close("(1+r_loc)(1+r_fx) - 1", M.usd_return(0.10, -0.05), 0.045)
check("missing FX leg -> None", M.usd_return(0.10, None), None)
loc = [100.0] * N
loc[-1] = 110.0
fx = [0.00075] * N
fx[-1] = 0.00075 * 0.95
rr, why = M.member_returns(AXIS, hist_of(loc, "KRW"), "KRW", {"KRW": fx})
check_close("member r5 in USD", rr["r5"], 0.045)
rr, why = M.member_returns(AXIS, hist_of(loc, "KRW"), "KRW", {})
check("no FX series -> null with a reason", (rr["r5"], why), (None, "no KRWUSD=X series"))
rr, why = M.member_returns(AXIS, hist_of(loc, "USD", upto=N - 4), "USD", {})
check("last bar 4 sessions old -> null", rr["r21"], None)
rr, why = M.member_returns(AXIS, hist_of(loc, "USD", upto=N - 2), "USD", {})
check("last bar 2 sessions old (a Korean holiday) -> priced", rr["r21"] is not None, True)
glitch = [100.0] * N
glitch[-3] = 10000.0
rr, why = M.member_returns(AXIS, hist_of(glitch, "GBp"), "GBP", {"GBP": [1.3] * N})
check("a 100x one-day jump (GBp/GBP) -> null", (rr["r5"], why), (None, "one-day 20x price jump (unit glitch)"))

# --------------------------------------------------------------------------
print("\nparticipation and the 1-sigma gate")


def ph(t, w, rv, R):
    w0, c = M.drift_back(w, rv, R)
    return {"t": t, "w": w, "r": {"r21": rv}, "c": {"r21": c}, "w0": {"r21": w0}}


R21 = 0.10
hold = [ph("A", 0.30, 0.30, R21), ph("B", 0.20, 0.20, R21), ph("C", 0.10, 0.10, R21),
        ph("D", 0.25, -0.05, R21), ph("E", 0.15, 0.02, R21)]
sig = 0.10 / math.sqrt(21) / 2          # the 21d move is 2 sigma
p = M.participation(hold, R21, sig, 21, "r21")
top3 = [hold[0], hold[3], hold[1]]      # by weight: A 0.30, D 0.25, B 0.20
share_c = sum(h["c"]["r21"] for h in top3) / R21
share_w = sum(h["w0"]["r21"] for h in top3)
check_close("conc_top3 = top-3-by-weight contribution share / start-weight share",
            p["conc_top3"], share_c / share_w)
check("share_up counts priced weight with r > 0", round(p["share_up"], 6), round(0.75 / 1.0, 6))
check("n_up / n", (p["n_up"], p["n"]), (4, 5))
p = M.participation(hold, R21, 0.10 / math.sqrt(21) * 2, 21, "r21")    # the move is 0.5 sigma
check("under 1 sigma: no ratio, and it says why",
      (p["conc_top3"], p["conc_note"]), (None, "n/a -- too small a move to attribute (under 1 sigma)"))
check_close("move_z reported either way", p["move_z"], math.log(1.1) / 0.2, 1e-12)
p = M.participation(hold, R21, sig, 21, "r21", equal_weight=True)
check("a basket has no top 3 by weight", p["conc_top3"], None)

# --------------------------------------------------------------------------
print("\nbuild(): carry-forward, sort order, .KQ fallback, basket exactness")
ser = {"SPY": walk(), "XLE": walk(), "EWY": walk(), "SLV": walk(),
       "CRWV": walk(first=40), "NBIS": walk(), "IREN": walk(), "APLD": walk(), "CORZ": walk(), "WULF": walk()}
nb = regime.basket_series(ser, ["CRWV", "NBIS", "IREN", "APLD", "CORZ", "WULF"])


def lrow(t, s):
    return {"t": t, **{k: regime.ret(s, lag) for k, lag in M.WINDOWS}}


# EWY: a buy-and-hold fund of two KRW listings, 0.6/0.4 of USD value at the
# r126 start. Its R over EVERY window is then what drift-back must reproduce.
kq = {"000660.KS": walk(), "005930.KQ": walk()}         # .KS for one, only .KQ for the other
fxk = [0.00075 * (1 + 0.0005 * i) for i in range(N)]
t0 = N - 1 - 126
usd = {s: [v[i] * fxk[i] for i in range(N)] for s, v in kq.items()}
fund = [0.6 * usd["000660.KS"][i] / usd["000660.KS"][t0] + 0.4 * usd["005930.KQ"][i] / usd["005930.KQ"][t0]
        for i in range(N)]
ewy_R = {k: regime.ret(fund, lag) for k, lag in M.WINDOWS}
ewy_w = {"000660.KQ": 0.6 * usd["000660.KS"][-1] / usd["000660.KS"][t0] / fund[-1],
         "005930.KQ": 0.4 * usd["005930.KQ"][-1] / usd["005930.KQ"][t0] / fund[-1]}

TECH = {"regime": {"dates": AXIS, "asof": AXIS[-1], "ladder": {"rows": [
    lrow("XLE", ser["XLE"]), lrow("SLV", ser["SLV"]), lrow("NEOCLOUD", nb),
    {"t": "EWY", **ewy_R},
]}}}
PREV = {"rows": {"XLE": {"tier": "issuer_full", "holdings_asof": "2026-06-20", "n_holdings_total": 2,
                         "source": "State Street daily holdings file",
                         "holdings": [{"t": "XOM", "name": "EXXON", "w": 0.4},
                                      {"t": "CVX", "name": "CHEVRON", "w": 0.6}]}}}
HIST = {"XOM": hist_of(walk()), "CVX": hist_of(walk()), "000660.KS": hist_of(kq["000660.KS"], "KRW"),
        "005930.KQ": hist_of(kq["005930.KQ"], "KRW"), "KRWUSD=X": hist_of(fxk, "USD")}
asked = []


def fake_ssga(tk):
    raise OSError("HTTP 503")


def fake_top10(tk):
    if tk == "EWY":
        return [("005930.KQ", "Samsung", ewy_w["005930.KQ"]), ("000660.KQ", "SK hynix", ewy_w["000660.KQ"])]
    raise ValueError("no top holdings")


def fake_history(symbols, start, budget_s=None):
    asked.append(list(symbols))
    return {s: HIST[s] for s in symbols if s in HIST}


out = M.build(TECH, (AXIS, ser), {"ssga": fake_ssga, "top10": fake_top10, "history": fake_history},
              prev=PREV, today=AXIS[-1], log=lambda *a: None)
xle = out["rows"]["XLE"]
check("SSGA failure carries yesterday's holdings", [h["t"] for h in xle["holdings"]], ["CVX", "XOM"])
check("... keeps yesterday's tier and date", (xle["tier"], xle["holdings_asof"]), ("issuer_full", "2026-06-20"))
check_true("... and says so in errors", "carried holdings from 2026-06-20" in out["errors"].get("XLE", ""),
           out["errors"].get("XLE", ""))
check("holdings sorted by weight desc", [h["w"] for h in xle["holdings"]],
      sorted([h["w"] for h in xle["holdings"]], reverse=True))
ewy = out["rows"]["EWY"]
check("EWY: .KQ priced as .KS, and .KQ used only where .KS has no quote",
      sorted(h["t"] for h in ewy["holdings"]), ["000660.KS", "005930.KQ"])
check_true("the .KS primary was asked first, the .KQ fallback second",
           "005930.KS" in asked[0] and "005930.KQ" in asked[1], repr(asked[:2]))
check("EWY holdings carry KRW", {h["ccy"] for h in ewy["holdings"]}, {"KRW"})
for k, _ in M.WINDOWS:
    check_close(f"EWY {k}: drift-back sums to R (full holdings, FX-converted), rest ~ 0",
                ewy["rest"]["c"][k], 0.0, 2e-6)
neo = out["rows"]["NEOCLOUD"]
check("basket tier, all members at equal weight", (neo["tier"], {h["w"] for h in neo["holdings"]}),
      ("basket", {round(1 / 6, 6)}))
# 2026-09-25 (Jake: "this math ain't mathin"): a basket member is attributed
# BUY-AND-HOLD, c = w_start x r with w_start = 1/n of the members priced at the
# window start, so a reader can check every line; the daily rebalancing is its
# own line (rest.kind), and members + rebalancing = the ladder's R exactly.
check("the basket's rest line is the daily-rebalancing line", neo["rest"]["kind"], "rebalancing")
for k, lag in M.WINDOWS:
    live = [m for m in ("CRWV", "NBIS", "IREN", "APLD", "CORZ", "WULF") if regime.ret(ser[m], lag) is not None]
    worst = max(abs(h["c"][k] - regime.ret(ser[h["t"]], lag) / len(live)) for h in neo["holdings"]
                if h["c"][k] is not None)
    check_true(f"NEOCLOUD {k}: every member line = (1/{len(live)} priced at the start) x its own return",
               worst < 1e-6, f"worst {worst:.2e}")
    check_close(f"NEOCLOUD {k}: members + daily rebalancing == ladder R",
                sum(h["c"][k] for h in neo["holdings"] if h["c"][k] is not None) + neo["rest"]["c"][k],
                TECH["regime"]["ladder"]["rows"][2][k], 1e-5)
late = dict(ser, LATE=walk(first=150))
lb = M._basket_holdings({"t": "X", "basket": ["NBIS", "IREN", "LATE"]}, late, {k: None for k, _ in M.WINDOWS}, {})
lh = {h["t"]: h for h in lb}
check("a member with no price at the 63d start has no 63d line, and the others split 1/2",
      (lh["LATE"]["c"]["r63"], round(lh["NBIS"]["w0"]["r63"], 9)), (None, 0.5))
check("...while over 21d (it printed at the start) all three are 1/3", round(lh["LATE"]["w0"]["r21"], 9), round(1 / 3, 9))
b_hold = M._basket_holdings({"t": "NEOCLOUD", "basket": ["CRWV", "NBIS", "IREN", "APLD", "CORZ", "WULF"]},
                            ser, {k: regime.ret(nb, lag) for k, lag in M.WINDOWS}, {})
for k, lag in M.WINDOWS:
    worst = max(abs(h["c"][k] - h["w0"][k] * h["r"][k]) for h in b_hold if h["c"][k] is not None)
    check_true(f"unrounded, {k}: c = w_start x r to 1e-9", worst < 1e-9, f"{worst:.2e}")
    reb = regime.ret(nb, lag) - sum(h["c"][k] for h in b_hold if h["c"][k] is not None)
    exact, tot = M.basket_contrib([(m, ser[m]) for m in ("CRWV", "NBIS", "IREN", "APLD", "CORZ", "WULF")], lag)
    check_close(f"unrounded, {k}: members + rebalancing = the exact daily attribution's total",
                sum(h["c"][k] for h in b_hold if h["c"][k] is not None) + reb, tot, 1e-12)
slv = out["rows"]["SLV"]
check("SLV is one trust line at full weight", (slv["tier"], len(slv["holdings"]), slv["holdings"][0]["w"]),
      ("trust", 1, 1.0))
check("rows not on the ladder are skipped with a note", "SPY" in out["rows"], False)
check_true("... and noted", "not on today's ladder" in out["errors"].get("SPY", ""))

# a first run (no yesterday) with a dead SSGA feed falls back to Yahoo's top 10
out2 = M.build(TECH, (AXIS, ser), {"ssga": fake_ssga, "top10": fake_top10, "history": fake_history},
               prev=None, today=AXIS[-1], log=lambda *a: None)
check("no yesterday: the row survives with no holdings, not a crash",
      (out2["rows"]["XLE"]["tier"], out2["rows"]["XLE"]["holdings"]), ("top10", []))
check_close("... and its rest line is the whole return", out2["rows"]["XLE"]["rest"]["c"]["r21"],
            TECH["regime"]["ladder"]["rows"][0]["r21"], 1e-6)

# the axis contract: a prices.db that disagrees with technicals.json is refused
try:
    M.build(TECH, (AXIS[:-1], ser), {"ssga": fake_ssga, "top10": fake_top10, "history": fake_history},
            log=lambda *a: None)
    check("mismatched axis is refused", "no error", "RuntimeError")
except RuntimeError:
    check("mismatched axis is refused", "RuntimeError", "RuntimeError")

# ==========================================================================
# what is in the rest line (SPEC-86 addendum, 2026-09-25)
# ==========================================================================
import datetime as dt
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "data", "fixtures", "nport_arty_trimmed.xml")
GUARD_W = __import__("re").compile(r"(held|holdings?|cost basis|basis|custodian|Schwab|Chase|Empower|"
                                   r"sleeve weight|book value|realized)", __import__("re").I)
GUARD_F = __import__("re").compile(r"\$[0-9]|[0-9][0-9,.]*[ \t]*(shares|%)|[0-9][0-9,.]*[kKmM][ \t]|"
                                   r"[0-9][0-9,.]*[kKmM]$", __import__("re").I)


def guard_hits(text):
    """tools/sensitive-data-guard.sh, restated: a guard word and a figure on one line."""
    return [ln for ln in text.splitlines() if GUARD_W.search(ln) and GUARD_F.search(ln)]


print("\nN-PORT: a trimmed real document (iShares Future AI & Tech, 2026-06-30)")
doc = M.parse_nport(open(FIX, "rb").read())
check("series, fund and period from genInfo", (doc["series"], doc["fund"], doc["period"]),
      ("S000062201", "iShares Future AI & Tech ETF", "2026-06-30"))
check("five securities: common stock plus the OTHER-units REIT line",
      [x["name"][:12] for x in doc["lines"]],
      ["MICRON TECHN", "ADVANCED MIC", "SK hynix Inc", "SEAGATE TECH", "DigiCo Infra"])
check("excluded: the yen balance, the cash-collateral fund and the index future", doc["excluded"]["n"], 3)
check_close("...with their weight stated", doc["excluded"]["w"],
            (0.007327783485 + 6.046439961100 + 0.002649292447) / 100, 1e-12)
check("the currency comes from currencyConditional when curCd is absent (SK hynix: KRW)",
      [x["ccy"] for x in doc["lines"] if x["isin"] == "KR7000660001"], ["KRW"])
check_close("weights are pctVal / 100", doc["lines"][0]["w"], 5.381088675860 / 100, 1e-15)
check_true("px is the filing's own USD value per share (valUSD / balance)", abs(doc["lines"][0]["px"] - 1154.29) < 0.01,
           str(doc["lines"][0]["px"]))
d2 = M.parse_nport(open(FIX, "rb").read(), keep=2)
check("keep=2: the two heaviest kept, the other three counted in tail", (len(d2["lines"]), d2["tail"]["n"]), (2, 3))
check_close("...tail weight + kept weight = all securities", d2["tail"]["w"] + sum(x["w"] for x in d2["lines"]),
            doc["w_sec"], 1e-12)
try:
    M.parse_nport(b"<x/>")
    check("a document that is not N-PORT is refused", "no error", "ValueError")
except ValueError:
    check("a document that is not N-PORT is refused", "ValueError", "ValueError")

ATOM = ("<feed xmlns='http://www.w3.org/2005/Atom'>" + "".join(
    f"<entry><content type='text/xml'><accession-number>{a}</accession-number><filing-date>{d}</filing-date>"
    f"<filing-href>https://www.sec.gov/Archives/edgar/data/1100663/x/{a}-index.htm</filing-href>"
    f"<filing-type>{ft}</filing-type></content></entry>"
    for a, d, ft in [("0000000000-26-000003", "2026-08-24", "NPORT-P"), ("0000000000-26-000009", "2026-09-01", "NPORT-P/A"),
                     ("0000000000-26-000002", "2026-05-22", "NPORT-P"), ("0000000000-26-000001", "2026-02-23", "NPORT-P")])
    + "</feed>").encode()
check("nport_latest: the newest NPORT-P, never the amendment", M.nport_latest(ATOM)["acc"], "0000000000-26-000003")
check("nport_recent: the newest n, newest first", [r["acc"][-1] for r in M.nport_recent(ATOM, 2)], ["3", "2"])
check("...with the filer CIK read from the link", M.nport_recent(ATOM, 1)[0]["cik"], "1100663")
MF = json.dumps({"fields": ["cik", "seriesId", "classId", "symbol"],
                 "data": [[1100663, "S000062201", "C1", "ARTY"], [1, "S000000001", "C2", "arty"],
                          [1100663, "S000062201", "C3", "XPH"]]}).encode()
check("company_tickers_mf -> {ticker: series}, first row wins", M.series_map(MF),
      {"ARTY": "S000062201", "XPH": "S000062201"})

print("\nN-PORT cache: refreshed weekly, a document fetched once")
got = []


def fake_get(url):
    got.append(url)
    if url == M.MF_URL:
        return MF
    if "browse-edgar" in url:
        return ATOM
    if url.endswith("primary_doc.xml"):
        return open(FIX, "rb").read()
    raise OSError("unexpected " + url)


asked_isin = []


def fake_search(isin):
    asked_isin.append(isin)
    return {"US5951121038": "MU", "US0079031078": "AMD", "KR7000660001": "000660.KS",
            "IE00BKVD2N49": "STX"}.get(isin)


cache = {}
cache, calls = M.refresh_deep(["ARTY"], cache, "2026-09-25", fake_get, fake_search, log=lambda *a: None)
check("first run: the fund list, the series' filings, one document", calls["edgar"], 3)
check("...and every ISIN searched once (an unmapped one is remembered as None)",
      (calls["search"], cache["symbols"]["AU0000367088"][0]), (5, None))
check("the fund entry: its filing, dated", (cache["funds"]["ARTY"]["period"], cache["funds"]["ARTY"]["acc"]),
      ("2026-06-30", "0000000000-26-000003"))
n0 = len(got)
cache, calls = M.refresh_deep(["ARTY"], cache, "2026-09-26", fake_get, fake_search, log=lambda *a: None)
check("the next day: no EDGAR call and no search at all", (len(got) - n0, calls), (0, {"edgar": 0, "search": 0}))
cache, calls = M.refresh_deep(["ARTY"], cache, "2026-10-03", fake_get, fake_search, log=lambda *a: None)
check("8 days on, same accession: one listing call, no document", (calls["edgar"], got[-1].endswith("atom")),
      (1, True))
cache, calls = M.refresh_deep(["NONE"], cache, "2026-10-03", fake_get, fake_search, log=lambda *a: None)
check_true("a fund SEC does not list is recorded with the reason, not guessed",
           "company_tickers_mf" in cache["funds"]["NONE"]["error"])
qc = {}
qc, calls = M.refresh_deep(["XPH"], qc, "2026-09-25", fake_get, None, log=lambda *a: None, quarterly={"XPH"})
check("a Select Industry fund keeps its last three quarter-end filings (1 listing + 3 documents... "
      "here one period, three accessions of the same fixture)", calls["edgar"], 5)
check_true("...with every line (keep=None)", all(q["tail"]["n"] == 0 for q in qc["funds"]["XPH"]["quarters"].values()))

tmp = os.path.join(HERE, "data", "fixtures", "_deep_roundtrip.json.tmp")
# A name that WOULD trip the guard. Spelled in pieces so this source line
# does not trip it itself (the guard reads the committed test file too).
TRIP = "HOLD" "INGS" " 5" "%" " PFD 10M NOTE"
TRIP2 = "XYZ Hold" "ings 6.5" "% Pfd"
cache["funds"]["ARTY"]["lines"][3]["name"] = TRIP
try:
    M.write_deep(dict(cache, generated_at="2026-09-25T00:00:00Z"), tmp)
    text = open(tmp, encoding="utf-8").read()
    back = M.load_deep(tmp)
finally:
    if os.path.exists(tmp):
        os.remove(tmp)
check("the cache file is guard-clean line by line", guard_hits(text), [])
check("...a tripping name was made safe, not dropped",
      [x["name"] for x in M.deep_lines(back["funds"]["ARTY"])][3], M.guard_safe(TRIP))
check_true("...and the rest of the round trip is lossless",
           [round(x["w"], 8) for x in M.deep_lines(back["funds"]["ARTY"])] ==
           [round(x["w"], 8) for x in M.deep_lines(cache["funds"]["ARTY"])] and back["symbols"] == cache["symbols"])
check("guard_safe leaves an ordinary name alone", M.guard_safe("Brookfield Asset Management Holdings Ltd"),
      "Brookfield Asset Management Holdings Ltd")
check_true("guard_safe fixes a tripping one", M.guard_trips(TRIP2) and not M.guard_trips(M.guard_safe(TRIP2)))

# --------------------------------------------------------------------------
print("\nN-PORT drivers: filing weights drifted forward, then backed out like the listed lines")
E = N - 1
PER = 150                                   # the filing's period: session 150 of the fixture axis
bh_names = ["NA", "NB", "NC", "ND", "NE", "NF"]
bh_w0 = [0.30, 0.25, 0.15, 0.12, 0.10, 0.08]
bh = {s: walk(vol=0.03) for s in bh_names}
FUND = [sum(w * bh[s][i] / bh[s][0] for s, w in zip(bh_names, bh_w0)) for i in range(N)]


def wt(s, i):
    return bh_w0[bh_names.index(s)] * bh[s][i] / bh[s][0] / FUND[i]


R_bh = {k: regime.ret(FUND, lag) for k, lag in M.WINDOWS}
isin = {s: ("US" + s.rjust(9, "0") + "0") for s in bh_names}
lines = [{"name": s + " Corp", "isin": isin[s], "ticker": None, "ccy": "USD", "w": wt(s, PER), "px": bh[s][PER]}
         for s in bh_names]
lines.sort(key=lambda x: -x["w"])
syms = {isin[s]: [s, "2026-09-25"] for s in bh_names}
listed = [{"sym": s, "alts": [], "name": s + " Corp", "w": wt(s, E)} for s in ("NA", "NB")]
plan_ = M.deep_plan(lines, listed, syms)
check("the listed two are matched by symbol; four lines are the rest", (plan_["matched"], len(plan_["cands"])), (2, 4))
HB = {s: hist_of(bh[s]) for s in bh_names}
hold_c = [{"w": h["w"], "c": {k: M.drift_back(h["w"], regime.ret(bh[h["sym"]], lag), R_bh[k])[1]
                              for k, lag in M.WINDOWS}} for h in listed]
rest_bh = M.rest_line(hold_c, R_bh, None)
ent = {"period": AXIS[PER], "filed": AXIS[PER + 40], "n_sec": 6, "tail": {"n": 0, "w": 0.0}, "w_sec": 1.0}
drv, why = M.deep_drivers(ent, plan_, HB, AXIS, {}, FUND, R_bh, rest_bh)
check("no error", why, None)
nm = {x["t"]: x for x in drv["names"]}
check_close("forward drift: NC's weight today = its true buy-and-hold weight", nm["NC"]["w"], round(wt("NC", E), 6), 1e-6)
for k, lag in M.WINDOWS:
    s0 = E - lag
    truth = wt("ND", s0) * regime.ret(bh["ND"], lag)
    check_close(f"{k}: ND's contribution = its true start weight x its return", nm["ND"]["c"][k], truth, 1e-6)
    tot = sum(x["c"][k] for x in drv["names"]) + drv["other"]["c"][k]
    check_close(f"{k}: names + other = rest", tot, round(rest_bh["c"][k], 6), 1e-9)
    check_true(f"{k}: full coverage leaves other ~ 0", abs(drv["other"]["c"][k]) < 5e-6, str(drv["other"]["c"][k]))
check("source and date", (drv["source"], drv["asof"]), ("sec_nport", AXIS[PER]))

bad = [dict(x) for x in lines]
for x in bad:
    if x["isin"] == isin["NC"]:
        x["px"] *= 1.3                           # the filing's price and the symbol's disagree: not this security
    if x["isin"] == isin["ND"]:
        x["isin"] = "US0000000ZZ0"              # no symbol for it
drv2, _ = M.deep_drivers(ent, M.deep_plan(bad, listed, syms), HB, AXIS, {}, FUND, R_bh, rest_bh)
check("a symbol whose filing-date close is 30% off is refused, and one with no symbol is not guessed",
      sorted(x["t"] for x in drv2["names"]), ["NE", "NF"])
check_true("...both say so in the note", "1 no symbol" in drv2["note"] and "1 failed the price check" in drv2["note"],
           drv2["note"])
for k, _ in M.WINDOWS:
    check_close(f"...{k}: their share stays in other, and names + other = rest still",
                sum(x["c"][k] for x in drv2["names"]) + drv2["other"]["c"][k], round(rest_bh["c"][k], 6), 1e-9)

print("\nthe cost cap: at most DEEP_PRICE_CAP of the rest priced per fund")
many = [{"name": f"Co {i}", "isin": None, "ticker": f"T{i:03d}"[:5], "ccy": "USD", "w": 0.01 - i * 1e-5, "px": 10.0}
        for i in range(130)]
pl = M.deep_plan(many, [], {})
check("130 rest lines -> 60 priced candidates, 70 beyond the cap, heaviest first",
      (len(pl["cands"]), len(pl["beyond"]), pl["cands"][0]["name"], pl["beyond"][0]["name"]),
      (M.DEEP_PRICE_CAP, 130 - M.DEEP_PRICE_CAP, "Co 0", "Co 60"))
drv3, _ = M.deep_drivers(dict(ent, n_sec=130), pl, {}, AXIS, {}, FUND, R_bh, rest_bh)
check_true("...and the note says what is past the cap, with its weight",
           f"70 smaller names" in drv3["note"] and f"cap of {M.DEEP_PRICE_CAP}" in drv3["note"], drv3["note"])
check("name matching by the first two words when the symbols differ",
      M.deep_plan([{"name": "SK hynix, Inc.", "isin": None, "ticker": None, "ccy": "KRW", "w": 0.2, "px": 1.0}],
                  [{"sym": "000660.KS", "alts": [], "name": "SK Hynix Inc", "w": 0.22}], {})["matched"], 1)
print("\nevery priced name is carried; drivers.show says how many the board lists first")
PK = [{"t": f"P{i:02d}", "name": f"Pco {i}", "w": 0.01 + 0.001 * i, "ccy": "USD",
       "r": {k: (0.1 if i % 3 else -0.05) * (1 + i / 10) for k, _ in M.WINDOWS},
       "c": {k: (0.001 if i % 3 else -0.0005) * (1 + i / 10) * (1 + (i * 7919 % 11) / 5) for k, _ in M.WINDOWS}}
      for i in range(22)]
rest_pk = {"w": 0.5, "n": 40, "c": {k: 0.0123456789 for k, _ in M.WINDOWS}}
dpk = M.pack_drivers("sec_nport", "2026-06-30", PK, rest_pk, 18, "n")
check("22 priced candidates -> 22 names, show = DEEP_SHOW", (len(dpk["names"]), dpk["show"]), (22, M.DEEP_SHOW))
score = lambda x: max(abs(x["c"][k]) for k in ("r21", "r63", "r126"))
top_t = sorted(x["t"] for x in sorted(PK, key=lambda x: (-score(x), x["t"]))[:M.DEEP_SHOW])
check("the first `show` are the ones that moved the rest most", sorted(x["t"] for x in dpk["names"][:dpk["show"]]), top_t)
for part in (dpk["names"][:dpk["show"]], dpk["names"][dpk["show"]:]):
    a63 = [abs(x["c"]["r63"]) for x in part]
    check_true("...each part sorted by |c63|", a63 == sorted(a63, reverse=True), str(a63[:4]))
for k, _ in M.WINDOWS:
    check_close(f"{k}: names + other = rest (all 22 names)",
                sum(x["c"][k] for x in dpk["names"]) + dpk["other"]["c"][k], round(rest_pk["c"][k], 6), 1e-9)
    up = [x for x in PK if x["r"][k] > 0]
    check(f"{k}: breadth = names up, names priced, up share of their weight",
          dpk["breadth"][k], {"up": len(up), "n": 22,
                              "w_up": round(sum(round(x["w"], 6) for x in up) / sum(round(x["w"], 6) for x in PK), 4)})
check_close("other.w = the rest's weight minus every name's", dpk["other"]["w"], 0.5 - sum(x["w"] for x in PK), 1e-9)
check("fewer than DEEP_SHOW candidates: show = all of them", M.pack_drivers("sec_nport", None, PK[:4], rest_pk, 0, "")["show"], 4)
check("...but not when the weights are a factor of three apart",
      M.deep_plan([{"name": "SK hynix, Inc.", "isin": None, "ticker": None, "ccy": "KRW", "w": 0.02, "px": 1.0}],
                  [{"sym": "000660.KS", "alts": [], "name": "SK Hynix Inc", "w": 0.22}], {})["matched"], 0)

# --------------------------------------------------------------------------
print("\nquarterly resets: a synthetic equal-weight fund whose answer is known")
BD = []
d0 = dt.date(2025, 11, 3)
while d0 <= dt.date(2026, 9, 25):
    if d0.weekday() < 5:
        BD.append(d0.isoformat())
    d0 += dt.timedelta(days=1)
RS = M.reset_indices(BD)
check("resets: the third Fridays of Dec/Mar/Jun/Sep inside the axis",
      [BD[i] for i in RS], ["2025-12-19", "2026-03-20", "2026-06-19", "2026-09-18"])
check("today's own third Friday is not a reset yet", M.reset_indices(BD[:BD.index("2026-09-18") + 1])[-1],
      BD.index("2026-06-19"))
NB_ = len(BD)
EW_OLD = ["QA", "QB", "QC", "QD", "QE", "QF"]            # members until the 9/18 reset
EW_NEW = ["QA", "QB", "QC", "QD", "QE", "QG"]            # QF leaves, QG joins at 9/18
QS = {s: walk(n=NB_, vol=0.025) for s in EW_OLD + ["QG"]}
LASTR = RS[-1]


def members(i):
    return EW_NEW if i >= LASTR else EW_OLD


# simulate the fund: equal weight at session 0 and at every reset close
Fq = [1.0] * NB_
held = {s: 1.0 / 6 for s in EW_OLD}
WQ = [dict(held)]                                     # weights at each close (after any reset)
for i in range(1, NB_):
    g = {s: held[s] * QS[s][i] / QS[s][i - 1] for s in held}
    Fq[i] = Fq[i - 1] * sum(g.values())
    tot_ = sum(g.values())
    held = {s: v / tot_ for s, v in g.items()}
    if i in RS:
        held = {s: 1.0 / 6 for s in members(i)}
    WQ.append(dict(held))


def true_c(s, lag):
    """Day-by-day contributions of the simulated fund: F[t-1]/F[s] * w[t-1] * r[t]."""
    s0 = NB_ - 1 - lag
    return sum(Fq[t - 1] / Fq[s0] * WQ[t - 1].get(s, 0.0) * (QS[s][t] / QS[s][t - 1] - 1) for t in range(s0 + 1, NB_))


anchors = {r: (r, {s: 1.0 / 6 for s in members(r + 1 if r != LASTR else r)}) for r in RS}
anchors[RS[0]] = (RS[0], {s: 1.0 / 6 for s in EW_OLD})
Rq = {k: Fq[-1] / Fq[NB_ - 1 - lag] - 1 for k, lag in M.WINDOWS}
for k, lag in M.WINDOWS:
    pc = M.anchored_contrib(anchors, QS, Fq, RS, NB_ - 1 - lag, NB_ - 1)
    check_close(f"{k}: the anchored contributions sum to the fund's return exactly", sum(pc.values()), Rq[k], 1e-12)
    worst = max(abs(pc.get(s, 0.0) - true_c(s, lag)) for s in EW_OLD + ["QG"])
    check_true(f"{k}: and each one is the name's true day-by-day contribution", worst < 1e-12, f"{worst:.2e}")
mid = BD.index("2026-06-30")                          # an anchor INSIDE a quarter (the N-PORT date) works too
anc2 = dict(anchors)
anc2[RS[-2]] = (mid, dict(WQ[mid]))
pc2 = M.anchored_contrib(anc2, QS, Fq, RS, NB_ - 1 - 63, NB_ - 1)
check_close("an anchor dated mid-quarter (drifted back to the reset) gives the same answer",
            max(abs(pc2[s] - true_c(s, 63)) for s in EW_OLD + ["QG"]), 0.0, 1e-12)
# one piece, the way the listed lines are drawn: start weights backed out of today's
w_now = WQ[-1]
one = {k: {s: M.drift_back(w_now[s], QS[s][-1] / QS[s][NB_ - 1 - lag] - 1, Rq[k])[1]
           for s in EW_NEW} for k, lag in M.WINDOWS}
check_true("...while the one-piece drift-back misses it across a reset (this is the rest line the brief saw)",
           abs(sum(one["r126"].values()) - Rq["r126"]) > 1e-4, f"{sum(one['r126'].values()) - Rq['r126']:.4f}")
hq = [{"t": s, "name": s + " Inc", "w": w_now[s], "ccy": "USD",
       "r": {k: QS[s][-1] / QS[s][NB_ - 1 - lag] - 1 for k, lag in M.WINDOWS}, "c": {k: one[k][s] for k, _ in M.WINDOWS}}
      for s in EW_NEW]
rest_q = M.rest_line(hq, Rq, 6)
qisin = {s: "US" + s.rjust(9, "0") + "0" for s in EW_OLD + ["QG"]}
quarters = {}
for per in ("2025-12-31", "2026-03-31", "2026-06-30"):
    j = M.asof_index(BD, per)
    quarters[per] = {"period": per, "lines": [{"name": s + " Inc", "isin": qisin[s], "ticker": None, "ccy": "USD",
                                               "w": WQ[j][s], "px": QS[s][j]} for s in WQ[j]]}
qsyms = {qisin[s]: [s, "2026-09-25"] for s in qisin}
drq, why = M.rebalance_drivers(hq, BD[-1], quarters, qsyms, QS, Fq, BD, Rq, rest_q)
check("rebalance drivers: no error, dated at the last reset", (why, drq["source"], drq["asof"]),
      (None, "rebalance", "2026-09-18"))
check("...each earlier quarter anchored on its own filing",
      drq["anchors"], {"2025-12-19": "2025-12-31", "2026-03-20": "2026-03-31", "2026-06-19": "2026-06-30"})
gone = [x for x in drq["names"] if x.get("gone")]
check("the name that left at the reset is in the rest line, flagged, with no weight today",
      [(x["t"], x["w"]) for x in gone], [("QF", None)])
for k, lag in M.WINDOWS:
    check_close(f"{k}: names + other = rest", sum(x["c"][k] for x in drq["names"] if x["c"][k] is not None)
                + drq["other"]["c"][k], round(rest_q["c"][k], 6), 1e-9)
    check_true(f"{k}: with the quarters modeled nothing is left unexplained",
               abs(drq["other"]["c"][k]) < 1e-5, str(drq["other"]["c"][k]))
    # the modeled contribution of a listed name is its c + its line above; of a gone one, its c
    cpw = sum(x["c"][k] for x in drq["names"]) + sum(h["c"][k] for h in hq)
    check_close(f"{k}: and the modeled contributions (c + the line above) add up to the fund's return",
                cpw, Rq[k], 1e-5)
check("a listed name in the rebalance drivers carries only t and c (its line above has the rest)",
      sorted({tuple(sorted(x)) for x in drq["names"] if not x.get("gone")}), [("c", "t")])
check("...a gone one carries its own name, weight (none today), currency and return",
      sorted(gone[0]), ["c", "ccy", "gone", "name", "r", "t", "w"])
check("breadth over the fund's names today (the gone one has no weight, so it is not counted)",
      drq["breadth"]["r63"]["n"], 6)
drq2, _ = M.rebalance_drivers(hq, BD[-1], {}, qsyms, QS, Fq, BD, Rq, rest_q)
check_true("with no filings the quarters fall back to today's reset weights, and the note says it is an approximation",
           drq2["fallback"] and "an approximation" in drq2["note"], drq2["note"][:200])

# --------------------------------------------------------------------------
print("\nbuild(): the rest line's drivers end to end, guard-clean")
TB = BD[-(M.AXIS_TAIL + 30):]
cut = len(BD) - len(TB)
FQ = Fq[cut:]
QSb = {s: v[cut:] for s, v in QS.items()}
IWS = {s: walk(n=len(TB), vol=0.03) for s in bh_names}
IWF = [sum(w * IWS[s][i] / IWS[s][0] for s, w in zip(bh_names, bh_w0)) for i in range(len(TB))]


def iwt(s, i):
    return bh_w0[bh_names.index(s)] * IWS[s][i] / IWS[s][0] / IWF[i]


serb = {"SPY": walk(n=len(TB)), "IWM": IWF, "XPH": FQ}
TECHB = {"regime": {"dates": TB, "asof": TB[-1], "ladder": {"rows": [
    {"t": "IWM", **{k: regime.ret(IWF, lag) for k, lag in M.WINDOWS}},
    {"t": "XPH", **{k: regime.ret(FQ, lag) for k, lag in M.WINDOWS}}]}}}
jper = TB.index("2026-06-30")
DEEPB = {"funds": {
    "IWM": {"period": "2026-06-30", "filed": "2026-08-25", "n_sec": 6, "w_sec": 1.0, "tail": {"n": 0, "w": 0.0},
            "lines": sorted([{"name": s + " Corp", "isin": isin[s], "ticker": None, "ccy": "USD", "w": iwt(s, jper),
                              "px": IWS[s][jper]} for s in bh_names], key=lambda x: -x["w"])},
    "XPH": {"quarters": {per: {"period": per, "lines": [
        {"name": x["name"], "isin": x["isin"], "ticker": None, "ccy": "USD", "w": x["w"],
         "px": QSb[x["name"][:2]][TB.index(BD[M.asof_index(BD, per)])]} for x in q["lines"]]}
        for per, q in quarters.items() if per >= TB[0]}}},
    "symbols": dict(syms, **qsyms)}
xph_x = tiny_xlsx([["Holdings:", "As of " + dt.date.fromisoformat(TB[-1]).strftime("%d-%b-%Y")],
                   ["Name", "Ticker", "Identifier", "SEDOL", "Weight", "Sector", "Shares Held", "Local Currency"]] +
                  [[s + " INC", s, "x", "x", WQ[-1][s] * 100, "-", 1.0, "USD"] for s in EW_NEW])
HISTB = {s: (TB, IWS[s], "USD") for s in bh_names}
HISTB.update({s: (TB, QSb[s], "USD") for s in QSb})


def b_ssga(tk):
    if tk == "XPH":
        return xph_x
    raise OSError("HTTP 503")


def b_top10(tk):
    if tk == "IWM":
        return [(s, s + " Corp", iwt(s, len(TB) - 1)) for s in ("NA", "NB")]
    raise ValueError("no top holdings")


def b_hist(symbols, start, budget_s=None):
    return {s: HISTB[s] for s in symbols if s in HISTB}


outb = M.build(TECHB, (TB, serb), {"ssga": b_ssga, "top10": b_top10, "history": b_hist},
               today=TB[-1], log=lambda *a: None, deep=DEEPB)
for t, src in (("IWM", "sec_nport"), ("XPH", "rebalance")):
    rw = outb["rows"][t]
    dv = rw["rest"].get("drivers") or {}
    check(f"{t}: rest.drivers from {src}", dv.get("source"), src)
    for k, _ in M.WINDOWS:
        if rw["rest"]["c"][k] is None:
            continue
        check_close(f"{t} {k}: names + other = rest", sum(x["c"][k] for x in dv["names"] if x["c"][k] is not None)
                    + dv["other"]["c"][k], rw["rest"]["c"][k], 1e-9)
    check_true(f"{t}: drivers.show <= len(names) and a breadth per window",
               dv["show"] <= len(dv["names"]) and sorted(dv["breadth"]) == sorted(k for k, _ in M.WINDOWS), str(dv.get("show")))
check("a top-10 row the cache cannot serve gets drivers with no source and says why",
      (lambda o: (o["rows"]["IWM"]["rest"]["drivers"]["source"], "no N-PORT" in o["errors"].get("IWM", "")))(
          M.build(TECHB, (TB, serb), {"ssga": b_ssga, "top10": b_top10, "history": b_hist},
                  today=TB[-1], log=lambda *a: None, deep={"funds": {}, "symbols": {}})), (None, True))
check("without a deep cache the payload has no drivers at all (yesterday's shape)",
      "drivers" in M.build(TECHB, (TB, serb), {"ssga": b_ssga, "top10": b_top10, "history": b_hist},
                           today=TB[-1], log=lambda *a: None)["rows"]["IWM"]["rest"], False)
dumped = json.dumps(outb, indent=0, separators=(",", ":"), ensure_ascii=False)
check("the payload, written one field per line, is guard-clean", guard_hits(dumped), [])
check("...and the rows' rest lines keep every section-10 field",
      sorted(outb["rows"]["IWM"]["rest"]), ["c", "drivers", "kind", "n", "w", "w_unpriced"])

print("\n" + "-" * 60)
print(f"{len(PASSED)} passed, {len(FAILS)} failed")
if FAILS:
    print("FAILED: " + ", ".join(FAILS))
    sys.exit(1)
