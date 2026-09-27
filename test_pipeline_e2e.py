#!/usr/bin/env python3
"""
End-to-end payload contract (review H3 / resilience F3, 2026-09-26).

Builds a temp prices.db (SPY, a live name, a name whose bars stop two sessions
early, 300 sessions each), runs the real emitters on it -- export_data.main,
closes.build, crowd.build -- and asserts the keys the board reads: every
payload's generated_at, latest.json's session / lagging / per-row stale and
per-ticker date / close / dma200, closes.json's axis, crowd.json's proxies.

The one check between these scripts and the board's PAYLOAD_KEY map and Brief
pipeline rows. Offline; nothing is written outside a temp dir.

Run:  python test_pipeline_e2e.py
"""

import datetime as dt
import json
import os
import shutil
import sqlite3
import sys
import tempfile

import closes as CL
import crowd as CR
import export_data as EX
import fetch_prices as FP

FAILS = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}" + ("" if ok else f", want {want!r}"))
    if not ok:
        FAILS.append(name)


TMP = tempfile.mkdtemp(prefix="pipeline_e2e_")
DB = os.path.join(TMP, "prices.db")
OUT = os.path.join(TMP, "data")
TICK = os.path.join(TMP, "tickers.txt")

days, d = [], dt.date(2025, 6, 2)
while len(days) < 300:
    if d.weekday() < 5:
        days.append(d.isoformat())
    d += dt.timedelta(days=1)
conn = sqlite3.connect(DB)
FP.init_db(conn)
for tk, last in (("SPY", days[-1]), ("LIVE", days[-1]), ("LAGGY", days[-3])):
    for i, day in enumerate(days):
        if day > last:
            break
        px = 100.0 + i * 0.1
        conn.execute("INSERT INTO daily_prices VALUES (?,?,?,?,?,?,?,?, 'test', 'x')",
                     (tk, day, px, px * 1.01, px * 0.99, px, px * 0.99, 1_000_000))
    conn.execute("INSERT INTO spot_quotes VALUES (?, ?, ?, ?, 'USD', 'test')",
                 (tk, days[-1] + "T21:00:00Z", 130.0, 129.0))
conn.commit()
conn.close()
with open(TICK, "w", encoding="utf-8") as fh:
    fh.write("SPY\nLIVE\nLAGGY\n")

argv = sys.argv
sys.argv = ["export_data.py", "--db", DB, "--out", OUT]
try:
    rc = EX.main()
finally:
    sys.argv = argv

print("\n# latest.json")
check("export_data exits 0", rc, 0)
L = json.load(open(os.path.join(OUT, "latest.json"), encoding="utf-8"))
check("top-level keys the board reads are present",
      [k for k in ("generated_at", "count", "tickers", "session", "lagging") if k not in L], [])
rows = {r["ticker"]: r for r in L["tickers"]}
check("session is SPY's last bar", L["session"], days[-1])
check("lagging lists exactly the name behind the session", L["lagging"],
      [{"ticker": "LAGGY", "date": days[-3]}])
check("the lagging row is flagged stale", rows["LAGGY"].get("stale"), True)
check("current rows carry no stale key (additive field)", "stale" in rows["LIVE"], False)
check("per-ticker fields the board reads",
      [k for k in ("ticker", "date", "close", "dma200", "adj_close", "spot_price") if k not in rows["LIVE"]], [])
check("dma200 is the 200-bar mean of closes",
      rows["LIVE"]["dma200"], round(sum(100.0 + i * 0.1 for i in range(100, 300)) / 200, 4))
check("the other exports were written",
      sorted(f for f in os.listdir(OUT)), ["daily_prices.csv.gz", "latest.json", "spot_quotes.csv", "technicals.json"])
T = json.load(open(os.path.join(OUT, "technicals.json"), encoding="utf-8"))
check("technicals.json carries generated_at and tickers",
      [k for k in ("generated_at", "tickers") if k not in T], [])

print("\n# closes.json")
C = CL.build(DB, TICK, n=50)
check("closes keys", [k for k in ("generated_at", "asof", "dates", "tickers", "n") if k not in C], [])
check("closes asof is the axis's last session", C["asof"], days[-1])
check("a lagging name is null past its last bar, never carried forward",
      C["tickers"]["LAGGY"][-2:], [None, None])
check("closes use adj_close", C["tickers"]["LIVE"][-1], round((100.0 + 299 * 0.1) * 0.99, 4))

print("\n# crowd.json")
R = CR.build(DB, TICK, os.path.join(TMP, "no-insiders.json"), today=dt.date.fromisoformat(days[-1]))
check("crowd keys", [k for k in ("generated_at", "method", "insider_window", "tickers") if k not in R], [])
check("crowd rows carry the proxies the board reads",
      sorted(R["tickers"]["LIVE"]), sorted(["asof", "relvol_20_60", "ext_50d", "ext_200d",
                                            "insider_net_usd_30d", "insider_net_30d"]))
check("crowd asof is the name's own last bar", R["tickers"]["LAGGY"]["asof"], days[-3])

shutil.rmtree(TMP, ignore_errors=True)
print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s): {', '.join(FAILS)}")
    sys.exit(1)
print("all tests passed")
