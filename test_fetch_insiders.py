#!/usr/bin/env python3
"""
fetch_insiders.py: the universe and the status labels (review L2 / L3, 2026-09-26).

Offline. The universe is tickers.txt resolved against EDGAR's own files (no
hand-kept list), and every name gets one status the board can label:

    ok / 144        Form 4 P/S (or, for an FPI, Form 144 notices) in the window
    no-section16    a foreign private issuer: files 20-F/40-F/6-K (F-6 for an ADR)
    etf             a fund or trust: on EDGAR's fund file, or a filer with no
                    Form 4 at all that does not file as an FPI
    no-cik          nothing on EDGAR under that symbol (foreign listings, new ETFs)
    crypto          -USD pairs

Plus L3: state carried through a failed fetch is re-cut on this run's month axis.

Run:  python test_fetch_insiders.py
"""

import json
import os
import shutil
import sys
import tempfile

import fetch_insiders as FI

FAILS = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}" + ("" if ok else f", want {want!r}"))
    if not ok:
        FAILS.append(name)


print("# the universe is tickers.txt, minus symbols no issuer can stand behind")
tmp = tempfile.mkdtemp(prefix="ins_")
tick = os.path.join(tmp, "tickers.txt")
with open(tick, "w", encoding="utf-8") as fh:
    fh.write("# --- a group header ---\n"
             "nvda   # lower case is upper-cased\n"
             "EWY\n"
             "^GSPC  # index\n"
             "GC=F   # future\n"
             "KRW=X  # FX\n"
             "BTC-USD\n"
             "000660.KS\n"
             "NVDA   # duplicate\n"
             "\n")
check("indexes, futures, FX and duplicates drop; order kept",
      FI.load_tickers(tick), ["NVDA", "EWY", "BTC-USD", "000660.KS"])
check("the default universe file is tickers.txt, not a hand list",
      os.path.basename(FI.TICKERS_FILE), "tickers.txt")
check("no hand-kept SKIP map remains", hasattr(FI, "SKIP"), False)
live = FI.load_tickers()
check("the live universe carries no index / future / FX symbol",
      [t for t in live if t.startswith("^") or "=" in t], [])

print("\n# pre-fetch status: who is never fetched, and why")
ciks, funds = {"NVDA": "0001045810", "SPY": "0000884394"}, {"EWY", "SPY"}
check("a filer with a CIK is fetched", FI.pre_status("NVDA", ciks, funds), None)
check("a CIK wins over the fund file (a trust with a CIK is judged by its filings)",
      FI.pre_status("SPY", ciks, funds), None)
check("no CIK, on EDGAR's fund file -> etf", FI.pre_status("EWY", ciks, funds), "etf")
check("no CIK, not a fund -> no-cik", FI.pre_status("000660.KS", ciks, funds), "no-cik")
check("a -USD pair -> crypto", FI.pre_status("BTC-USD", ciks, funds), "crypto")

print("\n# final status after the filings are read")
check("Form-4 P/S present -> ok", FI.final_status(True, True, 3, True), "ok")
check("no P/S, 144s in the window -> 144", FI.final_status(False, True, 5, True), "144")
check("Form 4s but no P/S -> ok (a real quiet)", FI.final_status(False, False, 7, False), "ok")
check("no Form 4, files as an FPI -> no-section16", FI.final_status(False, False, 0, True), "no-section16")
check("no Form 4, not an FPI -> etf (a fund or trust)", FI.final_status(False, False, 0, False), "etf")

print("\n# list_forms: the FPI flag comes from the filer's forms, whatever their date")
SUBS = {
    "fpi": {"filings": {"recent": {
        "form": ["6-K", "20-F", "4", "144"],
        "filingDate": ["2026-09-01", "2025-03-01", "2026-08-01", "2026-07-01"],
        "accessionNumber": ["a1", "a2", "a3", "a4"],
        "primaryDocument": ["d1", "d2", "d3.xml", "d4.xml"]}, "files": []}},
    "us": {"filings": {"recent": {
        "form": ["10-Q", "4", "4/A", "144", "8-K"],
        "filingDate": ["2026-08-01", "2026-09-10", "2024-01-05", "2026-09-09", "2026-09-01"],
        "accessionNumber": ["b1", "b2", "b3", "b4", "b5"],
        "primaryDocument": ["x", "f.xml", "g.xml", "h.xml", "y"]}, "files": []}},
    "adr": {"filings": {"recent": {
        "form": ["F-6EF"], "filingDate": ["2019-05-01"],
        "accessionNumber": ["c1"], "primaryDocument": ["z"]}, "files": []}},
}
real_get = FI.get
FI.get = lambda url, retry=1: json.dumps(SUBS[url.rsplit("CIK", 1)[1].split(".")[0]]).encode()
try:
    f = FI.list_forms("fpi", "2025-06-01")
    check("an FPI stream is flagged", f["fpi"], True)
    check("its Form 4 and 144 are still listed", (len(f["f4"]), len(f["f144"])), (1, 1))
    f = FI.list_forms("us", "2025-06-01")
    check("a US filer is not flagged", f["fpi"], False)
    check("a Form 4 before since_iso drops", [x[0] for x in f["f4"]], ["b2"])
    check("an old F-6 still marks an ADR", FI.list_forms("adr", "2025-06-01")["fpi"], True)
finally:
    FI.get = real_get

print("\n# L3: a failed fetch carries the prior state re-cut on today's month axis")
old_axis = ["2025-09", "2025-10", "2025-11"]
new_axis = ["2025-10", "2025-11", "2025-12"]
txns = [{"d": "2025-09-15", "c": "S", "v": 900, "plan": False},
        {"d": "2025-10-03", "c": "P", "v": 100, "plan": False},
        {"d": "2025-11-20", "c": "S", "v": 400, "plan": True}]
state = {"status": "ok", "txns": txns, "months": FI.aggregate(txns, old_axis),
         "tot_b": 100, "tot_s": 1300}
got = FI.carried(state, new_axis)
check("buckets follow today's axis", [m["m"] for m in got["months"]], new_axis)
check("each month keeps its own flows",
      [(m["b"], m["s"], m["sp"]) for m in got["months"]], [(100, 0, 0), (0, 400, 400), (0, 0, 0)])
check("totals drop the month that left the axis", (got["tot_b"], got["tot_s"]), (100, 400))
check("the stored state itself is not mutated", state["months"][0]["m"], "2025-09")
s144 = {"status": "144", "txns": [], "txns144": [{"d": "2025-12-01", "c": "S144", "v": 50, "plan": False}]}
check("a 144-mode state re-cuts its 144 notices", FI.carried(s144, new_axis)["tot_s"], 50)
check("a non-data status is carried as is", FI.carried({"status": "no-section16"}, new_axis),
      {"status": "no-section16"})
check("no prior state -> error", FI.carried({}, new_axis), {"status": "error"})

shutil.rmtree(tmp, ignore_errors=True)
print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s): {', '.join(FAILS)}")
    sys.exit(1)
print("all tests passed")
