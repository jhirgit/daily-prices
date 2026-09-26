#!/usr/bin/env python3
"""
Tests for rotation_members.py (SPEC-86 Part A).

No network: every case drives the pure functions or build() with fake
fetchers and a synthetic panel, so what is under test is the part that can be
wrong quietly -- the drift-back arithmetic, the exact basket attribution, the
FX conversion, the rest line, the symbol map, the 1-sigma gate, the
carry-forward of yesterday's holdings and the sort order.

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
for k, _ in M.WINDOWS:
    check_close(f"NEOCLOUD {k}: sum c == ladder R", sum(h["c"][k] for h in neo["holdings"]),
                TECH["regime"]["ladder"]["rows"][2][k], 1e-5)
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

print("\n" + "-" * 60)
print(f"{len(PASSED)} passed, {len(FAILS)} failed")
if FAILS:
    print("FAILED: " + ", ".join(FAILS))
    sys.exit(1)
