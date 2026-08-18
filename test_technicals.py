#!/usr/bin/env python3
"""
Tests for technicals.py.

The regime-gate tests deliberately reuse Hurwitz & Marwala's own method
(arXiv:1110.3383 section III): generate pseudodata with a known underlying
structure, then check the classifier recovers it. Their two generators are a
piecewise straight line (trending, their eq 5) and a sinusoid (cyclical, their
eq 6), each with noise laid over it.

Run:  python test_technicals.py
"""

import math
import random
import sys

import technicals as T

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
# pseudodata generators (H&M section III)
# --------------------------------------------------------------------------

def trending(n=250, seed=1, noise=0.01, seg=80):
    """Piecewise straight line, slope changing at intervals, continuity kept."""
    rng = random.Random(seed)
    out, val, slope = [], 100.0, 0.4
    for i in range(n):
        if i % seg == 0 and i:
            slope = rng.choice([0.3, 0.5, 0.45])
        val += slope
        out.append(val * (1 + noise * (rng.random() - 0.5)))
    return out


def cyclical(n=250, seed=2, noise=0.01, period=40, amp=15.0, mixed=False):
    """
    Sinusoid about a flat level -- the RSI assumption. `mixed` varies period and
    amplitude partway through, which is H&M's figure 6 case: the one their RSI
    could not handle "owing to the varying frequencies for which a static
    strategy could not account".
    """
    rng = random.Random(seed)
    out, p, a = [], period, amp
    for i in range(n):
        if mixed and i % 60 == 0 and i:
            p, a = rng.choice([20, 30, 50, 70]), rng.choice([8.0, 15.0, 22.0])
        out.append((100.0 + a * math.sin(2 * math.pi * i / p)) *
                   (1 + noise * (rng.random() - 0.5)))
    return out


def random_walk(n=250, seed=3, sigma=0.015):
    """No trend, no cycle -- the case where nothing should be readable."""
    rng = random.Random(seed)
    out, v = [], 100.0
    for _ in range(n):
        v *= math.exp(rng.gauss(0, sigma))
        out.append(v)
    return out


# --------------------------------------------------------------------------

print("primitives")
xs = [float(i) for i in range(1, 11)]
check("sma(3) last", T.sma(xs, 3)[-1], 9.0, 1e-9)
check("ema(3) seed", T.ema(xs, 3)[2], 2.0, 1e-9)
check("ema(3) step", T.ema(xs, 3)[3], 3.0, 1e-9)
check("rsi monotone up", T.rsi([float(i) for i in range(1, 60)])[-1], 100.0, 1e-9)
check("rsi monotone down", T.rsi([float(i) for i in range(60, 1, -1)])[-1], 0.0, 1e-9)

print("\nmacd identity")
c = trending(200)
ml, sl, hist = T.macd(c)
check_true("hist == line - signal",
           all(abs(hist[i] - (ml[i] - sl[i])) < 1e-9
               for i in range(len(c)) if hist[i] is not None))
check_true("macd warms up at slow+sig-2",
           ml[T.MACD_SLOW - 2] is None and ml[T.MACD_SLOW - 1] is not None,
           f"first non-None at index {next(i for i, v in enumerate(ml) if v is not None)}")

print("\nvariance ratio is a diagnostic, NOT a cycle detector")
# This block exists because an earlier version of the gate classified on VR and
# was wrong. Both facts below are the reason it no longer does.
vr_walk = T.variance_ratio(random_walk(600, seed=11))
vr_cyc = T.variance_ratio(cyclical(600, period=40))
vr_trend = T.variance_ratio(trending(600, noise=0.02))
check_true("random walk VR ~ 1", 0.6 < vr_walk < 1.5, f"VR={vr_walk:.3f}")
check_true("cyclical VR is ABOVE 1, not below (sinusoids are locally smooth)",
           vr_cyc > 1.5, f"VR={vr_cyc:.3f}")
check_true("drift alone does not push VR above 1", vr_trend < 1.5,
           f"VR={vr_trend:.3f} -- drift is detected by R^2, not VR")

print("\nR^2 threshold is calibrated against the random-walk null")
r2_null = sorted(T.r2_loglinear(random_walk(90, s)) for s in range(400))
frac_50 = sum(1 for x in r2_null if x >= 0.50) / len(r2_null)
frac_thr = sum(1 for x in r2_null if x >= T.R2_TREND_MIN) / len(r2_null)
print(f"    random walks clearing R^2>=0.50: {frac_50:.1%}  "
      f"clearing R^2>={T.R2_TREND_MIN}: {frac_thr:.1%}")
check_true("R^2>=0.50 would be a coin flip on random walks", frac_50 > 0.30,
           f"{frac_50:.1%} -- this is why the threshold is not 0.50")
check_true("chosen threshold keeps random-walk false positives near the stated rate",
           frac_thr <= T.GATE_FALSE_TREND + 0.05,
           f"{frac_thr:.1%} vs published {T.GATE_FALSE_TREND:.0%}")

