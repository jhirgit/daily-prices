#!/usr/bin/env python3
"""Offline tests for rotation_corr.py: the numbers against cases with a known
answer, the payload's shape, and the rules about what is null rather than a number.

    python test_rotation_corr.py
"""
import json
import math
import random
import sys

import rotation_corr as RC

fails = 0
count = 0


def ok(name, cond, detail=""):
    global fails, count
    count += 1
    print("  %s  %s%s" % ("PASS" if cond else "FAIL", name, "" if cond or not detail else "  (%s)" % (detail,)))
    if not cond:
        fails += 1


def path(rets, start=100.0):
    out = [start]
    for r in rets:
        out.append(out[-1] * (1 + r))
    return out


random.seed(7)
N = 900
base = [random.gauss(0, 0.01) for _ in range(N)]
noise = [random.gauss(0, 0.01) for _ in range(N)]
other = [random.gauss(0, 0.01) for _ in range(N)]
dates = ["d%04d" % i for i in range(N + 1)]
series = {
    "A": path(base),
    "B": path([2 * x for x in base]),                    # same direction, twice the size
    "C": path([-x for x in base]),                       # the mirror image
    "D": path(other),                                    # unrelated
    "E": path([x + y for x, y in zip(base, noise)]),     # half its variance is A's
    "T": path([0.5 * x for x in base]),                  # A's twin
    "S": [None] * (N + 1 - 40) + path(base[-39:]),       # 40 bars of history only
    "BM": path([x for x in base]),                       # a benchmark identical to A
}
etfs = [{"t": "A", "name": "Alpha", "side": "offense", "sector": "One", "grp": "g"},
        {"t": "T", "name": "Alpha twin", "side": "offense", "sector": "One", "grp": "g"},
        {"t": "B", "name": "Beta", "side": None, "sector": "One"},
        {"t": "C", "name": "Gamma", "side": "defense", "sector": "Two"},
        {"t": "D", "name": "Delta", "side": None, "sector": "Two"},
        {"t": "E", "name": "Epsilon", "side": None, "sector": "Two"},
        {"t": "S", "name": "Short", "side": None, "sector": "Three"},
        {"t": "PAIR", "name": "Pair (basket)", "side": None, "sector": "Three", "basket": ["A", "D"]},
        {"t": "GONE", "name": "Missing", "side": None, "sector": "Three"}]
bench = [("BM", "Benchmark"), ("NOPE", "Not there"), ("A", "already a row")]
out = RC.build(dates, series, etfs=etfs, bench=bench, roll_bench=["A", "BM", "NOPE"])
T = out["tickers"]


def corr(w, a, b):
    i, j = T.index(a), T.index(b)
    if i == j:
        return 1.0
    if i < j:
        i, j = j, i
    return out["corr"][str(w)][i][j]


def vol(w, a):
    return out["vol"][str(w)][T.index(a)]


print("the numbers")
ok("a series that is twice another has correlation 1", corr(63, "A", "B") == 1.0 and corr(252, "A", "B") == 1.0)
ok("its mirror image has correlation -1", corr(63, "A", "C") == -1.0)
ok("an unrelated series is near zero over a year", abs(corr(252, "A", "D")) < 0.2, corr(252, "A", "D"))
ok("half-shared variance reads near 0.71 over a year", abs(corr(252, "A", "E") - 0.7071) < 0.08, corr(252, "A", "E"))
ok("twice the moves is twice the volatility", abs(vol(63, "B") / vol(63, "A") - 2) < 1e-3, (vol(63, "A"), vol(63, "B")))
import statistics
want = statistics.stdev(base[-63:]) * math.sqrt(252)
ok("volatility is the annualised stdev of the window's daily returns", abs(vol(63, "A") - want) < 1e-4, (vol(63, "A"), want))
rb = out["rows"]["B"]["bench"]["BM"]["63"]
ok("beta to a benchmark identical to A is 2 for B, with r2 of 1", rb == {"corr": 1.0, "beta": 2.0, "r2": 1.0}, rb)
rc = out["rows"]["C"]["bench"]["BM"]["252"]
ok("...and -1 for the mirror image, r2 still 1", rc["beta"] == -1.0 and rc["r2"] == 1.0, rc)
re_ = out["rows"]["E"]["bench"]["BM"]["252"]
ok("r2 is the square of the correlation", abs(re_["r2"] - re_["corr"] ** 2) < 0.011, re_)
ok("the equal-weight basket of A and D correlates with both", corr(252, "PAIR", "A") > 0.5 and corr(252, "PAIR", "D") > 0.5,
   (corr(252, "PAIR", "A"), corr(252, "PAIR", "D")))

print("what is null rather than a number")
ok("40 bars is not a 63-session correlation", corr(63, "S", "A") is None and vol(63, "S") is None)
ok("a row with no series is an error entry, not a row", "GONE" not in T and "GONE" in out["errors"])
ok("a benchmark prices.db lacks is an error entry", "NOPE" not in T and "NOPE" in out["errors"])
ok("a benchmark that is already a ladder row is not added twice", T.count("A") == 1 and out["bench"] == ["BM"])
ok("the payload has no NaN or Infinity anywhere", "NaN" not in json.dumps(out) and "Infinity" not in json.dumps(out))
try:
    json.dumps(out, allow_nan=False)
    strict = True
