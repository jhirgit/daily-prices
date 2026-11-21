#!/usr/bin/env python3
"""
Tests for constituents_store.py (B1 stage 3, 2026-11-20). Offline.

Under test: a cold store fetches everything in full (a cache miss rebuilds);
a warm one asks for the tail only and appends; a dividend/split re-base is
reconciled by one ratio and an inconsistent overlap is refetched in full; a
symbol Yahoo fails today is served from the store; trimming; a corrupt file is
a miss, never a crash; the status block (hit / miss_rebuilt / partial, the
size warning).

Run:  python test_constituents_store.py
"""

import datetime as dt
import gzip
import os
import sys
import tempfile

import constituents_store as CS

PASSED, FAILS = [], []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}" + ("" if ok else f", want {want!r}"))
    (PASSED if ok else FAILS).append(name)


def check_close(name, got, want, tol=1e-9):
    ok = got is not None and want is not None and abs(got - want) <= tol
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r} (tol {tol})")
    (PASSED if ok else FAILS).append(name)


def days(n, end="2026-11-20"):
    """n weekdays ending at `end`."""
    out, d = [], dt.date.fromisoformat(end)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= dt.timedelta(days=1)
    return out[::-1]


D = days(600)
TODAY = D[-1]
START = D[-460]
SER = {s: [100.0 + i * (0.1 + j / 10) for i in range(len(D))] for j, s in enumerate(("AAA", "BBB", "CCC.TO"))}
CALLS = []


def yahoo(symbols, start, budget_s=None, fail=(), scale=None):
    """A fake fetch_history: every symbol from `start`, closes optionally re-based."""
    CALLS.append((list(symbols), start))
    out = {}
    for s in symbols:
        if s in fail or s not in SER:
            continue
        ds = [d for d in D if d >= start]
        k = (scale or {}).get(s, 1.0)
        cs = [SER[s][D.index(d)] * k for d in ds]
        out[s] = (ds, cs, "CAD" if s.endswith(".TO") else "USD")
    return out


print("a cold store: everything in full, the store fills")
store = {"symbols": {}}
h, st = CS.stored_history(yahoo, ["AAA", "BBB", "CCC.TO"], START, 300, store, TODAY)
check("one full request round, from the window start", [(sorted(s), a) for s, a in CALLS], [(["AAA", "BBB", "CCC.TO"], START)])
check("all three new", (st["new"], st["appended"], st["failed"]), (3, 0, 0))
check("the answer is fetch_history's shape", (h["AAA"][0][0], h["AAA"][0][-1], h["CCC.TO"][2]), (START, TODAY, "CAD"))

print("\na warm store: the tail only, appended")
CALLS.clear()
D.append("2026-11-23")
for s in SER:
    SER[s].append(SER[s][-1] * 1.01)
T2 = "2026-11-23"
h, st = CS.stored_history(yahoo, ["AAA", "BBB"], START, 300, store, T2)
check("one request round, for the last two weeks only", [(sorted(s), a) for s, a in CALLS],
      [(["AAA", "BBB"], "2026-11-09")])
check("both appended", (st["appended"], st["new"], st["rebased"]), (2, 0, 0))
check("the new bar is on the end, the history intact", (store["symbols"]["AAA"]["d"][-1], len(h["AAA"][0]) >= 460),
      (T2, True))
check_close("...and the stored history is unchanged", store["symbols"]["AAA"]["c"][-2], SER["AAA"][-2])

print("\na dividend re-base: one ratio over the overlap, applied to the stored history")
CALLS.clear()
h, st = CS.stored_history(lambda s, a, b=None: yahoo(s, a, b, scale={"AAA": 0.99}), ["AAA"], START, 300, store, T2)
check("re-based, not refetched", (st["rebased"], st["refetched"], len(CALLS)), (1, 0, 1))
check_close("an old close is now scaled by the same 0.99", store["symbols"]["AAA"]["c"][-100], SER["AAA"][-100] * 0.99, 1e-9)
r_store = store["symbols"]["AAA"]["c"][-1] / store["symbols"]["AAA"]["c"][-253] - 1
r_true = SER["AAA"][-1] / SER["AAA"][-253] - 1
check_close("...so a one-year return is the same as a fresh full download's", r_store, r_true, 1e-12)

