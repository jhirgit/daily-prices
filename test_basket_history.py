#!/usr/bin/env python3
"""
Tests for basket_history.py (B1 stage 5, 2026-11-20). Offline, fixtures only.

The no-look-ahead test (recon-3 section 7, stage 5): a quarter's company
vintage and its composite are unchanged when every fact filed AFTER the date is
mutated, and a quarter whose 10-Q was filed after the date is not used. Also:
the declared split is not applied before its date, one book per calendar
quarter, the request budget stops a dispatch and the next one resumes without
downloading anything twice, percentiles need 16 quarters, the SPY ratio, and a
basket's equal-weight book.

Run:  python test_basket_history.py
"""

import copy
import datetime as dt
import os
import sys

import basket_history as H

PASSED, FAILS = [], []
HERE = os.path.dirname(os.path.abspath(__file__))


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}" + ("" if ok else f", want {want!r}"))
    (PASSED if ok else FAILS).append(name)


def check_close(name, got, want, tol=1e-9):
    ok = got is not None and want is not None and abs(got - want) <= tol
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r} (tol {tol})")
    (PASSED if ok else FAILS).append(name)


def qends(n, first=(2019, 3)):
    out, (y, m) = [], first
    for _ in range(n):
        d = dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1)
        out.append(d.isoformat())
        m += 3
        if m > 12:
            y, m = y + 1, m - 12
    return out


def plus(d, n):
    return (dt.date.fromisoformat(d) + dt.timedelta(days=n)).isoformat()


def start_of(e):
    d = dt.date.fromisoformat(e)
    m = d.month - 2
    y = d.year
    if m <= 0:
        y, m = y - 1, m + 12
    return dt.date(y, m, 1).isoformat()


Q = qends(12)                         # 2019-03-31 .. 2021-12-31


def companyfacts(rev0=100e6, ni0=10e6, shares=50e6, restate=None):
    """A small but real-shaped companyfacts: 3-month revenue, operating income and net
    income for each quarter, each filed 40 days after its end, a cover share count."""
    def flows(base, g):
        arr = [{"start": start_of(e), "end": e, "val": base * (1 + g) ** i, "filed": plus(e, 40), "form": "10-Q",
                "accn": f"a{i}"} for i, e in enumerate(Q)]
        if restate:
            e, factor, filed = restate
            i = Q.index(e)
            arr.append(dict(arr[i], val=arr[i]["val"] * factor, filed=filed, accn="restated"))
        return arr
    return {"entityName": "Example Corp", "facts": {
        "us-gaap": {"Revenues": {"units": {"USD": flows(rev0, 0.02)}},
                    "OperatingIncomeLoss": {"units": {"USD": flows(rev0 * 0.2, 0.02)}},
                    "NetIncomeLoss": {"units": {"USD": flows(ni0, 0.03)}}},
        "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
            {"end": plus(e, 30), "val": shares, "filed": plus(e, 40), "accn": f"a{i}"} for i, e in enumerate(Q)]}}}}}


print("vintage(): a company as known on a date")
CF = companyfacts()
asof = "2020-10-15"          # the 2020-06-30 10-Q was filed 2020-08-09; the 2020-09-30 one on 2020-11-09
v = dict(zip(H.VINT_FIELDS, H.vintage(CF, "EXMP", asof)))
check("the latest quarter known on 2020-10-15 is 2020-06-30 (the Q3 10-Q came 2020-11-09)", v["end"], "2020-06-30")
check_close("TTM net income = the four quarters to 2020-06-30", v["ni"], round(sum(10e6 * 1.03 ** i for i in range(2, 6)) / 1e6, 1), 0.11)
mut = copy.deepcopy(CF)
for tax in mut["facts"].values():
    for c in tax.values():
        for arr in c["units"].values():
            for x in arr:
                if x["filed"] > asof:
                    x["val"] *= 10.0
check("NO LOOK-AHEAD: every fact filed after the date multiplied by 10 -- the vintage is identical",
      H.vintage(mut, "EXMP", asof), H.vintage(CF, "EXMP", asof))
RS = companyfacts(restate=("2020-03-31", 0.5, "2021-03-01"))
check("a restatement filed later (2021-03-01) does not reach the 2020-10-15 vintage",
      H.vintage(RS, "EXMP", asof), H.vintage(CF, "EXMP", asof))
check("...but does reach one dated after it",
      H.vintage(RS, "EXMP", "2021-03-15") != H.vintage(CF, "EXMP", "2021-03-15"), True)
