#!/usr/bin/env python3
"""
overnight.py's per-item bar_date (review M4, 2026-09-26), offline.

A fake yfinance Ticker stands in for Yahoo: fast_info gives the quote, a 5-day
history gives the bars. The cases are the ones the board has to tell apart:
a market that traded (bar dated today), a market shut for a holiday (it still
answers with its last session's move, so bar_date is that older session), a
history read that fails (bar_date None, quote kept), and the fallback that
repairs a missing previous close from the same history.

Run:  python test_overnight.py
"""

import math
import sys

import pandas as pd

import overnight as O

FAILS = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}" + ("" if ok else f", want {want!r}"))
    if not ok:
        FAILS.append(name)


class FakeFast:
    def __init__(self, last, prev, cur):
        self.last_price, self.previous_close, self.currency = last, prev, cur

    def __getitem__(self, k):
        return {"lastPrice": self.last_price}[k]


class FakeTicker:
    """Bars: [(date, close)] in exchange-local time; history=None makes it raise."""
    def __init__(self, last, prev, cur, bars, tz="Asia/Seoul"):
        self.fast_info = FakeFast(last, prev, cur)
        self._bars, self._tz = bars, tz

    def history(self, **_):
        if self._bars is None:
            raise RuntimeError("history unavailable")
        idx = pd.DatetimeIndex([pd.Timestamp(d) for d, _ in self._bars]).tz_localize(self._tz)
        return pd.DataFrame({"Close": [c for _, c in self._bars]}, index=idx)


CASES = {}
O.yf.Ticker = lambda sym: CASES[sym]

print("# a market that traded: bar_date is the session just finished")
CASES["^N225"] = FakeTicker(101.0, 100.0, "JPY",
                            [("2026-09-23", 99.0), ("2026-09-24", 100.0), ("2026-09-25", 101.0)],
                            tz="Asia/Tokyo")
last, prev, cur, bd = O.quote("^N225")
check("quote keeps fast_info's last / prev / currency", (last, prev, cur), (101.0, 100.0, "JPY"))
check("bar_date is the newest bar's exchange-local date", bd, "2026-09-25")

print("\n# a market shut for a holiday: the move is the last session's, dated then")
CASES["^KS11"] = FakeTicker(7080.92, 7017.91, "KRW",
                            [("2026-09-21", 7001.0), ("2026-09-22", 7017.91), ("2026-09-23", 7080.92),
                             ("2026-09-24", float("nan"))])
last, prev, cur, bd = O.quote("^KS11")
check("a NaN bar (no session) is not the bar date", bd, "2026-09-23")
check("pct inputs are unchanged by the date read", (last, prev), (7080.92, 7017.91))

print("\n# the exchange-local date, not UTC: a Tokyo close is late evening UTC the day before")
CASES["EARLY"] = FakeTicker(10.0, 9.0, "JPY", [("2026-09-25 00:00", 10.0)], tz="Asia/Tokyo")
check("bar_date reads the exchange's calendar day", O.quote("EARLY")[3], "2026-09-25")

print("\n# the history read fails: the quote survives, the date is unknown")
CASES["^HSI"] = FakeTicker(26000.0, 26100.0, "HKD", None, tz="Asia/Hong_Kong")
last, prev, cur, bd = O.quote("^HSI")
check("quote kept", (last, prev), (26000.0, 26100.0))
check("bar_date is None, never a guess", bd, None)

print("\n# fast_info lacks the previous close: the same history repairs it")
CASES["^TWII"] = FakeTicker(22000.0, None, "TWD",
                            [("2026-09-24", 21900.0), ("2026-09-25", 22000.0)], tz="Asia/Taipei")
last, prev, cur, bd = O.quote("^TWII")
check("prev repaired from the second-to-last bar", prev, 21900.0)
check("bar_date alongside the repair", bd, "2026-09-25")

print("\n# main() emits bar_date on every item (a symbol that never quotes stays null)")
written = {}


class Sink:
    def __init__(self, *_a, **_k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def write(self, s):
        written["body"] = written.get("body", "") + s


real_board, real_sleep, real_makedirs = O.BOARD, O.time.sleep, O.os.makedirs
O.BOARD = [("Asia (closed)", [("^KS11", "KOSPI"), ("NOPE", "never quotes")])]
CASES["NOPE"] = FakeTicker(None, None, None, [])
O.time.sleep = lambda *_: None          # the retry back-off
O.os.makedirs = lambda *_a, **_k: None
O.open = Sink                            # module-global shadow: nothing touches data/
try:
    rc = O.main()
finally:
    O.BOARD, O.time.sleep, O.os.makedirs = real_board, real_sleep, real_makedirs
    del O.open
import json  # noqa: E402

out = json.loads(written["body"])
items = out["groups"][0]["items"]
check("main exits 0 when one quote worked", rc, 0)
check("every item carries bar_date", [("bar_date" in i) for i in items], [True, True])
check("the quoted item is dated", items[0]["bar_date"], "2026-09-23")
check("the dead symbol's bar_date is null", items[1]["bar_date"], None)
check("pct is last vs previous close", items[0]["pct"], round((7080.92 / 7017.91 - 1) * 100, 3))
check("the note explains bar_date", "bar_date" in out["note"], True)

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s): {', '.join(FAILS)}")
    sys.exit(1)
print("all tests passed")
