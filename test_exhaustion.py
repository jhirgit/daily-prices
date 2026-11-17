#!/usr/bin/env python3
"""Offline tests for exhaustion.py: every indicator against a hand-worked case,
the de-clustering, the expanding percentile, the proof that a flag on a date
does not move when later data is appended, null on a thin universe, and a
byte-identical rerun.

    python test_exhaustion.py
"""
import json
import math
import os
import random
import sys
import tempfile

import exhaustion as EX

fails = 0
count = 0


def ok(name, cond, detail=""):
    global fails, count
    count += 1
    print("  %s  %s%s" % ("PASS" if cond else "FAIL", name, "" if cond or not detail else "  (%s)" % (detail,)))
    if not cond:
        fails += 1


def close(a, b, tol=1e-9):
    return a is not None and b is not None and abs(a - b) <= tol


def bars_from(closes, vols, highs=None, lows=None):
    n = len(closes)
    return {"c": list(closes), "rc": list(closes),
            "h": list(highs) if highs else [None if c is None else c * 1.01 for c in closes],
            "l": list(lows) if lows else [None if c is None else c * 0.99 for c in closes],
            "v": list(vols)}


print("RSI (Wilder)")
ch = [1, -1] * 7                       # 7 gains of 1, 7 losses of 1
cl = [100.0]
for d in ch + [2]:
    cl.append(cl[-1] + d)
r = EX.wilder_rsi(cl, 14)
ok("None before 14 changes", all(x is None for x in r[:14]))
ok("equal average gain and loss -> 50", close(r[14], 50.0), r[14])
# avg gain (0.5*13+2)/14 = 8.5/14, avg loss 6.5/14 -> RSI = 100*8.5/15
ok("one more +2 change -> 56.667 (hand-worked)", close(r[15], 100 * 8.5 / 15), r[15])
ok("only losses -> 0", EX.wilder_rsi([10 - k for k in range(16)], 14)[14] == 0.0)

print("the hand-worked universe day")
# 20 members flat at 10 with volume 100 for 60 sessions; on session 60, members
# 0-14 fall to 9 on volume 200, members 15-19 rise to 11 on volume 300.
N = 61
axis = ["d%03d" % i for i in range(N)]
bars, mem = {}, []
for k in range(20):
    c = [10.0] * 60 + [9.0 if k < 15 else 11.0]
    v = [100] * 60 + [200 if k < 15 else 300]
    t = "M%02d" % k
    bars[t] = bars_from(c, v)
    mem.append(t)
bars["PX"] = bars_from([100.0] * 60 + [97.0], [1000] * 61,
                       highs=[101.0] * 60 + [100.0], lows=[99.0] * 60 + [96.0])
S = EX.compute_universe(axis, bars, mem, "PX", 15)
i = N - 1
ok("advancers/(adv+dec) = 5/20", close(S["adr"][i], 0.25), S["adr"][i])
ok("down-volume share = 15*9*200 / (15*9*200 + 5*11*300)", close(S["down_vol"][i], 27000 / 43500), S["down_vol"][i])
ok("share above the 20-day = 5/20", close(S["above_20d"][i], 0.25), S["above_20d"][i])
ok("share above the 50-day = 5/20", close(S["above_50d"][i], 0.25), S["above_50d"][i])
ok("200-day not yet defined (60 bars) -> None", S["above_200d"][i] is None)
ok("share at a 20-day closing low = 15/20", close(S["low_20d"][i], 0.75), S["low_20d"][i])
ok("relative volume on a down day: median of fifteen 2.0s and five 3.0s = 2.0", close(S["vol_surge"][i], 2.0), S["vol_surge"][i])
ok("relative volume is None on a flat (not down) day", S["vol_surge"][i - 1] is None)
d50 = 9 / ((49 * 10 + 9) / 50.0) - 1
ok("median distance from the 50-day = 9/9.98 - 1", close(S["dist_50d"][i], d50), (S["dist_50d"][i], d50))
ok("median RSI: fifteen 0s and five 100s -> 0", close(S["rsi14"][i], 0.0), S["rsi14"][i])
ok("proxy close location (97-96)/(100-96) = 0.25", close(S["close_loc"][i], 0.25), S["close_loc"][i])
ok("equal-weight return = (15*-0.1 + 5*0.1)/20 = -0.05", close(S["ew_ret"][i], -0.05), S["ew_ret"][i])
ok("one 90% day needs the share >= 0.90; this is 0.62 -> count 0", S["down90_10d"][i] == 0)
ok("RSI rule on (median 0 <= 30)", S["flag"]["rsi14"][i] is True)
ok("20-day rule off (0.25 > 0.15)", S["flag"]["above_20d"][i] is False)
ok("20-day-low rule on (0.75 >= 0.40)", S["flag"]["low_20d"][i] is True)
ok("close-location rule off: proxy -3% but in the bottom quarter, not the bottom tenth", S["flag"]["close_loc"][i] is False)
ok("percentile rules are None before 252 observations", S["flag"]["dist_50d"][i] is None and S["pct"]["dist_50d"][i][0] is None)
ok("capitulation_count counts the rules that are on", S["cap"][i] == sum(1 for k in EX.CAP_KEYS if S["flag"][k][i]))