print("\ncrossing rate separates a smooth cycle from chop")
cr_walk = sorted(T.sma_cross_rate(random_walk(90, s)) for s in range(400))
cr_cyc = T.sma_cross_rate(cyclical(90, period=40))
print(f"    random walk median {cr_walk[200]:.1f}/100   cycle p40 {cr_cyc:.1f}/100")
check_true("a clean cycle crosses its SMA less than a random walk chops",
           cr_cyc < cr_walk[200], f"{cr_cyc:.1f} vs {cr_walk[200]:.1f}")
check_true("a pure trend barely crosses at all",
           T.sma_cross_rate(trending(90)) <= 1.0)

print("\nregime gate recovers the generator (H&M design)")
g_trend = T.classify_regime(trending(250))
g_cyc = T.classify_regime(cyclical(250))
check("trending pseudodata", g_trend["regime"], "trending")
check("cyclical pseudodata", g_cyc["regime"], "mean_reverting")

# Single-frequency cycles must be recognised every time.
cyc_labels = [T.classify_regime(cyclical(250, seed=s))["regime"] for s in range(200)]
check_true("stable cycle is recognised essentially always",
           cyc_labels.count("mean_reverting") / len(cyc_labels) > 0.95,
           f"{cyc_labels.count('mean_reverting')}/200")

# Mixed-frequency is deliberately NOT required to classify as mean_reverting.
# H&M found RSI "performed very erratically" on their figure-6 mixed oscillator
# "owing to the varying frequencies for which a static strategy could not
# account". A gate that greenlit a fixed-parameter RSI there would be repeating
# the mistake the gate exists to prevent. What it must never do is call it a
# trend.
mixed_labels = [T.classify_regime(cyclical(250, seed=s, mixed=True))["regime"]
                for s in range(200)]
print(f"    mixed-frequency -> " +
      ", ".join(f"{k} {mixed_labels.count(k)}" for k in set(mixed_labels)))
check_true("mixed-frequency cycle is never called a trend",
           mixed_labels.count("trending") == 0)
check_true("mixed-frequency cycle is gated off at least some of the time",
           mixed_labels.count("neither") > 0.25 * len(mixed_labels),
           "a static oscillator cannot track a moving frequency")

# Random walks are checked in aggregate, not on one seed: any single seed can
# legitimately look like a trend, which is the whole point of the error rate.
walks = [T.classify_regime(random_walk(250, seed=s))["regime"] for s in range(300)]
fp_trend = walks.count("trending") / len(walks)
fp_rev = walks.count("mean_reverting") / len(walks)
print(f"    random walks -> trending {fp_trend:.1%}, mean_reverting {fp_rev:.1%}, "
      f"neither {walks.count('neither') / len(walks):.1%}")
check_true("most random walks fall through to 'neither'",
           walks.count("neither") / len(walks) > 0.60)
check_true("published false-trending rate is honest",
           abs(fp_trend - T.GATE_FALSE_TREND) < 0.10,
           f"measured {fp_trend:.1%} vs published {T.GATE_FALSE_TREND:.0%}")

print("\ngate mutes the wrong indicator family")
check_true("trend regime enables MACD/VPVMA only",
           g_trend["trend_ok"] and not g_trend["oscillator_ok"])
check_true("cyclical regime enables RSI/MFI only",
           g_cyc["oscillator_ok"] and not g_cyc["trend_ok"])
check_true("gate publishes its own false-positive rate",
           g_trend["false_positive_rate"] == T.GATE_FALSE_TREND)

print("\nH&M's headline: MACD earns on trends, is erratic on mixed-frequency cycles")
for label, gen in (("trending", trending(400)),
                   ("cycle-p40", cyclical(400)),
                   ("cycle-mixed", cyclical(400, mixed=True))):
    h = [x * 1.01 for x in gen]
    lo = [x * 0.99 for x in gen]
    vol = [1_000_000] * len(gen)
    sigs = T.strategy_signals(gen, h, lo, list(gen), vol)
    bt = T.backtest(gen, *sigs["macd_crossover_sig"])
    print(f"    MACD on {label:11s}: strategy {bt['strategy_return']:+8.2%}  "
          f"buy&hold {bt['buy_hold_return']:+7.2%}  trades {bt['n_trades']}")
# Only the trending claim is asserted. On a single clean sinusoid MACD does
# well -- H&M's failure case was the mixed-frequency series, and reproducing a
# paper's numbers from a different generator would be testing the generator.
check_true("MACD profitable on trending pseudodata",
           T.backtest(trending(400),
                      *T.strategy_signals(trending(400),
                                          [x * 1.01 for x in trending(400)],
                                          [x * 0.99 for x in trending(400)],
                                          trending(400),
                                          [1_000_000] * 400)["macd_crossover_sig"]
                      )["strategy_return"] > 0)