check("before four quarters were filed there is no TTM: no vintage", H.vintage(CF, "EXMP", "2019-09-30"), None)
vs = dict(zip(H.VINT_FIELDS, H.vintage(CF, "SNDK", asof)))
check_close("a declared split (SNDK, 2026-11-19) is NOT applied to a 2020 vintage: the count as filed",
            vs["sh"], v["sh"], 1e-9)

print("\ncomposite_on(): one book on one date, from vintages only")
CMAP = {"EXMP": "1", "OTHR": "2"}
VINT = {"1": {"v": {asof: H.vintage(CF, "EXMP", asof)}},
        "2": {"v": {asof: H.vintage(companyfacts(rev0=50e6, ni0=-2e6), "OTHR", asof)}}}
BOOK = [[None, "EXMP", 0.6, 30.0], [None, "OTHR", 0.3, 10.0], ["XX0000000000", None, 0.1, 5.0]]
m, cov, gaps = H.composite_on(BOOK, asof, CMAP, {}, VINT)
check_close("coverage 90% (the unmapped line is a gap)", cov, 0.9, 1e-12)
ni1, ni2 = dict(zip(H.VINT_FIELDS, VINT["1"]["v"][asof]))["ni"], dict(zip(H.VINT_FIELDS, VINT["2"]["v"][asof]))["ni"]
check_close("P/E = harmonic over the line's own price x the share count known then",
            m["pe"], 0.9 / (0.6 * ni1 / (30.0 * 50.0) + 0.3 * ni2 / (10.0 * 50.0)), 1e-9)
VINT_M = {"1": {"v": {asof: H.vintage(mut, "EXMP", asof)}}, "2": VINT["2"]}
check("NO LOOK-AHEAD: the composite is unchanged when later filings are mutated",
      H.composite_on(BOOK, asof, CMAP, {}, VINT_M)[0], m)
check("a date with no vintage built is a gap, never the nearest other date",
      H.composite_on(BOOK, "2020-12-31", CMAP, {}, VINT)[0], None)

print("\nphase 1: books, one per calendar quarter, resumable inside a budget")
TPL = open(os.path.join(HERE, "data", "fixtures", "nport_arty_trimmed.xml"), encoding="utf-8").read()
ACCS = [("0000000000-26-00000%d" % i, f, p) for i, (f, p) in enumerate(
    [("2026-05-22", "2026-03-31"), ("2026-06-29", "2026-04-30"), ("2026-08-24", "2026-06-30")], 1)]
ATOM = ("<feed xmlns='http://www.w3.org/2005/Atom'>" + "".join(
    f"<entry><content type='text/xml'><accession-number>{a}</accession-number><filing-date>{d}</filing-date>"
    f"<filing-href>https://www.sec.gov/Archives/edgar/data/1100663/x/{a}-index.htm</filing-href>"
    f"<filing-type>NPORT-P</filing-type></content></entry>" for a, d, _ in ACCS) + "</feed>").encode()
got = []


def fake_get(url):
    got.append(url)
    if "browse-edgar" in url:
        return ATOM
    for a, _, p in ACCS:
        if a.replace("-", "") in url:
            return TPL.replace("<repPdDate>2026-06-30</repPdDate>", f"<repPdDate>{p}</repPdDate>").encode()
    raise AssertionError(url)


state = {}
b = H.Budget(2, 60)
check("a budget of 2 requests: the listing and one filing, then stop", H.fetch_books("ARTY", "S000062201", state, fake_get, b, log=lambda *a: None), False)
check("...the work done is recorded", (len(state["ARTY"]["done"]), state["ARTY"]["complete"]), (1, False))
n_before = len(got)
done = H.fetch_books("ARTY", "S000062201", state, fake_get, H.Budget(100, 60), log=lambda *a: None)
check("the next dispatch resumes: no second listing, no filing downloaded twice", (done, len(got) - n_before), (True, 2))
check("one book per calendar quarter (April folds into June's quarter)", sorted(state["ARTY"]["books"]), ["2026-03-31", "2026-06-30"])
check("a book line is [isin, ticker, weight, price on the date]", len(state["ARTY"]["books"]["2026-06-30"]["lines"][0]), 4)
n_before = len(got)
H.fetch_books("ARTY", "S000062201", state, fake_get, H.Budget(100, 60), log=lambda *a: None)
check("a complete fund costs nothing on a later dispatch", len(got) - n_before, 0)
check("Budget: the wall clock stops a dispatch too", H.Budget(100, 0.0001, clock=iter([0.0, 999.0]).__next__).ok(), False)