print("null on a thin universe")
S2 = EX.compute_universe(axis, bars, mem[:10], "PX", 15)
ok("10 members under a floor of 15 -> every reading None",
   all(S2[k][i] is None for k in ("above_20d", "low_20d", "down_vol", "rsi14", "dist_50d", "ew_ret", "close_loc")))
ok("...and no rule is on", S2["cap"][i] == 0 and S2["cap_eval"][i] == 0)
S3 = EX.compute_universe(axis, bars, mem[:10], "PX", 8)
ok("the same 10 under the ETF-set floor of 8 -> readings", S3["above_20d"][i] is not None)
late = dict(bars)
for t in mem[:12]:
    late[t] = bars_from([None] * 40 + [10.0] * 20 + [9.0], [None] * 40 + [100] * 21)
S4 = EX.compute_universe(axis, late, mem, "PX", 8)
ok("fewer than half the member list live -> null (8 of 20 before session 40)", S4["ew_ret"][30] is None and S4["ew_ret"][45] is not None)

print("close location rule")
bars5 = dict(bars)
bars5["PX"] = bars_from([100.0] * 60 + [97.0], [1000] * 61, highs=[101.0] * 60 + [100.0], lows=[99.0] * 60 + [96.9])
S5 = EX.compute_universe(axis, bars5, mem, "PX", 15)
ok("bottom tenth on a -3% day -> on", S5["flag"]["close_loc"][i] is True, S5["close_loc"][i])

print("EMA and breadth thrust")
e = EX.ema_reset([0.5, 0.6, None, 0.3], 10)
ok("EMA seeds on the first value, alpha 2/11", close(e[1], 0.5 + (2 / 11) * 0.1) and e[0] == 0.5)
ok("a gap restarts the EMA", e[2] is None and e[3] == 0.3)
th = EX.thrust_series([0.5, 0.38, 0.45, 0.55, 0.62, 0.63])
ok("fires on the cross above 0.615 after a sub-0.40 reading", th == [False, False, False, False, True, False], th)
ok("no fire without the sub-0.40 reading", not any(EX.thrust_series([0.5, 0.45, 0.62])))
ok("no fire when the low reading is more than 10 sessions back",
   not any(EX.thrust_series([0.38] + [0.5] * 10 + [0.62])))

print("follow-through day")
c = [10, 9.5, 9, 8.5, 8, 8.1, 8.2, 8.3, 8.5]
v = [100] * 8 + [150]
f = EX.ftd_series(c, v, w=5, dmin=4, dmax=12)
ok("4 sessions after the 5-session low, +2.4% on higher volume -> fires", f[8] == (True, 4), f[8])
ok("the low itself does not fire", f[4][0] is False)
f2 = EX.ftd_series(c[:8] + [8.5], v[:8] + [90], w=5, dmin=4, dmax=12)
ok("lower volume than the prior session -> no", f2[8][0] is False)
c3 = [10, 9.5, 9, 8.5, 8, 8.1, 8.2, 8.0 * 1.0, 8.5]   # session 7 ties the low -> a new low, rally resets
f3 = EX.ftd_series(c3, v, w=5, dmin=4, dmax=12)
ok("a retest of the low resets the count (day 1 of a new attempt)", f3[8] == (False, 1), f3[8])

