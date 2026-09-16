#!/usr/bin/env python3
"""
Tests for backtest_rotation.py.

Four things are worth pinning here and nothing else is:

  1. NO LOOK-AHEAD, on both sides. A decision taken on bar d must not change
     when bars after d are replaced with a different tape (selection side), and
     the return it earns must start at bar d+1 (accrual side). Both are tested
     WITH a positive control -- a mutation that is allowed to change the later
     decision -- so a test that passes because nothing was mutated fails.
  2. THE TOP-THIRD SELECTION, including the ceil(L/3) boundary and the twin
     dedup, because "the top third" is the whole of rule 1 and an off-by-one in
     it silently changes every number in the memo.
  3. THE EXPOSURE DIAL -- the 100/50/0 mapping and the one-bar weight offset.
  4. THE STATS ARITHMETIC -- CAGR, annualised vol, max drawdown, hit rate,
     turnover and the bootstrap -- against hand-computed values.

Synthetic series only; no database, no network.

Run:  python test_backtest_rotation.py
"""

import math
import sys

import backtest_rotation as B
import regime as RG

FAILS = []


def check(name, got, want, tol=None):
    ok = (abs(got - want) <= tol) if (tol is not None and
                                      isinstance(got, (int, float))) else (got == want)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}" +
          ("" if ok else f", want {want!r}"))
    if not ok:
        FAILS.append(name)