print("\nan overlap that does not reconcile: refetched in full")
m, how = CS.merge({"ccy": "USD", "d": ["2026-11-18", "2026-11-19", "2026-11-20"], "c": [10.0, 10.0, 10.0]},
                  ["2026-11-19", "2026-11-20", "2026-11-23"], [10.0, 11.0, 11.5], "USD")
check("ratios 1.00 and 1.10 on the overlap: refetch", (m, how), (None, "refetch"))
check("no overlap at all: refetch", CS.merge({"d": ["2026-01-02"], "c": [1.0]}, ["2026-11-20"], [2.0], "USD")[1], "refetch")
CALLS.clear()
bad = dict(store["symbols"]["BBB"])
bad["c"] = bad["c"][:-3] + [x * 1.5 for x in bad["c"][-3:]]      # a corrupted tail
store["symbols"]["BBB"] = bad
h, st = CS.stored_history(yahoo, ["BBB"], START, 300, store, T2)
check("the corrupted tail sends BBB back for a full request", (st["refetched"], [a for _, a in CALLS]),
      (1, ["2026-11-09", START]))
check_close("...and the store now holds the clean series", store["symbols"]["BBB"]["c"][-2], SER["BBB"][-2])

print("\nYahoo fails a symbol today")
CALLS.clear()
h, st = CS.stored_history(lambda s, a, b=None: yahoo(s, a, b, fail={"AAA"}), ["AAA", "ZZZ"], START, 300, store, T2)
check("a stored symbol is served from the store; a new one with no answer is a failure",
      (st["served_from_store"], st["failed"], "AAA" in h, "ZZZ" in h), (1, 1, True, False))

print("\nstale and short series, trimming")
store["symbols"]["OLD"] = {"ccy": "USD", "d": D[-500:-20], "c": [1.0] * 480}
CALLS.clear()
SER["OLD"] = [1.0] * len(D)
CS.stored_history(yahoo, ["OLD"], START, 300, store, T2)
check("a series whose last bar is 20 sessions old is fetched in full", [a for _, a in CALLS], [START])
big = {"ccy": "USD", "d": D[-700:] if len(D) >= 700 else D, "c": [1.0] * min(700, len(D))}
check("KEEP bars at most", len(CS._trim(dict(big))["d"]), min(CS.KEEP, len(big["d"])))

young = {"ccy": "USD", "d": D[-100:-1], "c": [1.0] * 99, "s0": START}
check("a listing younger than the window, fetched in full from the window start, counts as complete",
      CS.fresh(young, START, T2), True)
check("...without that record it would be refetched every night", CS.fresh(dict(young, s0=None), START, T2), False)

print("\nfiles and status")
tmp = tempfile.mkdtemp(prefix="cs_")
p = os.path.join(tmp, "c", "closes.json.gz")
check("no file: a miss, an empty store", CS.load(p), ({"symbols": {}}, "miss"))
size = CS.save(store, p)
back, state = CS.load(p)
check("round trip", (state, sorted(back["symbols"]), back["symbols"]["AAA"]["d"][-1]),
      ("hit", sorted(store["symbols"]), T2))
with open(p, "wb") as fh:
    fh.write(b"not gzip at all")
check("a corrupt file is a miss, never a crash", CS.load(p)[1], "miss")
with gzip.open(p, "wt", encoding="utf-8") as fh:
    fh.write('{"rows": []}')
check("a file of the wrong shape is a miss", CS.load(p)[1], "miss")
check("status: hit", CS.status({"wanted": 10, "failed": 1}, "hit", 1000)["store"], "hit")
check("status: a miss that rebuilt", CS.status({"wanted": 10, "failed": 0}, "miss", 1000)["store"], "miss_rebuilt")
check("status: partial past 20% with no price", CS.status({"wanted": 10, "failed": 3}, "hit", 1000)["store"], "partial")
check("status: a size warning past 25 MB", "warning" in CS.status({}, "hit", 26 * 1024 * 1024), True)

print("\n" + "-" * 60)
print(f"{len(PASSED)} passed, {len(FAILS)} failed")
if FAILS:
    print("FAILED: " + ", ".join(FAILS))
    sys.exit(1)