print("divergence (fewer lows at a lower low)")
idx = [12, 11, 10, 9, 8, 9, 10, 10.5, 9.5, 7.9]
low20 = [None] * 4 + [0.5, 0.2, 0.1, 0.1, 0.2, 0.3]
dv = EX.divergence_series(idx, low20, w=5, gap=2)
ok("new 5-session low with 30% at lows vs 50% at the prior low -> fires", dv[9] == (True, 0.3, 0.5, 4), dv[9])
low20b = list(low20)
low20b[9] = 0.6
ok("more names at lows than last time -> no", EX.divergence_series(idx, low20b, w=5, gap=2)[9][0] is False)
idx2 = [12, 11, 10, 9, 8, 7.9, 7.8, 7.7]
dv2 = EX.divergence_series(idx2, [0.5] * 4 + [0.5, 0.4, 0.3, 0.2], w=5, gap=2)
ok("a straight-line decline is one low, not two", not any(x and x[0] for x in dv2))

print("expanding percentile")
p = EX.expanding_pct([1, 2, 3, 2], "high", min_obs=1)
ok("high orient, mid-rank: 50, 75, 83.3, 50", [round(x[0], 4) for x in p] == [50.0, 75.0, 83.3333, 50.0], p)
q = EX.expanding_pct([1, 2, 3, 2], "low", min_obs=1)
ok("low orient: a low value ranks high", round(q[0][0], 4) == 50.0 and round(q[2][0], 4) == 16.6667 and round(q[3][0], 4) == 50.0, q)
ok("None under the minimum observations", EX.expanding_pct([1, 2, 3], "high", min_obs=3)[1][0] is None)
base = [random.Random(3).random() for _ in range(300)]
pa = EX.expanding_pct(base, "high", 10)
pb = EX.expanding_pct(base + [9.0, -9.0, 0.5], "high", 10)
ok("appending values never changes an earlier percentile", pa == pb[:300])

print("de-clustering and outcomes")
cond = [False] * 60
for k in (0, 1, 2, 10, 30, 51):
    cond[k] = True
ok("first day of each episode; a new one needs MORE than 20 quiet sessions", EX.decluster(cond, 20) == [0, 51], EX.decluster(cond, 20))
ok("episodes may only start inside lo..hi; a condition before lo still blocks",
   EX.decluster(cond, 20, lo=5) == [51])
px = [100, 90, 80, 120, 130]
ok("forward return", close(EX.fwd(px, 0, 3), 0.2) and EX.fwd(px, 3, 3) is None)
ok("max further drawdown within the window", close(EX.max_dd(px, 0, 3), -0.2) and EX.max_dd(px, 3, 1) == 0.0)
st = EX.stats([0.1, -0.1, 0.2, 0.3])
ok("stats: mean, median, hit, p10", close(st["mean"], 0.125) and close(st["median"], 0.15) and close(st["hit"], 0.75) and close(st["p10"], -0.04))
ok("under 10 episodes -> too few to conclude", EX.verdict(st, st)[0] == "too few to conclude")
many = EX.stats([0.05 + 0.001 * k for k in range(20)])
ok("a clear difference over 20 episodes -> above", EX.verdict(many, EX.stats([0.0, 0.01, -0.01] * 50))[0] == "above")


