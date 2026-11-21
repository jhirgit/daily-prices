#!/usr/bin/env python3
"""
Tests for basket_fundamentals.py (B1 stage 4, 2026-11-20).

No network. What is under test is what can be wrong quietly: the look-through
aggregation (harmonic, not arithmetic; losses in; aggregate over aggregate),
the coverage gate, the by-CIK merge of share classes, every reason a line is
uncovered, the refresh rules and budget, the store round trip, a source that
changed shape, and the prototype's XLK P/E (recon-3 proto_fund.py, 27.9x).

Run:  python test_basket_fundamentals.py
"""

import json
import os
import re
import sys
import tempfile

import basket_fundamentals as B

PASSED, FAILS = [], []
HERE = os.path.dirname(os.path.abspath(__file__))
TODAY = "2026-11-20"


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}" + ("" if ok else f", want {want!r}"))
    (PASSED if ok else FAILS).append(name)


def check_close(name, got, want, tol=1e-9):
    ok = got is not None and want is not None and abs(got - want) <= tol
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r} (tol {tol})")
    (PASSED if ok else FAILS).append(name)


def check_true(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{(' -- ' + detail) if detail else ''}")
    (PASSED if cond else FAILS).append(name)


def co(t, w, mcap, ni, rev=None, oi=None, ebitda=None, cash=0.0, debt=0.0, kind="operating", **k):
    """A resolved company (USD millions), as resolve() hands them to composite()."""
    d = {"cik": t, "t": t, "w": w, "mcap": mcap, "ni": ni, "rev": rev, "oi": oi, "ebitda": ebitda,
         "cash": cash, "debt": debt, "kind": kind, "end": "2026-09-30", "sh": 1.0, "sh_p": None}
    for f in ("fcf", "capex", "equity", "rev_p", "oi_p", "ni_p", "div", "bb", "intexp"):
        d.setdefault(f, None)
    d.update(k)
    return d


# --------------------------------------------------------------------------
print("composite(): look-through arithmetic")
A = co("A", 0.5, 100.0, 5.0, rev=50.0, oi=10.0, ebitda=12.0, cash=10.0, debt=30.0, rev_p=40.0, ni_p=4.0,
       oi_p=6.0, fcf=4.0, capex=2.0, equity=40.0, div=1.0, bb=2.0, intexp=1.0, sh_p=0.98)
Bq = co("B", 0.3, 50.0, -2.0, rev=20.0, oi=-1.0, ebitda=1.0, cash=20.0, debt=0.0, rev_p=10.0, ni_p=-1.0,
        oi_p=-2.0, fcf=-3.0, capex=4.0, equity=30.0)
Cb = co("C", 0.2, 200.0, 20.0, rev=80.0, kind="bank", equity=150.0, div=6.0, debt=None, cash=None)
m = B.composite([A, Bq, Cb], 1.0)
ey = (0.5 * 5 / 100 + 0.3 * -2 / 50 + 0.2 * 20 / 200)
check_close("P/E is the weighted HARMONIC mean, losses in: sum(w) / sum(w E / mcap)", m["pe"], 1.0 / ey)
arith = (0.5 * 20 + 0.2 * 10) / 0.7
check_true("...which is not the arithmetic mean of the profitable names' P/Es", abs(m["pe"] - arith) > 1.0,
           f"harmonic {m['pe']:.2f} vs arithmetic {arith:.2f}")
check("one loss-maker, 30% of the weight", (m["loss_n"], round(m["loss_w"], 9)), (1, 0.3))
check_close("median P/E of the profitable names", m["pe_median_profitable"], 15.0)
evA, evB = 100 + 30 - 10, 50 + 0 - 20
check_close("EV/sales over operating companies only (the bank is out)", m["ev_sales"],
            (0.5 * evA / 100 + 0.3 * evB / 50) / (0.5 * 50 / 100 + 0.3 * 20 / 50))
check_close("EV/EBIT = aggregate EV over aggregate EBIT", m["ev_ebit"],
            (0.5 * evA / 100 + 0.3 * evB / 50) / (0.5 * 10 / 100 + 0.3 * -1 / 50))
check_close("revenue growth holds today's ownership fixed", m["rev_growth"],
            (0.5 * 50 / 100 + 0.3 * 20 / 50) / (0.5 * 40 / 100 + 0.3 * 10 / 50) - 1.0)
check_true("...a company without last year's revenue (the bank) is left out of BOTH sides, not counted as zero",
           abs(m["rev_growth"] - ((0.5 * 50 / 100 + 0.3 * 20 / 50 + 0.2 * 80 / 200)
                                  / (0.5 * 40 / 100 + 0.3 * 10 / 50) - 1.0)) > 0.1)
check_close("operating margin now", m["op_margin"], (0.5 * 10 / 100 + 0.3 * -1 / 50) / (0.5 * 50 / 100 + 0.3 * 20 / 50))
check_close("...and a year ago", m["op_margin_prev"], (0.5 * 6 / 100 + 0.3 * -2 / 50) / (0.5 * 40 / 100 + 0.3 * 10 / 50))
check_close("net debt / EBITDA", m["nd_ebitda"], (0.5 * 20 / 100 + 0.3 * -20 / 50) / (0.5 * 12 / 100 + 0.3 * 1 / 50))
check_close("net-cash share of weight (B has more cash than debt)", m["net_cash_w"], 0.3)
check_close("FCF yield: negative FCF is in", m["fcf_yield"], (0.5 * 4 / 100 + 0.3 * -3 / 50) / 0.8)
check_close("dividend yield includes the bank", m["div_yield"], (0.5 * 1 / 100 + 0.2 * 6 / 200) / 0.7)
check_close("P/B over every company with equity", m["pb"], 1.0 / (0.5 * 40 / 100 + 0.3 * 30 / 50 + 0.2 * 150 / 200))
check_close("financials' share of weight", m["fin_w"], 0.2)
check_close("share-count change, weighted", m["share_change"], 1.0 / 0.98 - 1.0)
check("median interest cover over operating companies with interest", (m["int_cover_median"], m["int_cover_n"]), (10.0, 1))
loss = B.composite([co("L", 1.0, 10.0, -1.0)], 1.0)
check("an aggregate loss: P/E is n/m and says so, never a negative multiple", (loss["pe"], loss["pe_nm"]),
      (None, "aggregate loss"))

# --------------------------------------------------------------------------
print("\nresolve(): coverage, by-CIK merge, every gap reason")
CIK = {"GOOGL": "1", "GOOG": "1", "AAPL": "2", "OLD": "3", "NOPX": "4", "TSM": "5", "NOFIN": "6"}
REC = {"1": {"status": "ok", "end": "2026-09-30", "ni": 10.0, "rev": 50.0, "sh": 12.0, "ccy": "USD"},
       "2": {"status": "ok", "end": "2026-09-27", "ni": 100.0, "rev": 400.0, "sh": 15.0, "ccy": "USD"},
       "3": {"status": "ok", "end": "2025-12-31", "ni": 1.0, "rev": 5.0, "sh": 1.0, "ccy": "USD"},
       "4": {"status": "ok", "end": "2026-09-30", "ni": 1.0, "rev": 5.0, "sh": 1.0, "ccy": "USD"},
       "5": {"status": "ok", "end": "2026-09-30", "ni": 1.0, "rev": 5.0, "sh": 1.0, "ccy": "TWD", "adr": 5.0},
       "6": {"status": "na", "why": "x"}}
PX = {"GOOGL": 300.0, "GOOG": 301.0, "AAPL": 270.0, "OLD": 5.0, "TSM": 300.0, "NOFIN": 3.0}
lines = [{"sym": "GOOGL", "w": 0.10}, {"sym": "GOOG", "w": 0.08}, {"sym": "AAPL", "w": 0.40},
         {"sym": "2330.TW", "w": 0.05}, {"sym": "ZZZZ", "w": 0.04}, {"sym": "OLD", "w": 0.03},
         {"sym": "NOPX", "w": 0.02}, {"sym": "TSM", "w": 0.06}, {"sym": "NOFIN", "w": 0.01}, {"sym": None, "w": 0.01}]
cos, gaps = B.resolve(lines, CIK.get, lambda c, s: REC.get(c), PX.get, {"USD": 1.0}.get, TODAY)
check("two share classes are ONE company, their weights added", sorted((c["t"], round(c["w"], 9)) for c in cos),
      [("AAPL", 0.4), ("GOOGL", 0.18)])
check("every uncovered line has its reason, by weight", {k: round(v, 9) for k, v in gaps.items()}, {
    "listed outside the US (no SEC filer under this symbol)": 0.05,
    "not an SEC filer (not in SEC's ticker map)": 0.04,
    "latest SEC figures older than 200 days (annual filer)": 0.03,
    "no price or share count": 0.02,
    "no FX rate for TWD": 0.06,
    "no usable SEC financials": 0.01,
    "no listing symbol for the line": 0.01})
cos2, _ = B.resolve([{"sym": "TSM", "w": 1.0}], CIK.get, lambda c, s: REC.get(c), PX.get,
                    {"USD": 1.0, "TWD": 0.031}.get, TODAY)
check_close("an ADR's market value is price x shares / ADR ratio; TWD statements converted at the FX",
            cos2[0]["mcap"], 300.0 * 1.0 / 5.0)
check_close("...and its earnings in dollars", cos2[0]["ni"], 0.031)
out, _ = B.row_composite([{"sym": "AAPL", "w": 0.69}, {"sym": "ZZZZ", "w": 0.31}], "test", CIK.get,
                         lambda c, s: REC.get(c), PX.get, {"USD": 1.0}.get, TODAY)
check("69% covered: not shown, no measures, and the reason names the gap",
      (out["shown"], out["m"], "69 pct" in out["why"] and "not an SEC filer" in out["why"]), (False, None, True))
out, _ = B.row_composite([{"sym": "AAPL", "w": 0.71}, {"sym": "ZZZZ", "w": 0.29}], "test", CIK.get,
                         lambda c, s: REC.get(c), PX.get, {"USD": 1.0}.get, TODAY)
check("71% covered: shown", (out["shown"], out["coverage_w"], out["why"]), (True, 0.71, None))
check_close("...P/E of the covered part", out["m"]["pe"], 270.0 * 15.0 / 100.0, 1e-6)

# --------------------------------------------------------------------------
print("\ndeclared splits (fundamentals.DECLARED_SPLITS): SNDK 8-for-1 on 2026-11-19")
SN = {"status": "ok", "end": "2026-10-02", "ni": 19422.0, "rev": 28971.0, "sh": 142.289, "sh_p": 140.0,
      "sh_end": "2026-10-26", "ccy": "USD"}
cs_, _ = B.resolve([{"sym": "SNDK", "w": 1.0}], {"SNDK": "9"}.get, lambda c, s: dict(SN, t="SNDK"),
                   {"SNDK": 79.24}.get, {"USD": 1.0}.get, TODAY)
check_close("a pre-split filed count meets the post-split price: market value x8", cs_[0]["mcap"], 79.24 * 142.289 * 8, 1e-6)
check_close("...and the year-ago count on the same footing (the share change is not sevenfold)",
            cs_[0]["sh"] / cs_[0]["sh_p"] - 1, 142.289 / 140.0 - 1, 1e-12)
cs_, _ = B.resolve([{"sym": "SNDK", "w": 1.0}], {"SNDK": "9"}.get,
                   lambda c, s: dict(SN, t="SNDK", decl=["2026-11-19x8"]), {"SNDK": 79.24}.get, {"USD": 1.0}.get, TODAY)
check_close("a record already built with the split is not adjusted twice", cs_[0]["mcap"], 79.24 * 142.289, 1e-6)
cs_, _ = B.resolve([{"sym": "SNDK", "w": 1.0}], {"SNDK": "9"}.get,
                   lambda c, s: dict(SN, t="SNDK", sh=1138.3, sh_end="2027-01-23"), {"SNDK": 79.24}.get,
                   {"USD": 1.0}.get, TODAY)
check_close("a count dated after the split is already the new basis", cs_[0]["mcap"], 79.24 * 1138.3, 1e-6)
cs_, _ = B.resolve([{"sym": "MU", "w": 1.0}], {"MU": "8"}.get, lambda c, s: dict(SN, t="MU"),
                   {"MU": 79.24}.get, {"USD": 1.0}.get, TODAY)
check_close("control: a ticker with no declared split is untouched", cs_[0]["mcap"], 79.24 * 142.289, 1e-6)

# --------------------------------------------------------------------------
print("\nthe prototype: XLK's harmonic P/E (recon-3 proto_fund.py)")
with open(os.path.join(HERE, "data", "fixtures", "basket_xlk_pe.json"), encoding="utf-8") as fh:
    PXF = json.load(fh)
# each line as a company of market value 1 and earnings 1/P/E: the same owned
# earnings yield per unit of weight the prototype had
xc = [co(s, w / 100.0, 1.0, (1.0 / pe) if pe else None) for s, w, pe in PXF["lines"]]
xm = B.composite(xc, sum(c["w"] for c in xc))
check_close("XLK P/E 27.9x, harmonic with the three loss-makers in (inputs rounded to 0.1x)",
            xm["pe"], PXF["pe_ttm_harmonic"], 0.05)
check("...three loss-makers", xm["loss_n"], PXF["loss_n"])
check_close("...2.3% of the weight", xm["loss_w"], PXF["loss_w"], 0.001)
check_close("...median P/E of the profitable names 27.9x", xm["pe_median_profitable"], PXF["pe_median_positive"], 0.06)

# --------------------------------------------------------------------------
print("\nsummarise(): the compact company record")
REC_B = {"status": "ok", "kind": "operating", "currency": "USD", "adr_ratio": 1.0, "quarters": [
    {"end": "2025-09-27", "rev": 400.0, "oi": 120.0, "ni": 90.0},
    {"end": "2025-12-27", "rev": 410.0, "oi": 125.0, "ni": 95.0},
    {"end": "2026-09-26", "rev": 450.0, "oi": 150.0, "ni": 110.0, "ebitda": 160.0, "cash": 50.0,
     "debt": 80.0, "equity": 100.0, "fcf": 120.0, "capex": 9.0, "known": "2026-11-03"}],
    "shares": [{"end": "2025-10-17", "adj": 15000.0}, {"end": "2026-07-17", "adj": 14700.0},
               {"end": "2026-10-23", "adj": 14600.0}]}
s = B.summarise(REC_B, {"div": 15.0, "bb": 90.0, "intexp": 3.0}, t="AAPL", today=TODAY)
check("latest quarter's TTM and the one four quarters earlier (350-380 days)",
      (s["end"], s["rev"], s["end_p"], s["rev_p"], s["ni_p"]), ("2026-09-26", 450.0, "2025-09-27", 400.0, 90.0))
check("latest filed share count, and the one about a year before it", (s["sh"], s["sh_p"]), (14600.0, 15000.0))
check("extras carried", (s["div"], s["bb"], s["intexp"]), (15.0, 90.0, 3.0))
check("no share count: partial with the reason",
      B.summarise(dict(REC_B, shares=[]), None, t="X", today=TODAY)["status"], "partial")
check("no quarters: na", B.summarise({"status": "ok", "quarters": []}, None)["status"], "na")

# --------------------------------------------------------------------------
print("\ndue(): when a company is fetched again")
check("never fetched", B.due(None, TODAY), True)
check("a fresh quarter (ends 2026-09-30): not due", B.due({"fetched": "2026-11-01", "status": "ok", "end": "2026-09-30"}, TODAY), False)
check("a quarter 140 days old, checked 3 days ago: wait", B.due({"fetched": "2026-11-17", "status": "ok", "end": "2026-07-03"}, TODAY), False)
check("...checked 8 days ago: due", B.due({"fetched": "2026-11-12", "status": "ok", "end": "2026-07-03"}, TODAY), True)
check("an annual filer (TTM 280 days old) checked 10 days ago: monthly, not yet",
      B.due({"fetched": "2026-11-10", "status": "ok", "end": "2026-02-13"}, TODAY), False)
check("an error yesterday: retried", B.due({"fetched": "2026-11-19", "status": "error"}, TODAY), True)
check("'no financials' two weeks ago: not yet", B.due({"fetched": "2026-11-06", "status": "na"}, TODAY), False)

# --------------------------------------------------------------------------
print("\nbuild(): end to end with fake inputs, the budget, a failed fetch, the store")
MEM = {"asof": TODAY, "rows": {
    "SPY": {"tier": "issuer_full", "source": "State Street daily holdings file", "holdings_asof": "2026-11-19",
            "holdings": [{"t": "AAPL", "w": 0.6}, {"t": "MSFT", "w": 0.4}]},
    "XLK": {"tier": "issuer_full", "source": "State Street daily holdings file", "holdings_asof": "2026-11-19",
            "holdings": [{"t": "AAPL", "w": 0.5}, {"t": "MSFT", "w": 0.3}, {"t": "NOPE", "w": 0.2}]},
    "IWM": {"tier": "top10", "holdings": [{"t": "AAPL", "w": 0.01}]},
    "SLV": {"tier": "trust", "holdings": [{"t": "XAG", "w": 1.0}]}}}
DEEP = {"funds": {"IWM": {"period": "2026-09-30", "lines": [["Apple Inc", "US0378331005", "AAPL", "USD", 0.6, 250.0],
                                                           ["Small Co", "US0000000001", None, "USD", 0.4, 3.0]]}},
        "symbols": {"US0378331005": ["AAPL", "2026-11-01"]}}
CM = {"map": {"AAPL": "0000320193", "MSFT": "0000789019", "NOPE": "0000000009"}}
LAST = {"closes": {"AAPL": ["2026-11-20", 270.0, "USD"], "MSFT": ["2026-11-20", 500.0, "USD"],
                   "NOPE": ["2026-11-20", 10.0, "USD"]}, "fx": {}}
GOOD = {"0000320193": dict(B.summarise(REC_B, {}, t="AAPL", today=TODAY)),
        "0000789019": dict(B.summarise(dict(REC_B, quarters=[dict(q, ni=q["ni"] * 0.8) for q in REC_B["quarters"]],
                                            shares=[dict(x, adj=x["adj"] / 2) for x in REC_B["shares"]]), {},
                                       t="MSFT", today=TODAY))}
fetched = []


def fake_fetch(cik):
    fetched.append(cik)
    if cik == "0000000009":
        class E(Exception):
            code = 404
        raise E("not found")
    return {"cik": cik}


real_cff = B.company_from_facts
B.company_from_facts = lambda cf, t, today: dict(GOOD[cf["cik"]], fetched=today)
store = {"companies": {}}
pl, store, calls = B.build(MEM, DEEP, CM, store, {}, LAST, None, TODAY, fetch=fake_fetch, log=lambda *a: None)
check("every wanted company due on a cold store, heaviest first", fetched, ["0000320193", "0000789019", "0000000009"])
check("a 404 is recorded as 'no companyfacts', not an error", store["companies"]["0000000009"]["status"], "na")
check("SPY and XLK shown; SLV (a trust) not; IWM's N-PORT book only 60% covered, so not shown",
      {t: r["shown"] for t, r in pl["rows"].items()}, {"SPY": True, "XLK": True, "IWM": False, "SLV": False})
check("IWM's lines are its whole N-PORT book, the unmapped line uncovered",
      (pl["rows"]["IWM"]["lines_source"], pl["rows"]["IWM"]["coverage_w"]),
      ("SEC N-PORT, every line as of 2026-09-30", 0.6))
check_true("XLK carries a ratio to SPY's composite", (pl["rows"]["XLK"].get("vs_spy") or {}).get("pe") is not None)
check("per_line: each company's own P/E and revenue growth for the holdings table",
      sorted(pl["rows"]["XLK"]["per_line"]), ["AAPL", "MSFT"])
check_close("...AAPL's own P/E = 270 x 14,600 / 110", pl["rows"]["XLK"]["per_line"]["AAPL"][0], round(270 * 14600 / 110, 1))
check("status ok, error says nothing waits", (pl["status"], pl["error"]), ("ok", None))
fetched.clear()
pl2, store, calls = B.build(MEM, DEEP, CM, store, {}, LAST, None, TODAY, fetch=fake_fetch, log=lambda *a: None)
check("the next run the same day fetches nothing", fetched, [])
store3 = {"companies": {}}
pl3, store3, calls3 = B.build(MEM, DEEP, CM, store3, {}, LAST, None, TODAY, fetch=fake_fetch, max_fetch=1,
                              log=lambda *a: None)
check("a request cap: the rest wait, and the payload says how many", (calls3["sec"], calls3["budget_left"]), (1, 2))
check_true("...in its error line", "2 companies still wait" in (pl3["error"] or ""), pl3["error"])


def boom(cik):
    raise OSError("SEC down")


st4 = {"companies": {"0000320193": dict(GOOD["0000320193"], fetched="2026-11-01", end="2026-06-27")}}
pl4, st4, c4 = B.build(MEM, DEEP, CM, st4, {}, LAST, None, TODAY, fetch=boom, log=lambda *a: None)
check("a failed fetch keeps the last good record (re-dated, so it is not hammered)",
      (st4["companies"]["0000320193"]["end"], st4["companies"]["0000320193"]["fetched"]), ("2026-06-27", TODAY))
check("...and a company never fetched becomes an error to retry tomorrow", st4["companies"]["0000789019"]["status"], "error")
B.company_from_facts = real_cff
plN, _, _ = B.build(MEM, DEEP, CM, {"companies": dict(store["companies"])}, {}, {}, None, TODAY, fetch=None,
                    log=lambda *a: None)
check_true("no last closes from rotation_members: status partial and the error says so (loud)",
           plN["status"] == "partial" and "no last closes" in plN["error"], plN["error"])
plE, _, _ = B.build({"rows": {}}, DEEP, CM, {"companies": {}}, {}, LAST, None, TODAY, log=lambda *a: None)
check("rotation_members.json with no rows (the source changed shape): status failed", plE["status"], "failed")
stO = {"companies": {}}
plO, stO, cO = B.build(MEM, DEEP, CM, stO, {}, LAST, None, TODAY, fetch=lambda cik: None, log=lambda *a: None)
check("an offline cache miss (fetch answers None) changes nothing and is not a failure",
      (stO["companies"], cO["sec"], cO["sec_failed"], cO["unavailable"], plO["status"]), ({}, 0, 0, 3, "ok"))
check_true("...and the payload still says the companies are waiting", "3 companies still wait" in (plO["error"] or ""),
           plO["error"])
cut = {"funds": {"IWM": dict(DEEP["funds"]["IWM"], tail={"n": 1934, "w": 0.8})}, "symbols": DEEP["symbols"]}
ln_, src_ = B.vehicle_lines(MEM["rows"]["IWM"], cut["funds"]["IWM"], cut["symbols"])
check("a cut-short N-PORT book counts the weight it does not list, as a gap with its reason",
      (ln_[-1]["sym"], ln_[-1]["w"], ln_[-1]["gap"], "heaviest 2 lines" in src_),
      (None, 0.8, "lines past the stored N-PORT book", True))
FUND = {"AAPL": dict(REC_B, status="ok")}
plF, _, _ = B.build(MEM, DEEP, CM, {"companies": {}}, FUND, LAST, None, TODAY, fetch=None, log=lambda *a: None)
check("offline with an empty store, fundamentals.json stands in for the names it carries",
      sorted(plF["rows"]["XLK"]["per_line"]), ["AAPL"])

# --------------------------------------------------------------------------
print("\nfiles: the store round trip, guard-clean, no advice words")
d = tempfile.mkdtemp(prefix="bf_")
sp = os.path.join(d, "store.json")
store["generated_at"] = "2026-11-20T23:00:00Z"
B.write_store(store, sp)
back = B.read_store(sp)
def _norm(cs):
    return {k: {f: v for f, v in c.items() if v is not None and f in B.REC_FIELDS} for k, c in cs.items()}


check("store written one company per line and read back the same", _norm(back["companies"]), _norm(store["companies"]))
lines_ = open(sp, encoding="utf-8").read().splitlines()
check_true("...one line per company", sum(1 for ln in lines_ if ln.startswith('"00')) == len(store["companies"]))
op = os.path.join(d, "out.json")
B.write_payload(pl, op)
text = open(op, encoding="utf-8").read()
import rotation_members as RM  # noqa: E402
check("payload, one field per line, is guard-clean", [ln for ln in text.splitlines() if RM.guard_trips(ln)], [])
ADVICE = re.compile(r"\b(buy|sell|should|will|likely|target|recommend|hold|trim|add)\b", re.I)
strings = re.findall(r'"((?:[^"\\]|\\.)*)"', text)
check("no advice words in any payload text", sorted({w.lower() for s_ in strings for w in ADVICE.findall(s_)}), [])
try:
    # assembled at run time so this source line does not itself trip the commit guard it exercises
    B.write_payload({"x": "he" + "ld 5 sha" + "res"}, op)
    check("a guard-tripping line is refused, not written", "written", "ValueError")
except ValueError:
    check("a guard-tripping line is refused, not written", "ValueError", "ValueError")
rc = B.main(["--offline", "--members", os.path.join(d, "missing.json"), "--out", op])
check("main(): a missing rotation_members.json fails loudly and keeps yesterday's file", rc, 1)

print("\n" + "-" * 60)
print(f"{len(PASSED)} passed, {len(FAILS)} failed")
if FAILS:
    print("FAILED: " + ", ".join(FAILS))
    sys.exit(1)
