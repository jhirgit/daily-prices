"""test_extra_funds.py -- extra_funds.json: look-through composites for funds that are not
ladder rows (basket_fundamentals.py `extra_rows`; rotation_members.py lists their N-PORT book).

Offline, synthetic inputs only (no SEC, no Yahoo, no prices.db).

    python test_extra_funds.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

import basket_fundamentals as B
import rotation_members as RM

PASS = FAIL = 0
TODAY = "2026-11-20"


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}\n        got  {got!r}\n        want {want!r}")


def check_true(name, cond, detail=""):
    check(name + (f"  [{detail}]" if detail and not cond else ""), bool(cond), True)


REC = {"status": "ok", "kind": "operating", "currency": "USD", "adr_ratio": 1.0, "quarters": [
    {"end": "2025-09-27", "rev": 400.0, "oi": 120.0, "ni": 90.0},
    {"end": "2026-09-26", "rev": 450.0, "oi": 150.0, "ni": 110.0, "ebitda": 160.0, "cash": 50.0,
     "debt": 80.0, "equity": 100.0, "fcf": 120.0, "capex": 9.0, "known": "2026-11-03"}],
    "shares": [{"end": "2025-10-17", "adj": 15000.0}, {"end": "2026-10-23", "adj": 14600.0}]}
STORE = {"companies": {
    "0000000001": dict(B.summarise(REC, {}, t="AAA", today=TODAY), fetched=TODAY),
    "0000000002": dict(B.summarise(REC, {}, t="BBB", today=TODAY), fetched=TODAY)}}
CM = {"map": {"AAA": "0000000001", "BBB": "0000000002"}}
# prices that give the market row a sane composite (P/E about 21x): basket_fundamentals.market_gate
# withholds every ratio to SPY when SPY's own composite falls outside MARKET_BAND
LAST = {"closes": {"AAA": [TODAY, 0.17, "USD"], "BBB": [TODAY, 0.15, "USD"]}, "fx": {}}
MEM = {"asof": TODAY, "rows": {
    "SPY": {"tier": "issuer_full", "source": "State Street daily holdings file", "holdings_asof": "2026-11-19",
            "holdings": [{"t": "AAA", "w": 0.5}, {"t": "BBB", "w": 0.5}]},
    "QQQ": {"tier": "issuer_full", "source": "Invesco holdings file", "holdings_asof": "2026-11-19",
            "holdings": [{"t": "AAA", "w": 0.7}, {"t": "BBB", "w": 0.3}]}}}
DEEP = {"funds": {"KXF": {"period": "2026-09-30", "lines": [["Aaa Inc", "US0000000AA1", "AAA", "USD", 0.8, 100.0],
                                                            ["Foreign Co", "GB0000000001", None, "GBP", 0.2, 5.0]]},
                  "THN": {"period": "2026-09-30", "lines": [["Aaa Inc", "US0000000AA1", "AAA", "USD", 0.3, 100.0],
                                                            ["Foreign Co", "GB0000000001", None, "GBP", 0.7, 5.0]]}},
        "symbols": {"US0000000AA1": ["AAA", "2026-11-01"]}}
EXTRAS = {"QQQX": {"same_book_as": "QQQ"}, "KXF": {"nport": True}, "THN": {"nport": True},
          "NEW": {"nport": True}, "GONE": {"same_book_as": "NOTAROW"}, "ODD": {"weird": 1}, "QQQ": {"nport": True}}

print("extra_rows: the same machinery for funds that are not ladder rows")
pl, _, calls = B.build(MEM, DEEP, CM, json.loads(json.dumps(STORE)), {}, LAST, None, TODAY, fetch=None,
                       log=lambda *a: None, extras=EXTRAS)
X = pl["extra_rows"]
check("rows holds exactly the ladder; extras sit apart", sorted(pl["rows"]), ["QQQ", "SPY"])
check("every extra that is not a ladder row has a record (QQQ itself is a ladder row, skipped)",
      sorted(X), ["GONE", "KXF", "NEW", "ODD", "QQQX", "THN"])
check("same_book_as: the composite IS the book row's", X["QQQX"]["m"], pl["rows"]["QQQ"]["m"])
check_true("...and says whose book it is", X["QQQX"]["same_book_as"] == "QQQ" and "same book as QQQ" in X["QQQX"]["lines_source"])
check("nport: 80% of the book is a covered US filer -> shown", (X["KXF"]["shown"], X["KXF"]["coverage_w"]), (True, 0.8))
check_true("...with a ratio to SPY's composite", (X["KXF"].get("vs_spy") or {}).get("pe") is not None, X["KXF"].get("vs_spy"))
check_true("...its own N-PORT book named as the source", "N-PORT" in X["KXF"]["lines_source"], X["KXF"]["lines_source"])
check_true("...and no own history, said plainly", "back-fill covers ladder rows only" in X["KXF"]["history"]["why"])
check("nport: three tenths covered -> not shown under the coverage rule, the reason printed", (X["THN"]["shown"], X["THN"]["why"][:13]), (False, "not shown: 30"))
check_true("nport with no cached book: withheld, says the book is not cached yet",
           X["NEW"]["shown"] is False and "cached yet" in X["NEW"]["why"], X["NEW"].get("why"))
check_true("same_book_as a row that is not on the ladder: withheld with the reason",
           X["GONE"]["shown"] is False and "NOTAROW" in X["GONE"]["why"])
check_true("an entry naming neither: withheld with the reason", "neither" in X["ODD"]["why"])
check_true("every extra is flagged extra", all(r.get("extra") for r in X.values()))
check("n_extra", pl["n_extra"], 6)

print("\nthe company store: extras' companies are wanted like a row's")
st = {"companies": {}}
fetched = []
real = B.company_from_facts
B.company_from_facts = lambda cf, t, today: dict(STORE["companies"][cf["cik"]], fetched=today)
MEM_ONLY_SPY = {"asof": TODAY, "rows": {"SPY": {"tier": "issuer_full", "holdings": [{"t": "BBB", "w": 1.0}]}}}
B.build(MEM_ONLY_SPY, DEEP, CM, st, {}, LAST, None, TODAY, fetch=lambda cik: (fetched.append(cik), {"cik": cik})[1],
        log=lambda *a: None, extras={"KXF": {"nport": True}})
B.company_from_facts = real
check("a company held only by an extra fund is fetched", sorted(fetched), ["0000000001", "0000000002"])

print("\nno extras file, and a broken one (loud, never silent)")
pl0, _, _ = B.build(MEM, DEEP, CM, json.loads(json.dumps(STORE)), {}, LAST, None, TODAY, log=lambda *a: None)
check("no extras -> extra_rows empty, rows unchanged", (pl0["extra_rows"], sorted(pl0["rows"])), ({}, ["QQQ", "SPY"]))
d = tempfile.mkdtemp()
check("read_extras: an absent file -> {}", B.read_extras(os.path.join(d, "none.json")), {})
bad = os.path.join(d, "bad.json")
with open(bad, "w", encoding="utf-8") as fh:
    fh.write("{not json")
said = []
ex = B.read_extras(bad, log=said.append)
check_true("read_extras: an unreadable file is reported in the log", said and "[warn]" in said[0], said)
plB, _, _ = B.build(MEM, DEEP, CM, json.loads(json.dumps(STORE)), {}, LAST, None, TODAY, log=lambda *a: None, extras=ex)
check_true("...and in the payload's error line", "bad.json unreadable" in (plB["error"] or ""), plB["error"])
good = os.path.join(d, "good.json")
with open(good, "w", encoding="utf-8") as fh:
    json.dump({"funds": {"KXF": {"nport": True}}}, fh)
check("read_extras: the funds map", B.read_extras(good), {"KXF": {"nport": True}})

print("\nrotation_members: the N-PORT list gains the nport extras")
check("extra_nport: nport entries not already listed, sorted; same_book_as and errors skipped",
      RM.extra_nport({"VGT": {"nport": True}, "KXF": {"nport": True}, "QQQM": {"same_book_as": "QQQ"},
                      "IGV": {"nport": True}, "__error__": "x"}, ["IGV", "ARTY"]), ["KXF", "VGT"])
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "extra_funds.json"), encoding="utf-8") as fh:
    shipped = json.load(fh)["funds"]
check_true("the shipped extra_funds.json parses; every entry names same_book_as or nport",
           shipped and all(("same_book_as" in e) != bool(e.get("nport")) for e in shipped.values()), shipped)
src = open(os.path.join(here, "rotation_members.py"), encoding="utf-8").read()
check_true("rotation_members.main adds the extras to the weekly N-PORT list",
           "deep_rows += extra_nport(basket_fundamentals.read_extras(), deep_rows)" in src)

print("-" * 60)
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