print("VIX curve (hand-worked)")
vax = ["v%02d" % k for k in range(16)]
v1 = [20, 20, 22, 25, 24, 21, 19, 18, 18, 18, 18, 18, 18, 18, 18, None]
v3 = [21, 21, 21, 22, 22, 22, 20, 20, 20, 20, 20, 20, 20, 20, 20, 20]
v9 = [19, 19, 24, 30, 26, 20, 18, 17, 17, 17, 17, 17, 17, 17, 17, 17]
vb = {"^VIX": bars_from(v1, [0] * 16), "^VIX3M": bars_from(v3, [0] * 16), "^VIX9D": bars_from(v9, [0] * 16)}
VV = EX.vix_series(vax, vb)
ok("ratio VIX/VIX3M = 22/21 on session 2", close(VV["ratio_1m_3m"][2], 22 / 21.0), VV["ratio_1m_3m"][2])
ok("ratio VIX9D/VIX = 30/25 on session 3", close(VV["ratio_9d_1m"][3], 1.2))
ok("inverted only above 1.0: sessions 2-4 (22/21, 25/22, 24/22); 21/22 is not",
   VV["inverted"][:7] == [False, False, True, True, True, False, False], VV["inverted"][:7])
ok("days_inverted counts the current run", VV["days_inverted"][:6] == [0, 0, 1, 2, 3, 0], VV["days_inverted"][:6])
ok("un-inverted within 10: session 5 (first calm session) through 14 (session 4 is 10 back)",
   VV["uninverted"][:15] == [False] * 5 + [True] * 10, VV["uninverted"][:15])
ok("a missing VIX bar -> every flag on that session is None",
   VV["inverted"][15] is None and VV["uninverted"][15] is None and VV["days_inverted"][15] is None)
ok("percentiles None under a year of observations", VV["pct"]["ratio_1m_3m"][14][0] is None)
ok("any of the three series absent -> no vix block (degrades, never breaks)",
   EX.vix_series(vax, {k: vb[k] for k in ("^VIX", "^VIX3M")}) is None)


def synth(n_days, n_mem=24, seed=11):
    rnd = random.Random(seed)
    ax = ["2000-%04d" % k for k in range(n_days)]
    bb, mm = {}, []
    mkt = [rnd.gauss(0.0003, 0.012) for _ in range(n_days)]
    for k in range(n_mem):
        c, v, lvl = [], [], 50.0
        for d in range(n_days):
            lvl *= 1 + 1.3 * mkt[d] + rnd.gauss(0, 0.015)
            c.append(lvl)
            v.append(int(1e5 * (1 + abs(mkt[d]) * 40 + rnd.random())))
        t = "S%02d" % k
        bb[t] = bars_from(c, v, highs=[x * (1 + rnd.random() * 0.02) for x in c],
                          lows=[x * (1 - rnd.random() * 0.02) for x in c])
        mm.append(t)
    lvl, c, h, l, v = 100.0, [], [], [], []
    for d in range(n_days):
        lvl *= 1 + mkt[d]
        c.append(lvl)
        h.append(lvl * (1 + rnd.random() * 0.01))
        l.append(lvl * (1 - rnd.random() * 0.01))
        v.append(int(1e6 * (1 + abs(mkt[d]) * 50)))
    bb["PX"] = bars_from(c, v, highs=h, lows=l)
    bb["SPY"] = bars_from(c, v, highs=h, lows=l)
    bb["SMH"] = bars_from([x * (1 + 0.3 * math.sin(d / 40.0)) for d, x in enumerate(c)], v)
    # a mean-reverting VIX that jumps on down days; VIX3M is half as far from 20,
    # so the curve inverts whenever VIX is above 20
    vx, x = [], 18.0
    for d in range(n_days):
        x = max(9.0, x + 0.15 * (17.0 - x) - 120 * mkt[d] + rnd.gauss(0, 0.6))
        vx.append(x)
    bb["^VIX"] = bars_from(vx, [0] * n_days)
    bb["^VIX3M"] = bars_from([20 + 0.5 * (y - 20) for y in vx], [0] * n_days)
    bb["^VIX9D"] = bars_from([20 + 1.3 * (y - 20) if y > 11 else y for y in vx], [0] * n_days)
    return ax, bb, mm