print("\npercentiles and the history block")
hist = {"book": "SEC N-PORT books of XLK", "q": [{"date": d, "m": {"pe": 10.0 + i, "ev_sales": 2.0, "ev_ebit": None,
                                                                  "fcf_yield": 0.05, "pb": None}}
                                                for i, d in enumerate(qends(20, (2021, 3)))]}
spy = {"q": [{"date": d, "m": {"pe": 20.0, "ev_sales": 2.0, "ev_ebit": None, "fcf_yield": 0.04, "pb": None}}
             for d in qends(20, (2021, 3))]}
blk = H.history_block("XLK", {"pe": 14.5, "ev_sales": 2.0, "fcf_yield": 0.05}, hist, spy, {"pe": 20.0, "ev_sales": 2.0, "fcf_yield": 0.04})
check("20 quarters; P/E 14.5 sits above 5 of them (10..14): the 25th percentile", (blk["n"], blk["pct"]["pe"]), (20, 0.25))
check("vs SPY: today's 0.725 ratio against the quarters' 0.50..1.45", blk["pct_vs_spy"]["pe"], 0.25)
check("a measure with no history has no percentile", blk["pct"]["ev_ebit"], None)
short = dict(hist, q=hist["q"][:10])
b2 = H.history_block("XLK", {"pe": 14.5}, short, spy, {"pe": 20.0})
check("ten quarters: no percentile, and it says why", (b2["pct"]["pe"], "a percentile needs 16" in b2["why"]), (None, True))
check("SPY has no ratio to itself", H.history_block("SPY", {"pe": 20.0}, spy, spy, {"pe": 20.0})["pct_vs_spy"]["pe"], None)

VS = {"1": dict(VINT["1"], splits=[["2021-06-01", 4.0]])}
mp, _, _ = H.composite_on([[None, "EXMP", 1.0, 30.0 / 4, "db"]], asof, CMAP, {}, VS)
mn, _, _ = H.composite_on([[None, "EXMP", 1.0, 30.0]], asof, CMAP, {}, VS)
check_close("a basket line priced from prices.db (re-based for a later 4-for-1 split) gets the same market "
            "value as the real price of the day", mp["pe"], mn["pe"], 1e-9)
check("later_split_factor counts only splits after the date", (H.later_split_factor([["2021-06-01", 4.0]], asof),
      H.later_split_factor([["2020-01-01", 4.0]], asof)), (4.0, 1.0))

print("\nbaskets and targets")
PX = {("AAA", "2020-03-31"): 10.0, ("BBB", "2020-03-31"): 20.0}
check("a basket's book: today's members with a price that day, equal weight",
      H.basket_book(["AAA", "BBB", "CCC"], "2020-03-31", lambda s, d: PX.get((s, d))),
      [[None, "AAA", 0.5, 10.0, "db"], [None, "BBB", 0.5, 20.0, "db"]])
check("targets 'shown': the shown rows, plus SPY always",
      H.targets("shown", {"rows": {"XLK": {"shown": True}, "COPX": {"shown": False}}}, {}), ["SPY", "XLK"])
check("quarter ends from 2019-Q3", H.quarter_ends("2019-07-01", "2020-01-15"), ["2019-09-30", "2019-12-31"])

print("\nrun(): end to end with fakes, then resumed")
MEM = {"rows": {"ARTY": {"tier": "top10"}, "SPY": {"tier": "issuer_full"}}}
CFS = {"1": CF}
fetched = []


def cf_of(cik):
    fetched.append(cik)
    return CFS[cik]


books, vints = {}, {}
series = {"ARTY": "S000062201", "IVV": None}
pl, pr = H.run(["ARTY", "SPY"], MEM, {}, {"NVDA": "1"}, books, vints, fake_get, cf_of, lambda s, d: None,
               H.Budget(1000, 60), "2026-11-20", series.get, log=lambda *a: None)
check("SPY's book is IVV's (SPY files no N-PORT); with no series it is marked, not fetched",
      (books["IVV"]["why"], pl["rows"]["SPY"]["book"]), ("no N-PORT fund series found",
                                                        "SEC N-PORT books of IVV (a proxy: SPY files no N-PORT)"))
check("each company is fetched once", fetched.count("1") <= 1, True)
check("the payload lists every book date per row", [q["date"] for q in pl["rows"]["ARTY"]["q"]], ["2026-03-31", "2026-06-30"])
check("progress says complete", (pl["status"], pr["complete"]), ("complete", True))

print("\n" + "-" * 60)
print(f"{len(PASSED)} passed, {len(FAILS)} failed")
if FAILS:
    print("FAILED: " + ", ".join(FAILS))
    sys.exit(1)
