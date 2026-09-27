#!/usr/bin/env python3
"""
Offline tests for crowd.py (review H3/M3, 2026-09-26). Replaces the old
`crowd.py --verify` parity fixture, which read the LIVE insider file and so
failed whenever a late Form 4 landed.

A temp prices.db with hand-computable bars and an inline insiders file: every
expected number below can be checked with a pencil.

Run:  python test_crowd.py
"""

import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile

import crowd as C

FAILS = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}" + ("" if ok else f", want {want!r}"))
    if not ok:
        FAILS.append(name)


def sessions(n, start=dt.date(2025, 9, 1)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


TMP = tempfile.mkdtemp(prefix="crowd_test_")
DB = os.path.join(TMP, "prices.db")
DAYS = sessions(210)
conn = sqlite3.connect(DB)
conn.execute("CREATE TABLE daily_prices (ticker TEXT, date TEXT, close REAL, volume INTEGER)")
for i, d in enumerate(DAYS):
    # LONG: close = 100 for 209 sessions then 110; volume 1000, the last 20 at 2000.
    conn.execute("INSERT INTO daily_prices VALUES ('LONG', ?, ?, ?)",
                 (d, 110.0 if i == len(DAYS) - 1 else 100.0, 2000 if i >= len(DAYS) - 20 else 1000))
for d in DAYS[-55:]:
    # SHORT: 55 bars -- enough for SMA50, not for SMA200 or relvol's 60.
    conn.execute("INSERT INTO daily_prices VALUES ('SHORT', ?, 50.0, 500)", (d,))
conn.commit()
conn.close()

TICK = os.path.join(TMP, "tickers.txt")
with open(TICK, "w", encoding="utf-8") as fh:
    fh.write("# header\nLONG\nSHORT   # inline comment\nNOBARS\n")

TODAY = dt.date(2026, 6, 30)
INS = os.path.join(TMP, "insiders.json")
with open(INS, "w", encoding="utf-8") as fh:
    json.dump({"generated_at": "2026-06-27T13:00:00Z", "tickers": {
        "LONG": {"status": "ok", "txns": [
            {"d": "2026-05-30", "c": "S", "v": 999_999},   # 31 days back: outside
            {"d": "2026-05-31", "c": "S", "v": 5_000},     # 30 days back: inside
            {"d": "2026-06-15", "c": "P", "v": 1_500},     # a buy nets against it
            {"d": "2026-06-30", "c": "S", "v": 250},       # today: inside
            {"d": "2026-07-01", "c": "S", "v": 777},       # after today: outside
        ]},
        "SHORT": {"status": "144", "txns": [], "txns144": [{"d": "2026-06-20", "c": "S144", "v": 10}]},
    }}, fh)

print("\n# positioning proxies")
out = C.build(DB, TICK, INS, today=TODAY)
L, S = out["tickers"]["LONG"], out["tickers"]["SHORT"]
check("asof is the last session", L["asof"], DAYS[-1])
check("relvol_20_60 = 2000 / ((40*1000 + 20*2000)/60)", L["relvol_20_60"], round(2000 / (80000 / 60), 4))
check("ext_50d = 110 / ((49*100+110)/50) - 1", L["ext_50d"], round(110 / (5010 / 50) - 1, 4))
check("ext_200d = 110 / ((199*100+110)/200) - 1", L["ext_200d"], round(110 / (20010 / 200) - 1, 4))
check("short history: relvol is null, not 0", S["relvol_20_60"], None)
check("short history: ext_200d is null, not 0", S["ext_200d"], None)
check("... but 55 bars do make an ext_50d", S["ext_50d"], 0.0)
check("a ticker with no bars is left out", "NOBARS" in out["tickers"], False)
check("the asof cutoff truncates the series",
      C.build(DB, TICK, INS, today=TODAY, asof=DAYS[-2])["tickers"]["LONG"]["ext_50d"], 0.0)

print("\n# insider net: dollars, a true 30-day window")
check("sells minus buys, 30 days to today inclusive", L["insider_net_usd_30d"], 5_000 - 1_500 + 250)
check("the old key carries the same number", L["insider_net_30d"], L["insider_net_usd_30d"])
check("Form 144 notices are not executions: null", S["insider_net_usd_30d"], None)
check("a name the cron does not cover: null", C.insider_net_usd_30d(None, TODAY), None)
check("covered and quiet: 0, not null",
      C.insider_net_usd_30d({"status": "ok", "txns": []}, TODAY), 0)
check("net buying is negative",
      C.insider_net_usd_30d({"status": "ok", "txns": [{"d": "2026-06-01", "c": "P", "v": 40}]}, TODAY), -40)
w = out["insider_window"]
check("the window is stated in the payload", (w["from"], w["to"], w["unit"]), ("2026-05-31", "2026-06-30", "USD"))
check("... with the insider file's own stamp", w["insiders_generated_at"], "2026-06-27T13:00:00Z")
check_method = "DOLLARS" in out["method"] and "shares" not in out["method"]
check("the method string says dollars, never shares", check_method, True)
check("a missing insiders file leaves every insider field null",
      C.build(DB, TICK, os.path.join(TMP, "absent.json"), today=TODAY)["tickers"]["LONG"]["insider_net_usd_30d"],
      None)

import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s): {', '.join(FAILS)}")
    sys.exit(1)
print("all tests passed")