print("no look-ahead: flags on date D do not change when later data is appended")
ax_full, bb_full, mm = synth(1000)
D = 850
ax_cut = ax_full[:D]
bb_cut = {t: {k: arr[:D] for k, arr in b.items()} for t, b in bb_full.items()}
A = EX.compute_universe(ax_cut, bb_cut, mm, "PX", 15)
B = EX.compute_universe(ax_full, bb_full, mm, "PX", 15)
same_flags = all(A["flag"][k][:D] == B["flag"][k][:D] for k in EX.CAP_KEYS)
same_pct = all(A["pct"][k][:D] == B["pct"][k][:D] for k in EX.CAP_KEYS)
ok("every capitulation flag identical on every session <= D", same_flags)
ok("every percentile identical on every session <= D", same_pct)
ok("capitulation_count identical", A["cap"][:D] == B["cap"][:D])
ok("turn signals identical", A["thrust"][:D] == B["thrust"][:D] and A["ftd"][:D] == B["ftd"][:D] and A["div"][:D] == B["div"][:D])
ok("the test is not vacuous: some flags evaluated, some on",
   sum(1 for x in A["cap_eval"] if x == len(EX.CAP_KEYS)) > 100 and any(A["cap"]), (max(A["cap_eval"]), max(A["cap"])))
VA, VB = EX.vix_series(ax_cut, bb_cut), EX.vix_series(ax_full, bb_full)
ok("VIX curve: inverted, days_inverted and un-inverted identical on every session <= D",
   all(VA[k][:D] == VB[k][:D] for k in ("inverted", "days_inverted", "uninverted")))
ok("VIX curve: every percentile identical on every session <= D",
   all(VA["pct"][k][:D] == VB["pct"][k][:D] for k, _ in EX.VIX_KEYS))
ok("VIX test is not vacuous: inversions and un-inversions both occur, percentiles evaluated",
   any(VA["inverted"]) and any(VA["uninverted"]) and VA["pct"]["ratio_1m_3m"][D - 1][0] is not None)

print("payload: shape, determinism, public-safe words")
specs = {"coverage": {"label": "c", "members": mm, "proxy": "PX", "rule": "r"},
         "semis": {"label": "s", "members": mm[:16], "proxy": "PX", "rule": "r"},
         "market": {"label": "m", "members": mm[:9], "proxy": "PX", "rule": "r"}}


def make():
    body = EX.build(ax_full, bb_full, specs)
    p = EX.finish(body, EX.auto_read(body))
    p["generated_at"] = "2000-01-01T00:00:00Z"
    return json.dumps(p, separators=(",", ":"), sort_keys=True, allow_nan=False)


s1, s2 = make(), make()
ok("two runs are byte-identical", s1 == s2)
P = json.loads(s1)
u = P["universes"]["coverage"]
ok("history is at most 252 sessions", len(u["history"]) == min(EX.HIST_SESSIONS, len(ax_full)))
ok("today carries value, pctile, extreme for every capitulation rule",
   all(set(("value", "pctile", "extreme", "pctile_obs", "pctile_since")) <= set(u["today"][k]) for k in EX.CAP_KEYS))
ok("capitulation_on matches the count", len(u["capitulation_on"]) == u["capitulation_count"])
bt = P["backtest"]["universes"]["coverage"]
ok("every K from 2 to the rule count is reported, in order",
   [c["k"] for c in bt["capitulation"]] == list(range(2, len(EX.CAP_KEYS) + 1)))
ok("every cell carries n and verdict",
   all("n" in c[nm]["h%d" % h] and "verdict" in c[nm]["h%d" % h]
       for c in bt["capitulation"] for nm in ("proxy", "ew") for h in EX.HORIZONS))
ok("thin cells say so in the JSON", all(c["proxy"]["h21"]["verdict"] == "too few to conclude"
                                        for c in bt["capitulation"] if c["proxy"]["h21"]["n"] < 10))
ok("turn tables present", set(bt["turn"]) == {"thrust", "ftd", "divergence", "cap%d_then_turn" % EX.COMBO_K})
ok("backtest.read is written", len(P["backtest"]["read"]) > 80)
ok("no guard word anywhere in the payload", EX.GUARD.search(s1) is None, EX.GUARD.search(s1))
ok("no members list for the stock universes", u["members"] is None)
tmp = tempfile.mkdtemp()
fp = os.path.join(tmp, "exhaustion.json")
with open(fp, "w", encoding="utf-8") as fh:
    fh.write(s1)