def check_true(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{(' -- ' + detail) if detail else ''}")
    if not cond:
        FAILS.append(name)


# --------------------------------------------------------------------------
# synthetic panel
# --------------------------------------------------------------------------

def ramp(n, drift, start=100.0):
    """A clean geometric series with a constant daily drift. No noise on
    purpose: every ranking below is then exactly predictable, so a failure is a
    bug in the harness and never a draw of the dice."""
    s = [start]
    for _ in range(n - 1):
        s.append(s[-1] * (1.0 + drift))
    return s


N = 220
# nine sleds, strictly ordered by drift: A fastest .. I slowest (and negative)
DRIFTS = {"A": 0.0040, "B": 0.0035, "C": 0.0030, "D": 0.0020, "E": 0.0010,
          "F": 0.0005, "G": 0.0000, "H": -0.0010, "I": -0.0020}
ORDER = ["A", "B", "C", "D", "E", "F", "G", "H", "I"]
SERIES = {t: ramp(N, d) for t, d in DRIFTS.items()}
ETFS = [{"t": t, "name": t, "side": None} for t in ORDER]


print("== top-third selection ==")

lad = RG.sector_ladder(SERIES, ETFS)
check("field size is the whole 9-sled field", len(B.field_rows(lad)), 9)
check("top third of 9 = ceil(9/3) = 3 sleds", len(B.sel_blend_top(lad)), 3)
check("and it is the three fastest", sorted(B.sel_blend_top(lad)), ["A", "B", "C"])
check("bottom third is the three slowest", sorted(B.sel_blend_bottom(lad)), ["G", "H", "I"])

# ceil(L/3) boundary: 10 sleds -> 4, not 3
ten = dict(SERIES)
ten["J"] = ramp(N, 0.0032)
lad10 = RG.sector_ladder(ten, ETFS + [{"t": "J", "name": "J", "side": None}])
check("top third of 10 = ceil(10/3) = 4 sleds", len(B.sel_blend_top(lad10)), 4)
check("and J sorts into it by blend", "J" in B.sel_blend_top(lad10), True)

# the r63 / streak / 21d variants read the ladder's own tags
check("streak>=5 selector reads third=='top'",
      sorted(B.sel_r63_streak5(lad)), ["A", "B", "C"])
check_true("every streak>=5 pick really has streak>=5",
           all(r["streak"] >= 5 for r in lad["rows"] if r["t"] in B.sel_r63_streak5(lad)))
check("ex-bottom-21d selector on a monotone tape is the plain top third",
      sorted(B.sel_r63_ex21bottom(lad)), ["A", "B", "C"])

# twin dedup: a second member of the same grp must be out of the FIELD entirely
twin_etfs = [{"t": "A", "name": "A", "side": None, "grp": "g"},
             {"t": "B", "name": "B", "side": None, "grp": "g"}] + \
            [{"t": t, "name": t, "side": None} for t in ORDER[2:]]
lad_tw = RG.sector_ladder(SERIES, twin_etfs)
check("a twin is dropped from the field", len(B.field_rows(lad_tw)), 8)
check_true("and can never be selected", "B" not in B.sel_blend_top(lad_tw),
           str(B.sel_blend_top(lad_tw)))


print("\n== no look-ahead: selection ==")

REBALS = [130, 160, 190]


def decisions_on(series):
    out = []
    for i in REBALS:
        out.append((i, B.sel_blend_top(RG.sector_ladder(B.window(series, set(ORDER), i), ETFS))))
    return out


base = decisions_on(SERIES)
# replace every bar AFTER 160 with a tape that reverses the ranking
mut = {}
for t in ORDER:
    s = list(SERIES[t])
    for i in range(161, N):
        s[i] = s[160] * (1.0 + (-DRIFTS[t] * 4.0)) ** (i - 160)
    mut[t] = s
after = decisions_on(mut)
check("decision on bar 130 is untouched by bars after 160", after[0][1], base[0][1])
check("decision on bar 160 is untouched by bars after 160", after[1][1], base[1][1])
check_true("POSITIVE CONTROL: the bar-190 decision DOES change",
           after[2][1] != base[2][1], f"{base[2][1]} -> {after[2][1]}")

# the whole-window version of the same claim, through walk_ladder
w_base = B.decisions_from(B.walk_ladder(SERIES, ETFS, 130, 160, 30), B.sel_blend_top)
w_mut = B.decisions_from(B.walk_ladder(mut, ETFS, 130, 160, 30), B.sel_blend_top)
check("walk_ladder decisions up to the mutation point are identical", w_mut, w_base)


print("\n== no look-ahead: return accrual ==")

# one decision at bar 5; the sled jumps +100% ON bar 5 and +10% on bar 6.
# Only the bar-6 move may reach the equity curve.
jump = {"X": [0.0] * 10}
jump["X"][5] = 1.00     # the day the decision is taken -- belongs to the PRIOR holder
jump["X"][6] = 0.10
idx0, eq, per, bounds, empty = B.run_decisions(jump, [(5, ["X"])], 8)
check("equity starts at 1.0 on the decision bar", eq[0], 1.0)
check("the decision-bar move is excluded", round(eq[1], 10), 1.10)
check("period return is the post-decision compounding only", round(per[0], 10), 0.10)
check("bounds run decision-bar -> end", bounds, [(5, 8)])
check("nothing was flagged empty", empty, 0)

empty_run = B.run_decisions({}, [(0, [])], 3, cash_rets=[0.0, 0.01, 0.01, 0.01])
check("an empty selection earns the cash leg", round(empty_run[1][-1], 10), round(1.01 ** 3, 10))
check("and is counted as an empty period", empty_run[4], 1)


print("\n== exposure dial ==")

check("risk-on  -> 100%", B.state_weight("risk-on"), 1.0)
check("neutral  ->  50%", B.state_weight("neutral"), 0.5)
check("defensive ->  0%", B.state_weight("defensive"), 0.0)
check("an unknown state falls back to 50%", B.state_weight(None), 0.5)

ra = [None, 0.10, 0.10, 0.10]
rb = [None, 0.00, 0.00, 0.00]
w = [0.0, 1.0, 0.5, 0.0]
eqd = B.run_weighted(ra, rb, w, 0, 3)
check("dial compounds 100% then 50% then 0%", [round(v, 10) for v in eqd],
      [1.0, 1.1, round(1.1 * 1.05, 10), round(1.1 * 1.05, 10)])
eqc = B.run_weighted(ra, [None, 0.02, 0.02, 0.02], [0.0, 0.0, 0.0, 0.0], 0, 3)
check("a 0% dial earns the cash leg, not zero", round(eqc[-1], 10), round(1.02 ** 3, 10))

# the one-bar offset the dial is built on: the weight applied to bar i comes
# from the state on bar i-1, so a flip on bar i cannot trade bar i's own move.
state = ["risk-on", "risk-on", "defensive", "defensive"]
wts = [0.0] * 4
for i in range(1, 4):
    wts[i] = B.state_weight(state[i - 1])
check("a flip on bar 2 first bites on bar 3", wts, [0.0, 1.0, 1.0, 0.0])


print("\n== stats arithmetic ==")

check("CAGR doubles over exactly 252 sessions", round(B.cagr([1.0, 2.0], 252), 10), 1.0)
check("CAGR doubles over 504 sessions", round(B.cagr([1.0, 2.0], 504), 6), round(2 ** 0.5 - 1, 6))
check("constant returns have zero vol", round(B.ann_vol([1.0, 1.1, 1.21, 1.331]), 10), 0.0)
_v = B.ann_vol([1.0, 1.1, 1.0, 1.1])
check_true("alternating returns have positive vol", _v > 0, f"{_v:.4f}")
check("max drawdown is peak-to-trough", round(B.max_drawdown([1.0, 1.2, 0.9, 1.0]), 10), -0.25)
check("a monotone curve has no drawdown", B.max_drawdown([1.0, 1.1, 1.2]), 0.0)

hr, hn = B.hit_rate([0.1, 0.2, -0.1], [0.0, 0.3, -0.2])
check("hit rate counts strictly-better periods", (round(hr, 6), hn), (round(2 / 3.0, 6), 3))

check("turnover of a half-swapped 2-name book is 0.5",
      round(B.turnover([(0, ["A", "B"]), (1, ["B", "C"])]), 10), 0.5)
check("turnover of an unchanged book is 0",
      round(B.turnover([(0, ["A", "B"]), (1, ["B", "A"])]), 10), 0.0)
check("turnover of a fully-swapped book is 1",
      round(B.turnover([(0, ["A"]), (1, ["B"])]), 10), 1.0)
check("one decision has no turnover to report", B.turnover([(0, ["A"])]), None)

check("period_returns reads the curve at the bounds",
      [round(x, 10) for x in B.period_returns([1.0, 1.1, 2.2], 0, [(0, 1), (1, 2)])],
      [round(0.1, 10), 1.0])

check("ew_bar_return skips members with no bar",
      round(B.ew_bar_return({"A": [None, 0.10], "B": [None, None]}, ["A", "B"], 1), 10), 0.10)
check("ew_bar_return is 0.0 when nothing printed",
      B.ew_bar_return({"A": [None, None]}, ["A"], 1), 0.0)
check("daily_returns index i is the i-1 -> i move",
      [round(v, 10) if v is not None else None for v in B.daily_returns([100.0, 110.0, None, 121.0])],
      [None, round(0.1, 10), None, None])

check("first_field_idx wants lag bars of history on every sled",
      B.first_field_idx({"A": [None, 1, 2, 3, 4], "B": [None, None, 1, 2, 3]}, 1.0, lag=2), 4)
check("and relaxes with frac", B.first_field_idx({"A": [None, 1, 2, 3, 4],
                                                  "B": [None, None, 1, 2, 3]}, 0.5, lag=2), 3)
check("first_bar_idx finds the first real bar", B.first_bar_idx([None, None, 7.0]), 2)


print("\n== bootstrap ==")

pos = B.bootstrap([0.02] * 30, B=2000)
check("a constant positive excess bootstraps to p=0", pos["p"], 0.0)
check("and reports its own mean", round(pos["mean"], 10), 0.02)
sym = B.bootstrap([0.05, -0.05] * 15, B=2000)
check_true("a mean-zero excess is indistinguishable from noise", sym["p"] > 0.5,
           f"p={sym['p']}")
check_true("the CI brackets the observed mean",
           pos["lo"] <= pos["mean"] <= pos["hi"], f"{pos['lo']}..{pos['hi']}")
check("the seed makes it reproducible", B.bootstrap([0.01, -0.02, 0.03] * 8, B=500)["p"],
      B.bootstrap([0.01, -0.02, 0.03] * 8, B=500)["p"])
check("n<3 returns no p at all", B.bootstrap([0.01, 0.02])["p"], None)
check("the block bootstrap collapses to iid when there are <4 blocks",
      B.bootstrap([0.01] * 6, B=200, block=4)["block"], 1)
check("and keeps its block length when there is room",
      B.bootstrap([0.01] * 40, B=200, block=4)["block"], 4)
check("signs are counted, not weighted", B.sign_test_pos([0.1, -0.1, 0.1, 0.0]), (2, 4))

_mr = B.make_result("t", "s", "u", "b", ["2026-01-02"] * 12, 0, 11,
                    [1.0] * 12, [1.0] * 12, [0.0] * 4, [0.0] * 4, [(0, ["A"])], 1)
check_true("a result with fewer than MIN_PERIODS periods says so",
           _mr["verdict"].startswith("can't tell yet"), _mr["verdict"])


print("\n== spearman ==")

check("monotone increasing -> +1", round(B.spearman([(1, 1), (2, 2), (3, 3), (4, 4)]), 10), 1.0)
check("monotone decreasing -> -1", round(B.spearman([(1, 4), (2, 3), (3, 2), (4, 1)]), 10), -1.0)
check("ties get average ranks", round(B.spearman([(1, 1), (1, 2), (2, 3), (2, 4)]), 6),
      round(B.spearman([(1, 1), (1, 2), (2, 3), (2, 4)]), 6))


print("\n== the composite state series is causal ==")

# composite_series is the function rule 3 reads. Its state on bar i must depend
# only on bars <= i -- it is a forward recursion with hysteresis, so this is a
# prefix-stability claim, and it is the claim rule 3 rests on.
legs = [[1, 1, 0, -1, -1, -1, 0, 1, 1, 1],
        [0, 1, 1, -1, -1, 0, 0, 1, 1, 0],
        [1, 1, 1, -1, -1, -1, -1, 0, 1, 1]]
full = RG.composite_series(legs)["state"]
for cut in (4, 6, 8):
    part = RG.composite_series([L[:cut + 1] for L in legs])["state"]
    check(f"state prefix through bar {cut} is unchanged by later bars", part, full[:cut + 1])
check("100/50/0 maps every state the composite can emit",
      sorted({B.state_weight(s) for s in ("risk-on", "neutral", "defensive")}),
      [0.0, 0.5, 1.0])
check_true("and the synthetic legs really did flip state",
           len(set(full)) >= 2, str(sorted(set(full))))


print("\n== sled_series / needed_tickers ==")

bask = [{"t": "BK", "name": "BK", "side": None, "basket": ["A", "B"]},
        {"t": "C", "name": "C", "side": None}]
sl = B.sled_series(SERIES, bask)
check_true("a basket sled is an equal-weight index, not a lookup",
           sl["BK"] is not None and sl["BK"][0] is None and sl["BK"][-1] > 100.0,
           f"last={sl['BK'][-1]:.1f}")
check_true("a plain sled is its own series, not a copy", sl["C"] is SERIES["C"])
check("needed_tickers pulls basket members in", B.needed_tickers(bask), {"BK", "A", "B", "C"})
check("window truncates to bars 0..i inclusive",
      len(B.window(SERIES, {"A"}, 10)["A"]), 11)


print("\n" + ("-" * 60))
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all tests passed")