print("\nbacktest bookkeeping")
flat = [100.0] * 50
check("flat series buy&hold is zero",
      T.backtest(flat, [False] * 50, [False] * 50)["buy_hold_return"], 0.0, 1e-9)
rising = [100.0 * (1.01 ** i) for i in range(50)]
bt = T.backtest(rising, [i == 0 for i in range(50)], [False] * 50)
check_true("always-long matches buy&hold on a monotone series",
           abs(bt["strategy_return"] - bt["buy_hold_return"]) < 0.02,
           f"strat {bt['strategy_return']:.4f} vs hold {bt['buy_hold_return']:.4f}")

print("\nwatchlist grouping")
groups = T.load_groups()
check_true("NVDA maps to a semi group with SMH proxy",
           groups.get("NVDA", {}).get("proxy") == "SMH",
           str(groups.get("NVDA")))
check_true("NEM maps to GDX proxy",
           groups.get("NEM", {}).get("proxy") == "GDX", str(groups.get("NEM")))
check_true("NOW maps to IGV proxy",
           groups.get("NOW", {}).get("proxy") == "IGV", str(groups.get("NOW")))
check_true("comment headers are not parsed as tickers",
           not any(t.startswith("-") or " " in t for t in groups))

print("\nmomentum structure -- the three screener families")
# ATR: constant 2-point range on a 100 close = 2% forever
n_c = [100.0] * 300
n_h = [101.0] * 300
n_l = [99.0] * 300
atr = T.atr_pct_series(n_h, n_l, n_c)
check("ATR% on constant-range series", atr[-1], 2.0, 1e-9)
check("ATR warms up after n bars", atr[T.ATR_N - 1], None)

check("pct_rank_trailing top", T.pct_rank_trailing([1.0, 2.0, 3.0, 4.0, 5.0], 5), 1.0, 1e-9)
check("pct_rank_trailing bottom", T.pct_rank_trailing([5.0, 4.0, 3.0, 2.0, 1.0], 5), 0.2, 1e-9)
check("pct_rank_trailing partial window is None",
      T.pct_rank_trailing([1.0, 2.0], 5), None)

up = [100.0 * (1.003 ** i) for i in range(300)]
dn = list(reversed(up))
check("ema_stack monotone up", T.ema_stack(up)["state"], "aligned_up")
check("ema_stack monotone down", T.ema_stack(dn)["state"], "aligned_down")

# rising zigzag: 14-bar triangle waves, amplitude 14, base climbing 4 per wave.
# Geometry matters: the peak must be the strict max of its +/-5 window (swing_label
# rejects tied extremes on purpose -- a flat top is not a confirmed pivot).
zz_h, zz_l = [], []
for blk in range(8):
    base = 100.0 + 4 * blk
    for j in range(14):
        lvl = base + 2 * (j if j <= 7 else 14 - j)
        zz_h.append(lvl + 1)
        zz_l.append(lvl - 1)
check("swing label on rising zigzag", T.swing_label(zz_h, zz_l), "HH/HL")
# and the mirror: falling zigzag reads LH/LL
check("swing label on falling zigzag",
      T.swing_label(list(reversed(zz_h)), list(reversed(zz_l))), "LH/LL")

alt_c = [100 + (1 if i % 2 else 0) for i in range(80)]
alt_v = [2_000_000 if alt_c[i] > alt_c[i - 1] else 1_000_000 for i in range(1, 80)]
alt_v = [1_000_000] + alt_v
check("U/D volume ratio 2:1", T.ud_vol_ratio(alt_c, alt_v), 2.0, 0.05)

# bearish divergence: price grinds up on thin volume, dumps on heavy volume
div_c, div_v, px = [], [], 100.0
for i in range(120):
    if i % 10 == 9:
        px -= 0.5
        div_v.append(50_000_000)
    else:
        px += 0.1
        div_v.append(1_000_000)
    div_c.append(px)
check("OBV bearish divergence detected", T.obv_read(div_c, div_v), "bearish_divergence")

novol = T.momentum_structure(n_h, n_l, n_c, [0] * 300)
check_true("zero-volume name cannot be in setup",
           novol["ud_vol_50d"] is None and not novol["in_setup"])

print("\nevidence flags are lookahead-free")
import momentum_evidence as ME
tr_c = trending(400)
tr_h = [x * 1.01 for x in tr_c]
tr_l = [x * 0.99 for x in tr_c]
tr_v = [1_000_000] * 400
st, co, bu = ME.day_flags(tr_h, tr_l, tr_c, tr_v)
check_true("flag arrays match series length", len(st) == len(co) == len(bu) == 400)
check_true("stacked flag warms up (None early, defined late)",
           st[0] is None and st[-1] is not None)
check_true("coiled needs a full trailing year",
           all(v is None for v in co[:T.ATR_RANK_WIN - 1]))
check_true("buyers flag defined after 50 sessions", bu[60] is not None)

print("\n" + ("-" * 60))
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all tests passed")