ok("--check passes a good payload", EX.check(fp) == [], EX.check(fp))
with open(fp, "w", encoding="utf-8") as fh:
    fh.write(s1.replace('"read":', '"brokerage":"x","read":'))
ok("--check catches a guard word", any("guard word" in x for x in EX.check(fp)))

print("VIX block in the payload")
vx = P["vix"]
ok("vix.today carries value + expanding pctile for every continuous reading",
   all(set(("value", "pctile", "pctile_obs", "pctile_since")) <= set(vx["today"][k]) for k, _ in EX.VIX_KEYS))
ok("vix.today carries the two fixed flags and the run length",
   {"inverted", "days_inverted", "uninverted_within_10"} <= set(vx["today"]))
ok("vix.history is at most 252 sessions of d, vix, ratio_1m_3m",
   len(vx["history"]) == EX.HIST_SESSIONS and set(vx["history"][0]) == {"d", "vix", "ratio_1m_3m"})
ok("the VIX flags are NOT in any universe's capitulation count",
   all(set(P["universes"][k]["capitulation_on"]) <= set(EX.CAP_KEYS) for k in P["universes"]))
vbt = P["backtest"]["vix"]
ok("backtest.vix: inversion, un-inversion and K>=3-while-inverted per universe",
   {"inverted", "uninverted", "cap%d_inverted" % EX.COMBO_K} <= set(vbt)
   and set(vbt["cap%d_inverted" % EX.COMBO_K]) == set(specs))
ok("every VIX event cell carries n, verdict and vs_naive on SPY and SMH",
   all(set(("n", "verdict", "vs_naive", "mean")) <= set(vbt[b]["events"][t]["h%d" % h])
       for b in ("inverted", "uninverted") for t in ("SPY", "SMH") for h in EX.HORIZONS))
ok("the VIX backtest is not vacuous", vbt["inverted"]["events"]["episodes"] > 0, vbt["inverted"]["events"]["episodes"])
nov = json.loads(s1)
del nov["vix"]
del nov["backtest"]["vix"]
with open(fp, "w", encoding="utf-8") as fh:
    fh.write(json.dumps(nov))
ok("--check still passes a payload with no vix block (a failed fetch degrades)", EX.check(fp) == [], EX.check(fp))
bad = json.loads(s1)
del bad["vix"]["today"]["inverted"]
with open(fp, "w", encoding="utf-8") as fh:
    fh.write(json.dumps(bad))
ok("--check catches a vix block missing a key", any("vix.today missing inverted" in x for x in EX.check(fp)))
nobars = {t: b for t, b in bb_full.items() if t != "^VIX9D"}
body2 = EX.build(ax_full, nobars, specs)
ok("build without a VIX series: no vix block, no backtest.vix, universes unchanged",
   "vix" not in body2 and "vix" not in body2["backtest"]
   and json.dumps(EX._round_tree(body2["universes"]), sort_keys=True) == json.dumps(P["universes"], sort_keys=True))

print("backtest.read: plain English, both branches")
NO_EDGE = "There is no measurable edge here"
CLOSER = "today's readings describe how washed-out conditions are and are not a timing signal."


def _rows(p):
    """Every result row of the stock/sector groups and the volatility curve."""
    for u in p["backtest"]["universes"].values():
        for c in list(u["capitulation"]) + list(u["turn"].values()):
            for nm in ("proxy", "ew"):
                for h in EX.HORIZONS:
                    yield c[nm]["h%d" % h]


def _plain_ok(r, label):
    ok(label + ": no jargon or advice words", EX.PLAIN_READ_BANNED.search(r) is None, EX.PLAIN_READ_BANNED.search(r))
    ok(label + ": no guard word", EX.GUARD.search(r) is None, EX.GUARD.search(r))
    ok(label + ": three or four sentences ending in the timing caveat",
       3 <= r.count(". ") + 1 <= 5 and r.endswith(CLOSER), r)
    ok(label + ": under ~130 words", len(r.split()) <= 130, len(r.split()))