except ValueError:
    strict = False
ok("...and serialises under allow_nan=False", strict)

print("the shape")
ok("tickers = the sleeves in ladder-config order, then the benchmarks", T == ["A", "T", "B", "C", "D", "E", "S", "PAIR", "BM"], T)
ok("the matrix is a lower triangle: row i holds i values", all(len(r) == i for i, r in enumerate(out["corr"]["63"])) and len(out["corr"]["63"]) == len(T))
ok("one volatility per ticker per window", all(len(out["vol"][str(w)]) == len(T) for w in out["windows"]))
ok("the twin is marked and held out of the field", out["meta"]["T"]["twin_of"] == "A" and "T" not in out["field"] and "A" in out["field"])
ok("every sleeve has a row; no benchmark does", set(out["rows"]) == set(T) - {"BM"})
hiB = out["rows"]["B"]["hi"]
ok("the 'moves with' list is sorted high to low and never names the row itself", [x[1] for x in hiB] == sorted([x[1] for x in hiB], reverse=True)
   and all(x[0] != "B" for x in hiB), hiB)
ok("...and never a twin (one sleeve cannot appear twice)", all(x[0] != "T" for r in out["rows"].values() for x in r["hi"] + r["lo"]))
ok("a row's own twin/primary is not in its lists", all(x[0] != "A" for x in out["rows"]["T"]["hi"] + out["rows"]["T"]["lo"]))
ok("the 'moves apart' list starts at the most negative", out["rows"]["A"]["lo"][0][0] == "C" and out["rows"]["A"]["lo"][0][1] == -1.0, out["rows"]["A"]["lo"])
ok("each list entry is [ticker, short window, long window]", all(len(x) == 3 for x in hiB))

print("the history")
R = out["roll"]
ok("one point every STEP sessions, ending on the last session", R["dates"][-1] == dates[-1] and len(R["dates"]) == len(R["avg"]) == len(R["pc1"]) == len(R["n"])
   and R["dates"][-2] == dates[-1 - RC.STEP])
ok("no more than HIST sessions back", dates.index(R["dates"][0]) >= len(dates) - 1 - RC.HIST)
ok("only benchmarks that exist are rolled", R["bench"] == ["A", "BM"], R["bench"])
ok("every sleeve carries one value per point per rolled benchmark and for the field",
   all(len(r["roll"][k]) == len(R["dates"]) for r in out["rows"].values() for k in R["bench"] + ["field"]))
ok("a sleeve's history against itself is null", all(v is None for v in out["rows"]["A"]["roll"]["A"]))
ok("B against A is 1 at every point", all(v == 1.0 for v in out["rows"]["B"]["roll"]["A"]))
ok("the short-history sleeve is null until it has the window", out["rows"]["S"]["roll"]["A"][-1] is None)
ok("the field's mean pairwise correlation is between -1 and 1", all(v is None or -1 <= v <= 1 for v in R["avg"]) and R["avg"][-1] is not None)
ok("the first component's share is between 1/n and 1", all(v is None or 0 < v <= 1 for v in R["pc1"]) and R["pc1"][-1] is not None, R["pc1"][-3:])
# a field that all moves as one: pc1 share is 1
one = {k: path([(i + 1) * x for x in base]) for i, k in enumerate("PQRS")}
o2 = RC.build(dates, one, etfs=[{"t": k, "name": k, "side": None} for k in "PQRS"], bench=[], roll_bench=["P"])
ok("a field that moves as one puts all its variance on the first component", o2["roll"]["pc1"][-1] == 1.0 and o2["roll"]["avg"][-1] == 1.0, (o2["roll"]["pc1"][-1], o2["roll"]["avg"][-1]))
# the field's history is blanked where most of today's field was not there yet
late = dict(one)
for k in "QRS":
    late[k] = [None] * 500 + late[k][500:]
o3 = RC.build(dates, late, etfs=[{"t": k, "name": k, "side": None} for k in "PQRS"], bench=[], roll_bench=["P"])
i0 = next(i for i, d in enumerate(o3["roll"]["dates"]) if dates.index(d) < 500)
ok("a point from before most of today's field existed is null, not a mean over the few",
   o3["roll"]["avg"][i0] is None and o3["roll"]["pc1"][i0] is None and o3["rows"]["P"]["roll"]["field"][i0] is None and o3["roll"]["avg"][-1] == 1.0)

print("the real configuration")
import regime as RG
ok("every rolled benchmark is a ladder row or a listed benchmark",
   all(b in {e["t"] for e in RG.REG_ETFS} | {t for t, _ in RC.BENCH} for b in RC.ROLL_BENCH))
ok("the method note says display only", "Display only" in out["method"])

print("\n%d passed, %d failed" % (count - fails, fails))
sys.exit(1 if fails else 0)