R = P["backtest"]["read"]
_plain_ok(R, "synthetic payload")
ok("read_detail keeps the statistical wording", P["backtest"]["read_detail"] == EX.auto_read_detail(P)
   and "standard errors" in P["backtest"]["read_detail"])
ok("read names the periods tested", R.startswith("Tested from") and "through" in R)
REAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "exhaustion.json")
if os.path.exists(REAL):
    with open(REAL, encoding="utf-8") as fh:
        J = json.load(fh)
    RR = EX.auto_read(J)
    _plain_ok(RR, "real payload")
    ok("real payload today: no-edge branch, phrase the card keys on, at most ~110 words",
       NO_EDGE in RR and "none did measurably better or worse than an ordinary day" in RR
       and "did measurably better" in RR and len(RR.split()) <= 112, RR)
    ok("real payload: read on disk is the generator's output", J["backtest"]["read"] == RR)
    E = json.loads(json.dumps(J))
    flipped = {"above": 0, "below": 0, "naive": 0}
    for row in _rows(E):
        if row["verdict"] == "no measurable difference" and not flipped["above"]:
            row["verdict"] = "above"; flipped["above"] = 1
        elif row["verdict"] == "no measurable difference" and not flipped["below"]:
            row["verdict"] = "below"; flipped["below"] = 1
        if row.get("vs_naive") == "no measurable difference" and not flipped["naive"]:
            row["vs_naive"] = "above"; flipped["naive"] = 1
    ok("synthetic edge case flips one of each", all(flipped.values()), flipped)
    RE = EX.auto_read(E)
    _plain_ok(RE, "edge branch")
    ok("edge branch states the differences truthfully and drops the no-edge phrase",
       NO_EDGE not in RE and "1 did measurably better than an ordinary day and 1 measurably worse" in RE
       and "1 did measurably better and none measurably worse" in RE and "leads to examine, not proof" in RE
       and "none of the volatility curve's" in RE, RE)
    V = json.loads(json.dumps(J))
    V["backtest"]["vix"]["inverted"]["events"]["SPY"]["h%d" % EX.HORIZONS[0]]["verdict"] = "below"
    RV = EX.auto_read(V)
    ok("a volatility-curve difference gets its own sentence", "On the volatility curve's" in RV
       and "and 1 measurably worse" in RV and NO_EDGE in RV, RV)
    J2 = json.loads(json.dumps(J))
    J2["backtest"]["read"] = J2["backtest"]["read_detail"]
    with open(fp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(J2))
    ok("--check rejects the jargon text in backtest.read", any("plain English" in x for x in EX.check(fp)), EX.check(fp))
    del V["backtest"]["vix"]
    RN = EX.auto_read(V)
    ok("no vix block: the volatility curve is not mentioned", "volatility" not in RN and NO_EDGE in RN, RN)

print("the real configuration")
specs_real = EX.universes_from()
cov, sem, mkt = (set(specs_real[k]["members"]) for k in ("coverage", "semis", "market"))
ok("coverage excludes ETFs in single-stock sections", not ({"GDX", "XLF", "KRE", "UFO", "ARKX", "DRAM", "CIBR", "XBI"} & cov))
ok("coverage excludes indices, futures, FX, crypto and foreign listings",
   not any(t.startswith("^") or "=" in t or t.endswith("-USD") or "." in t for t in cov))
ok("coverage has the obvious single stocks", {"NVDA", "JPM", "NEM", "MSFT", "VST"} <= cov)
ok("semis are a subset of coverage and include NVDA and MU", sem <= cov and {"NVDA", "MU"} <= sem)
ok("semis proxy is SMH", specs_real["semis"]["proxy"] == "SMH")
ok("market is the ladder's sector ETFs, one per twin group", "XLK" in mkt and "SMH" in mkt and "SOXX" not in mkt and "SLV" not in mkt and "SPY" not in mkt)
ok("thresholds are one rule per indicator", len(EX.RULES) == len(set(EX.CAP_KEYS)))

print("\n%d passed, %d failed" % (count - fails, fails))
sys.exit(1 if fails else 0)
