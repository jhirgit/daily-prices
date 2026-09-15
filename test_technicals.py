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

print("\nregime port -- golden-fixture parity vs the JS oracle (SPEC-8)")
# The client-side regime engine (deploy-jr-dash/regime_core.js) is the ORACLE.
# tools/regime_parity_gen.js froze its output for a shared fixture into
# parity/expected.json; here the Python port (regime.py) is run on the SAME
# parity/fixture.json and must match EXACTLY for integer votes / states / tiers
# / streaks and within abs<1e-9 for floats (blend, sinceRet, base-rate medians).
# Any divergence blocks the port. Regenerate the golden data with:
#   node tools/regime_parity_gen.js
import json
import os
import regime as RG

_PAR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parity")
_PARITY_TOL = 1e-9


def _regime_run_case(fn, a):
    if fn == "ratio": return RG.ratio(a["num"], a["den"])
    if fn == "ratioLegSeries": return RG.ratio_leg_series(a["rat"])
    if fn == "trendLegSeries": return RG.trend_leg_series(a["px"], a["invert"])
    if fn == "breadthSeries": return RG.breadth_series(a["series"], a["names"])
    if fn == "breadthLegSeries": return RG.breadth_leg_series(a["frac"])
    if fn == "compositeSeries": return RG.composite_series(a["legs"], a.get("firstActives"))
    if fn == "flips": return RG.flips(a["dates"], a["state"])
    if fn == "baseRates": return RG.base_rates(a["series"], a["names"], a["state"], a.get("H") or 21)
    if fn == "baseRatesMulti": return RG.base_rates_multi(a["series"], a["names"], a["state"], a["hs"])
    if fn == "rankReceipts": return RG.rank_receipts(a["series"], a["names"])
    if fn == "ret": return RG.ret(a["close"], a["lag"])
    if fn == "sectorLadder": return RG.sector_ladder(a["series"], a["etfs"])
    if fn == "durations": return RG.durations(a["state"])
    if fn == "retSeries": return RG.ret_series(a["close"])
    if fn == "rvSeries": return RG.rv_series(a["close"], a["n"])
    if fn == "pctRankSeries": return RG.pct_rank_series(a["vals"], a["win"])
    if fn == "pctVoteSeries": return RG.pct_vote_series(a["pct"], a["lo"], a["hi"])
    if fn == "corrPairSeries": return RG.corr_pair_series(a["a"], a["b"], a["win"])
    if fn == "corrVoteSeries": return RG.corr_vote_series(a["corr"], a["thr"])
    if fn == "avgCorrSeries": return RG.avg_corr_series(a["series"], a["names"], a["win"], a["minN"])
    if fn == "legFirstActive": return RG.leg_first_active(a["cont"])
    if fn == "firstNonNull": return RG.first_non_null(a["arr"])
    raise ValueError("unknown fn " + fn)


def _regime_diff(exp, act, path=""):
    """Tolerant deep compare: exact for None/str/bool, abs<=1e-9 for numbers
    (which is also exact for integer votes/streaks), recursive for list/dict."""
    if exp is None:
        return [] if act is None else [f"{path}: expected null, got {act!r}"]
    if isinstance(exp, bool) or isinstance(act, bool):
        return [] if exp == act else [f"{path}: bool {exp!r} != {act!r}"]
    if isinstance(exp, str):
        return [] if exp == act else [f"{path}: str {exp!r} != {act!r}"]
    if isinstance(exp, (int, float)):
        if not isinstance(act, (int, float)):
            return [f"{path}: number {exp!r} vs {act!r}"]
        return [] if abs(exp - act) <= _PARITY_TOL else [f"{path}: {exp!r} != {act!r}"]
    if isinstance(exp, list):
        if not isinstance(act, list) or len(exp) != len(act):
            return [f"{path}: list shape {len(exp) if isinstance(exp, list) else '?'} vs "
                    f"{len(act) if isinstance(act, list) else type(act).__name__}"]
        out = []
        for i, (e, a) in enumerate(zip(exp, act)):
            out += _regime_diff(e, a, f"{path}[{i}]")
        return out
    if isinstance(exp, dict):
        if not isinstance(act, dict):
            return [f"{path}: dict vs {type(act).__name__}"]
        out = []
        if set(exp) != set(act):
            out.append(f"{path}: keys {sorted(exp)} != {sorted(act)}")
        for k in set(exp) & set(act):
            out += _regime_diff(exp[k], act[k], f"{path}.{k}")
        return out
    return [f"{path}: unhandled {type(exp).__name__}"]


try:
    with open(os.path.join(_PAR, "fixture.json"), encoding="utf-8") as _fh:
        _cases = json.load(_fh)["cases"]
    with open(os.path.join(_PAR, "expected.json"), encoding="utf-8") as _fh:
        _expected = json.load(_fh)
except OSError as _e:
    check_true("regime parity fixtures present", False, str(_e))
    _cases, _expected = [], {}

_by_fn = {}
_mismatch_examples = []
for _c in _cases:
    _act = _regime_run_case(_c["fn"], _c["args"])
    _mism = _regime_diff(_expected[_c["id"]], _act, _c["id"])
    _by_fn.setdefault(_c["fn"], [0, 0])
    _by_fn[_c["fn"]][0] += 0 if _mism else 1
    _by_fn[_c["fn"]][1] += 1
    if _mism and len(_mismatch_examples) < 8:
        _mismatch_examples += _mism[:2]
for _fn in sorted(_by_fn):
    _ok, _tot = _by_fn[_fn]
    check_true(f"regime parity: {_fn} ({_ok}/{_tot} cases)", _ok == _tot,
               "" if _ok == _tot else "; ".join(_mismatch_examples[:4]))
check_true(f"regime parity: all {len(_cases)} fixture cases match the JS oracle",
           bool(_cases) and all(v[0] == v[1] for v in _by_fn.values()))

# --------------------------------------------------------------------------
# staleness guard (2026-09-13): a delisted name is DEAD, not flat
#
# PBS printed no bar after 2026-07-17. fetch_prices logged "[FAIL] PBS no daily
# bars returned" every day and exited 0 (it only fails on a total wipeout), so
# build_panel forward-filled the last close across every later SPY session and
# the ladder ranked eight weeks of flat line in the top third on 21d -- r5 0.0,
# r21 0.0 -- because a dead ticker never goes down. These tests pin the three
# pieces of the fix on an in-memory database, so they need no network and no
# prices.db.
# --------------------------------------------------------------------------
print("\nstaleness guard -- a name that stopped printing is emitted nowhere")

import sqlite3 as _sq


def _stale_db(last_alive="2026-07-17", n=400):
    """SPY runs to the end of the axis; DEAD stops at `last_alive`; LIVE keeps
    printing. Prices rise monotonically so a forward-filled DEAD would look
    merely flat, never negative -- exactly the PBS shape."""
    import datetime as _dt
    conn = _sq.connect(":memory:")
    conn.execute("CREATE TABLE daily_prices (ticker TEXT, date TEXT, open REAL, "
                 "high REAL, low REAL, close REAL, volume REAL, adj_close REAL)")
    d = _dt.date(2025, 3, 3)
    axis = []
    while len(axis) < n:
        if d.weekday() < 5:
            axis.append(d.isoformat())
        d += _dt.timedelta(days=1)
    for i, day in enumerate(axis):
        px = 100.0 + i * 0.1
        for tk in ("SPY", "LIVE"):
            conn.execute("INSERT INTO daily_prices VALUES (?,?,?,?,?,?,?,?)",
                         (tk, day, px, px, px, px, 1e6, px))
        if day <= last_alive:
            conn.execute("INSERT INTO daily_prices VALUES (?,?,?,?,?,?,?,?)",
                         ("DEAD", day, px, px, px, px, 1e6, px))
    conn.commit()
    return conn, axis


_conn, _axis = _stale_db()
_last_dead = max(d for d in _axis if d <= "2026-07-17")
_behind = RG.sessions_behind(_axis, _last_dead)
check_true("sessions_behind counts SESSIONS, not calendar days", _behind > 5,
           f"DEAD is {_behind} sessions behind")
check("a live name is 0 sessions behind", RG.sessions_behind(_axis, _axis[-1]), 0)
check("a weekend does not make a name stale",
      RG.sessions_behind(_axis, _axis[-1]) <= RG.MAX_STALE_SESSIONS, True)

_dates, _series = RG.build_panel(_conn)
check_true("build_panel stops forward-filling a dead name",
           _series["DEAD"][-1] is None)
check_true("build_panel keeps filling a live name", _series["LIVE"][-1] is not None)
check("the fill stops exactly MAX_STALE_SESSIONS past the last real bar",
      sum(1 for v in _series["DEAD"] if v is not None)
      - sum(1 for d in _axis if d <= _last_dead), RG.MAX_STALE_SESSIONS)
check_true("the old unbounded fill is still reachable for comparison",
           RG.build_panel(_conn, max_stale=None)[1]["DEAD"][-1] is not None)

_etfs = [{"t": "LIVE", "name": "Live", "side": None},
         {"t": "DEAD", "name": "Dead", "side": None},
         {"t": "SPY", "name": "Ref", "side": None}]
_lad = RG.sector_ladder(_series, _etfs)
_row = {r["t"]: r for r in _lad["rows"]}
check("a stale sled is flagged stale", _row["DEAD"].get("stale"), True)
check("a stale sled gets no third", _row["DEAD"]["third"], None)
check("a stale sled gets no 21d third", _row["DEAD"]["third21"], None)
check("a stale sled gets no streak", _row["DEAD"]["streak"], 0)
check_true("a live sled is not flagged", "stale" not in _row["LIVE"])
check_true("a live sled still gets a third", _row["LIVE"]["third"] is not None)

# The key must be ABSENT (not False) on a healthy row: that is what keeps the
# emitted shape byte-identical to the regime_core.js oracle the parity fixtures
# were frozen from.
check_true("the stale key is absent, never False, on every healthy row",
           all("stale" not in r for r in _lad["rows"] if r["t"] != "DEAD"))

# A dead GROUP PRIMARY must hand the sleeve to the next live member rather than
# taking the whole group out of the field with it.
_grp = RG.sector_ladder(_series, [
    {"t": "DEAD", "name": "Dead primary", "side": None, "grp": "g"},
    {"t": "LIVE", "name": "Live twin", "side": None, "grp": "g"},
    {"t": "SPY", "name": "Ref", "side": None}])
_grow = {r["t"]: r for r in _grp["rows"]}
check_true("a dead group primary hands the sleeve to the next live member",
           _grow["LIVE"]["third"] is not None and _grow["LIVE"]["twin_of"] is None)

_conn.close()

print("\nstaleness guard -- technicals.build skips a stale name")
import tempfile as _tf

_tmp = _tf.mkdtemp()
_dbp = os.path.join(_tmp, "stale.db")
_fconn, _faxis = _stale_db()
_disk = _sq.connect(_dbp)
_fconn.backup(_disk)
_disk.close()
_fconn.close()
_tkp = os.path.join(_tmp, "tickers.txt")
with open(_tkp, "w", encoding="utf-8") as _fh:
    _fh.write("# --- Reference ---\nSPY\nLIVE\nDEAD\n")

_out = T.build(db=_dbp, tickers_file=_tkp)
_skip = {s["ticker"]: s for s in _out["skipped"]}
check_true("DEAD is skipped, not emitted", "DEAD" not in _out["tickers"])
check("DEAD is skipped for the right reason", _skip.get("DEAD", {}).get("reason"), "stale")
check_true("the skip row says how far behind it is",
           _skip.get("DEAD", {}).get("sessions_behind", 0) > RG.MAX_STALE_SESSIONS)
check_true("LIVE is still emitted", "LIVE" in _out["tickers"])
check_true("a stale name is reported, never silently dropped",
           "DEAD" in _skip and _skip["DEAD"].get("last_date") is not None)
import shutil as _sh
_sh.rmtree(_tmp, ignore_errors=True)

# --------------------------------------------------------------------------
# style-box and bond-category ladders (9/15/26, Jake: "add style boxes to the
# sector rotation tracking -- large cap (value/growth/core) down to micro caps
# -- also major bond categories/proxies/indices")
#
# Two SIBLING ladders beside the sector one, each ranked in its own field. The
# regression these tests exist to catch is leakage between the fields: a `field`
# key reaching a sector-ladder row, or the new rows re-thirding the ~80-row
# sector field.
# --------------------------------------------------------------------------
print("\nstyle-box / bond-category ladders (9/15/26)")

_SIDES = (None, "offense", "defense")
for _nm, _lst, _fld, _n in (("REG_STYLE", RG.REG_STYLE, "style", 10),
                            ("REG_BONDS", RG.REG_BONDS, "bonds", 14)):
    check(f"{_nm} row count", len(_lst), _n)
    check_true(f"{_nm}: every row has exactly t/name/side/sector/field",
               all(set(e) == {"t", "name", "side", "sector", "field"} for e in _lst))
    check_true(f"{_nm}: side is only offense / defense / None",
               all(e["side"] in _SIDES for e in _lst))
    check_true(f"{_nm}: every row carries field={_fld!r}",
               all(e["field"] == _fld for e in _lst))
    check_true(f"{_nm}: tickers are unique", len({e["t"] for e in _lst}) == len(_lst))
    check_true(f"{_nm}: names are unique", len({e["name"] for e in _lst}) == len(_lst))
    check_true(f"{_nm}: no entry smuggles in a grp/basket/gics key",
               all(not ({"grp", "basket", "gics"} & set(e)) for e in _lst))

# Separate fields never dedup against each other: IWM is small-cap CORE in the
# style boxes and "Small caps" in the sector ladder, and the four macro-leg bond
# proxies are reused rather than cloned.
check_true("IWM sits in the sector ladder AND the style boxes",
           any(e["t"] == "IWM" for e in RG.REG_ETFS)
           and any(e["t"] == "IWM" for e in RG.REG_STYLE))
check_true("REG_BONDS reuses the macro-leg proxies (TLT/IEF/HYG/LQD)",
           {"TLT", "IEF", "HYG", "LQD"} <= {e["t"] for e in RG.REG_BONDS})
check_true("the style boxes span large -> micro",
           {e["sector"] for e in RG.REG_STYLE}
           == {"Large cap", "Mid cap", "Small cap", "Micro cap"})

# A member with no series yet is ABSENT from `rows` (sector_ladder's `present`
# filter), never a row of Nones -- so a row appearing is itself the signal that
# the price backfill landed. `_series` here holds SPY/LIVE/DEAD only.
check("a ladder whose members have no series yet is empty, not rows of Nones",
      RG.sector_ladder(_series, RG.REG_STYLE)["rows"], [])

# THE REGRESSION THAT MATTERS. `field` is copied onto a row only when the etf
# entry sets it (exactly like `stale`), and ladder_payload appends the key only
# for the two new ladders -- so `regime.ladder` is byte-identical to what the
# pre-9/15 inline emitter produced, key set AND key order.
_OLD_LADDER_KEYS = ["t", "name", "side", "r5", "r21", "r63", "r126", "blend",
                    "third", "third21", "streak", "twin_of", "sector", "gics",
                    "basket", "members"]


def _rnd(v, d=6):
    return None if v is None else round(v, d)


check_true("no sector-ladder row carries a `field` key",
           all("field" not in r for r in _lad["rows"]))
_pay = RG.ladder_payload(_lad, _rnd)
# verbatim copy of the emitter that shipped before the two ladders existed
_frozen = {"rows": [{
    "t": r["t"], "name": r["name"], "side": r["side"],
    "r5": _rnd(r.get("r5")), "r21": _rnd(r["r21"]), "r63": _rnd(r["r63"]), "r126": _rnd(r["r126"]),
    "blend": _rnd(r["blend"]), "third": r["third"], "third21": r["third21"],
    "streak": r["streak"], "twin_of": r["twin_of"],
    "sector": r.get("sector"), "gics": r.get("gics"), "basket": r.get("basket", False),
    "members": r.get("members"),
} for r in _lad["rows"]], "divergence": _lad["divergence"]}
check_true("the ladder payload is BYTE-IDENTICAL to the pre-9/15 emitter",
           json.dumps(_pay) == json.dumps(_frozen),
           f"{len(_pay['rows'])} rows compared, {len(json.dumps(_pay))} bytes")
check_true("and its key order is the frozen one",
           all(list(r.keys()) == _OLD_LADDER_KEYS for r in _pay["rows"]))
check_true("with_field appends `field` last, and only when asked",
           all(list(r.keys()) == _OLD_LADDER_KEYS + ["field"]
               for r in RG.ladder_payload(_lad, _rnd, with_field=True)["rows"]))


# Construction check on a hand-built six-box panel: known trajectories, so the
# blend order, the thirds (ceil(6/3) = 2 rows each end) and the divergence tell
# are all predictable.
def _ramp(n, per_day):
    v, out = 100.0, []
    for _ in range(n):
        out.append(v)
        v *= (1.0 + per_day)
    return out


_fx = {t: _ramp(300, r) for t, r in
       (("A", 0.0030), ("B", 0.0025), ("C", 0.0018),
        ("D", 0.0012), ("E", 0.0006), ("F", 0.0001))}
_fxe = [{"t": "A", "name": "Large growth", "side": "offense", "sector": "Large cap", "field": "style"},
        {"t": "B", "name": "Large value", "side": "defense", "sector": "Large cap", "field": "style"},
        {"t": "C", "name": "Large core", "side": None, "sector": "Large cap", "field": "style"},
        {"t": "D", "name": "Mid core", "side": None, "sector": "Mid cap", "field": "style"},
        {"t": "E", "name": "Small growth", "side": "offense", "sector": "Small cap", "field": "style"},
        {"t": "F", "name": "Small value", "side": "defense", "sector": "Small cap", "field": "style"}]
_fl = RG.sector_ladder(_fx, _fxe)
check("the fixture ladder sorts by the 63/126 blend, best first",
      [r["t"] for r in _fl["rows"]], ["A", "B", "C", "D", "E", "F"])
check("thirds put the two leaders on top",
      [r["third"] for r in _fl["rows"]], ["top", "top", "mid", "mid", "bottom", "bottom"])
check_true("every fixture row carries its field",
           all(r["field"] == "style" for r in _fl["rows"]))
check_true("every fixture row has numeric r21/r63/r126/blend",
           all(isinstance(r[k], float) for r in _fl["rows"]
               for k in ("r21", "r63", "r126", "blend")))
check_true("the leader's streak counts sessions, not rows", _fl["rows"][0]["streak"] > 1)
check("offense and defense both in the top third fires the tell", _fl["divergence"], True)
# same tape, both leaders on the same side: no tell.
_fl2 = RG.sector_ladder(_fx, [dict(e, side=("offense" if e["t"] in ("A", "B") else e["side"]))
                              for e in _fxe])
check("two leaders on the SAME side is not a divergence", _fl2["divergence"], False)
check_true("re-siding does not move the field", [r["t"] for r in _fl2["rows"]]
           == [r["t"] for r in _fl["rows"]])


# --------------------------------------------------------------------------
# #91 (9/15/26): the bond field on EXCESS RETURN.
#
# The bug these tests pin: ranking fourteen bond ETFs on TOTAL return mostly
# ranks them by DURATION, because five of them are one factor at five
# maturities and the other nine each carry a duration of their own. Pull the
# Treasuries out into a curve strip and rank the rest on excess return over a
# duration-matched Treasury, and a parallel rate move -- the thing that used to
# BE the ranking -- has to leave the field completely unmoved. That is the
# central test below, and it is the one that would have caught the bug.
# --------------------------------------------------------------------------
print("\nbond field on excess return (#91, 9/15/26)")


def _ex(v, d=6):
    """Identity rounding shim -- build_regime's round_floats=False path. The
    excess-return invariants are exact to ~1e-16 and 6dp would hide them."""
    return v


def _walk(seed, n=260, vol=0.004):
    """A distinct random price path per rung, so an interpolation test cannot
    pass by accident on parallel lines."""
    rng = random.Random(seed)
    v, out = 100.0, []
    for _ in range(n):
        out.append(v)
        v *= 1.0 + vol * (rng.random() - 0.5)
    return out


def _shock_panel(moves, n=300):
    """Every ticker flat at 100 with ONE multiplicative move on the LAST bar,
    so r5 == r21 == r63 == r126 == blend == that move, exactly."""
    return {t: [100.0] * (n - 1) + [100.0 * (1.0 + m)] for t, m in moves.items()}


def _rebased(c):
    return [None if v is None else v / c[0] for v in c]


def _close(a, b, tol=1e-12):
    return all(x is not None and y is not None and abs(x - y) <= tol
               for x, y in zip(a, b))


# ---- config ----
check("BOND_DURATION covers exactly the REG_BONDS universe",
      sorted(RG.BOND_DURATION), sorted(e["t"] for e in RG.REG_BONDS))
check("TREASURY_RUNGS is the five-point curve strip",
      RG.TREASURY_RUNGS, ["BIL", "SHY", "IEF", "TLH", "TLT"])
check_true("the rungs are listed in maturity order",
           [RG.BOND_DURATION[t] for t in RG.TREASURY_RUNGS]
           == sorted(RG.BOND_DURATION[t] for t in RG.TREASURY_RUNGS))
check("the ranked field is REG_BONDS minus the rungs", len(RG.REG_BOND_SPREAD), 9)
check_true("no Treasury rung is left in the ranked field",
           not ({e["t"] for e in RG.REG_BOND_SPREAD} & set(RG.TREASURY_RUNGS)))
check_true("REG_BONDS is unchanged -- still the 14-ticker universe",
           len(RG.REG_BONDS) == 14
           and {e["t"] for e in RG.REG_BOND_SPREAD} | set(RG.TREASURY_RUNGS)
           == {e["t"] for e in RG.REG_BONDS})
check("BKLN's label names the rung, not a 0.2y interpolation",
      RG.match_label(RG.BOND_DURATION["BKLN"]), "vs BIL")
check("HYG's label names the interpolated point",
      RG.match_label(RG.BOND_DURATION["HYG"]), "vs ~3.3y UST (SHY/IEF)")

# ---- matched_treasury_series ----
_rp = {t: _walk(191 + k) for k, t in enumerate(RG.TREASURY_RUNGS)}

for _t in RG.TREASURY_RUNGS:
    _m = RG.matched_treasury_series(_rp, RG.BOND_DURATION[_t])
    check_true(f"a match at {_t}'s own duration reproduces {_t} (w = 0 or 1)",
               _close(_m, _rebased(_rp[_t])),
               f"w={RG._bracket_rungs(RG.BOND_DURATION[_t])[2]}")

_mid = (RG.BOND_DURATION["SHY"] + RG.BOND_DURATION["IEF"]) / 2.0
_mb = RG._bracket_rungs(_mid)
check("the midpoint of a bracket is bracketed by the two rungs", _mb[:2], ("SHY", "IEF"))
check("...and weighted half and half", _mb[2], 0.5, tol=1e-12)
_mm = RG.matched_treasury_series(_rp, _mid)
_want = [1.0]
for _i in range(1, len(_rp["SHY"])):
    _want.append(_want[-1] * (1.0 + 0.5 * (_rp["SHY"][_i] / _rp["SHY"][_i - 1] - 1.0)
                                   + 0.5 * (_rp["IEF"][_i] / _rp["IEF"][_i - 1] - 1.0)))
check_true("a midpoint match interpolates the two rungs' DAILY returns",
           _close(_mm, _want))
check_true("and it is not either rung on its own",
           not _close(_mm, _rebased(_rp["SHY"]), 1e-6)
           and not _close(_mm, _rebased(_rp["IEF"]), 1e-6))

check("below BIL the bracket clamps to BIL", RG._bracket_rungs(0.01), ("BIL", "BIL", 0.0))
check("above TLT the bracket clamps to TLT", RG._bracket_rungs(30.0), ("TLT", "TLT", 1.0))
check_true("a clamped-short match IS BIL",
           _close(RG.matched_treasury_series(_rp, 0.01), _rebased(_rp["BIL"])))
check_true("a clamped-long match IS TLT",
           _close(RG.matched_treasury_series(_rp, 30.0), _rebased(_rp["TLT"])))
check("a match with no rung series at all is None, never a flat line",
      RG.matched_treasury_series({}, 7.0), None)

# A session either rung missed has NO matched level -- it must not silently
# become a zero return, which is a real answer here.
_gap = {t: list(c) for t, c in _rp.items()}
_gap["IEF"][100] = None
check("a session a bracketing rung missed has no matched level",
      RG.matched_treasury_series(_gap, 4.6)[100], None)

# ---- THE TEST THIS ITEM EXISTS FOR ----
# A pure parallel rate move: every ETF, Treasuries included, moves by
# -duration x dy on the same day. That is exactly what the old total-return
# ladder was ranking. After the match it must leave every spread row at zero.
_DY = 0.0025      # +25bp parallel
_par = _shock_panel({t: -d * _DY for t, d in RG.BOND_DURATION.items()})
_pb, _pc = RG.bond_field(_par, _ex)
check("a parallel shock still leaves all nine spread rows", len(_pb["rows"]), 9)
check_true("a parallel rate shock produces ZERO excess return, every row",
           all(abs(r["r63"]) < 1e-9 for r in _pb["rows"]),
           "max |excess r63| = "
           f"{max(abs(r['r63']) for r in _pb['rows']):.2e}")
check_true("...on 21d and 126d too",
           all(abs(r["r21"]) < 1e-9 and abs(r["r126"]) < 1e-9 for r in _pb["rows"]))
check_true("...while the TOTAL return it strips is not zero at all",
           all(abs(r["tr63"]) > 1e-4 for r in _pb["rows"]))
# The same inputs through the OLD arithmetic: a duration ranking wearing a
# credit ranking's clothes.
_tot = RG.sector_ladder(_par, RG.REG_BONDS)
check("the same inputs ranked on TOTAL return are in duration order",
      [RG.BOND_DURATION[r["t"]] for r in _tot["rows"]],
      sorted(RG.BOND_DURATION.values()))
check_true("which is the bug: the old field's top third was just the short end",
           {r["t"] for r in _tot["rows"][:3]} <= {"BIL", "BKLN", "SHY", "HYG"})

# ---- curve strip + its read ----
_CURVE_ROW_KEYS = ["t", "name", "dur", "r5", "r21", "r63", "r126", "blend",
                   "dy21", "dy63", "dy126"]
check("the curve strip carries the five rungs", len(_pc["rows"]), 5)
check("the curve strip is in maturity order",
      [r["t"] for r in _pc["rows"]], RG.TREASURY_RUNGS)
check_true("every curve row has the stated keys, in order",
           all(list(r.keys()) == _CURVE_ROW_KEYS for r in _pc["rows"]))
check("dy is the duration-1 inversion of the total return, in bp",
      _pc["rows"][4]["dy63"], round(-_pc["rows"][4]["r63"] / 16.5 * 10000))
check_true("a parallel shock reads as a parallel shock on the strip",
           len({r["dy63"] for r in _pc["rows"]}) == 1,
           f"dy63 = {_pc['rows'][0]['dy63']}bp at every point")


def _curve_read(ief, shy, tlt, tlh=0.0, bil=0.0):
    return RG.bond_field(_shock_panel(
        {"BIL": bil, "SHY": shy, "IEF": ief, "TLH": tlh, "TLT": tlt}), _ex)[1]["read"]


# rallying = total return POSITIVE = yields fell. Steepening = the long end's
# yield move MINUS the short end's is positive (the long end sold off relatively).
for _nm, _args, _want_shape in (
        ("bull steepener", dict(ief=0.010, shy=0.005, tlt=0.010), "bull steepener"),
        ("bull flattener", dict(ief=0.010, shy=0.002, tlt=0.080), "bull flattener"),
        ("bear steepener", dict(ief=-0.020, shy=-0.001, tlt=-0.050), "bear steepener"),
        ("bear flattener", dict(ief=-0.010, shy=-0.005, tlt=-0.020), "bear flattener"),
        ("flat (duration inside its dead zone)", dict(ief=0.001, shy=0.005, tlt=0.010), "flat"),
        ("flat (curve inside its dead zone)", dict(ief=0.010, shy=0.010, tlt=0.087), "flat")):
    check(f"curve shape: {_nm}", _curve_read(**_args)["shape"], _want_shape)

check("duration: a belly rally", _curve_read(ief=0.010, shy=0.0, tlt=0.0)["duration"], "rallying")
check("duration: a belly sell-off", _curve_read(ief=-0.010, shy=0.0, tlt=0.0)["duration"], "selling off")
check("duration: inside +/-0.5% is flat", _curve_read(ief=0.004, shy=0.0, tlt=0.0)["duration"], "flat")
check("curve: steepening", _curve_read(ief=-0.020, shy=-0.001, tlt=-0.050)["curve"], "steepening")
check("curve: flattening", _curve_read(ief=-0.010, shy=-0.005, tlt=-0.020)["curve"], "flattening")
check("curve: inside +/-10bp is flat", _curve_read(ief=0.010, shy=0.010, tlt=0.087)["curve"], "flat")
_gr = _curve_read(ief=-0.020, shy=-0.001, tlt=-0.050)
check("gap_bp IS the dy63 difference it judged on", _gr["gap_bp"],
      round(0.050 / 16.5 * 10000) - round(0.001 / 1.9 * 10000))
check_true("the curve read has exactly the stated keys",
           list(_gr.keys()) == ["duration", "curve", "shape", "gap_bp"])


# ---- credit read + the quadrant ----
def _bond_panel(rung_moves, excess):
    """A panel in which each spread sleeve's EXCESS return is exactly `excess`.
    Solved through the real bracket weights: a one-day move of
    (1+x)(1+m_matched)-1 on a flat history gives excess return x on the nose."""
    mv = dict(rung_moves)
    for e in RG.REG_BOND_SPREAD:
        lo, hi, w = RG._bracket_rungs(RG.BOND_DURATION[e["t"]])
        m = (1.0 - w) * rung_moves[lo] + w * rung_moves[hi]
        mv[e["t"]] = (1.0 + excess.get(e["t"], 0.0)) * (1.0 + m) - 1.0
    return _shock_panel(mv)


_FLAT_RUNGS = {t: 0.0 for t in RG.TREASURY_RUNGS}


def _credit(x, rungs=None):
    ex = {t: x for t in RG.CREDIT_SLEEVES}
    b, c = RG.bond_field(_bond_panel(rungs or _FLAT_RUNGS, ex), _ex)
    return b, c


_b, _c = _credit(0.010)
check_true("the constructed excess return comes back EXACTLY",
           all(abs(r["r63"] - 0.010) < 1e-12 for r in _b["rows"]
               if r["t"] in RG.CREDIT_SLEEVES))
check("credit: the four spread sleeves up on a still curve is tightening",
      _b["read"]["credit"], "tightening")
check("credit: down is widening", _credit(-0.010)[0]["read"]["credit"], "widening")
check("credit: inside +/-0.25% is flat", _credit(0.002)[0]["read"]["credit"], "flat")
check_true("the ladder read has exactly the stated keys",
           list(_b["read"].keys()) == ["credit", "duration", "quadrant"])
check("a flat curve leaves duration flat, so the quadrant is mixed",
      _b["read"]["quadrant"], "mixed")

# Both axes live. IEF carries the duration read; the sleeves' excess is solved
# against the matched Treasury, so the credit read is independent of it.
_RALLY = {"BIL": 0.0, "SHY": 0.004, "IEF": 0.015, "TLH": 0.020, "TLT": 0.025}
_SELL = {t: -v for t, v in _RALLY.items()}
for _nm, _rungs, _x, _want_q in (
        ("goldilocks / disinflation", _RALLY, 0.010, "goldilocks / disinflation"),
        ("growth scare", _RALLY, -0.010, "growth scare"),
        ("inflation shock (2022 shape)", _SELL, -0.010, "inflation shock (2022 shape)"),
        ("reflation", _SELL, 0.010, "reflation"),
        ("mixed (credit inside its dead zone)", _RALLY, 0.001, "mixed")):
    _qb, _qc = _credit(_x, _rungs)
    check(f"quadrant: {_nm}", _qb["read"]["quadrant"], _want_q)
    check_true(f"...and the ladder read copies the curve's duration ({_nm})",
               _qb["read"]["duration"] == _qc["read"]["duration"])

# ---- payload shape ----
_BOND_ROW_KEYS = _OLD_LADDER_KEYS + ["field", "tr21", "tr63", "tr126",
                                     "trblend", "dur", "match"]
_sb, _sc = RG.bond_field(_bond_panel(_RALLY, {t: 0.01 for t in RG.CREDIT_SLEEVES}), _rnd)
check("bond_ladder carries the nine spread rows", len(_sb["rows"]), 9)
check_true("every spread row has the stated keys, in the stated order",
           all(list(r.keys()) == _BOND_ROW_KEYS for r in _sb["rows"]))
check_true("bond_ladder has exactly rows/divergence/read/spread63/spread_blend",
           list(_sb.keys()) == ["rows", "divergence", "read", "spread63", "spread_blend"])
check_true("bond_curve has exactly rows/read", list(_sc.keys()) == ["rows", "read"])
check_true("every spread row carries its duration and its match label",
           all(r["dur"] == RG.BOND_DURATION[r["t"]]
               and r["match"] == RG.match_label(r["dur"]) for r in _sb["rows"]))
check_true("every spread row still carries field='bonds'",
           all(r["field"] == "bonds" for r in _sb["rows"]))
check_true("the rows are sorted by the EXCESS blend, best first",
           [r["blend"] for r in _sb["rows"]]
           == sorted((r["blend"] for r in _sb["rows"]), reverse=True))
check("spread_blend is the field's dispersion, in percentage points",
      _sb["spread_blend"],
      round((max(r["blend"] for r in _sb["rows"])
             - min(r["blend"] for r in _sb["rows"])) * 100.0, 4))
check("spread63 likewise, on the 63d excess return", _sb["spread63"],
      round((max(r["r63"] for r in _sb["rows"])
             - min(r["r63"] for r in _sb["rows"])) * 100.0, 4))
check_true("tr* is the TOTAL return, and it is not the excess one",
           all(abs(r["tr63"] - r["r63"]) > 1e-6 for r in _sb["rows"]))

# A bond field whose ETFs have no series yet is EMPTY on both halves -- never
# rows of Nones, and never a read invented out of nothing.
_eb, _ec = RG.bond_field({"SPY": [100.0] * 300}, _rnd)
check("no bond series yet -> no spread rows", _eb["rows"], [])
check("no bond series yet -> no curve rows", _ec["rows"], [])
check("no bond series yet -> no curve shape claimed", _ec["read"]["shape"], "flat")
check("no bond series yet -> no quadrant claimed", _eb["read"]["quadrant"], "mixed")
check("no bond series yet -> no field spread", _eb["spread63"], None)

# ---- and the two neighbouring ladders are untouched ----
# The sector one is byte-compared above; the style one is the other ladder that
# must not have moved, and #91 touched the function they both go through.
_stylep = RG.ladder_payload(_fl, _rnd, with_field=True)
_style_frozen = {"rows": [{
    "t": r["t"], "name": r["name"], "side": r["side"],
    "r5": _rnd(r.get("r5")), "r21": _rnd(r["r21"]), "r63": _rnd(r["r63"]), "r126": _rnd(r["r126"]),
    "blend": _rnd(r["blend"]), "third": r["third"], "third21": r["third21"],
    "streak": r["streak"], "twin_of": r["twin_of"],
    "sector": r.get("sector"), "gics": r.get("gics"), "basket": r.get("basket", False),
    "members": r.get("members"), "field": r.get("field"),
} for r in _fl["rows"]], "divergence": _fl["divergence"]}
check_true("the STYLE ladder payload is byte-identical after #91",
           json.dumps(_stylep) == json.dumps(_style_frozen),
           f"{len(_stylep['rows'])} rows, {len(json.dumps(_stylep))} bytes")
check_true("and the sector ladder is still byte-identical after #91",
           json.dumps(RG.ladder_payload(_lad, _rnd)) == json.dumps(_frozen))
check_true("neither neighbouring ladder learned a bond key",
           all(not ({"tr63", "dur", "match"} & set(r))
               for r in _stylep["rows"] + RG.ladder_payload(_lad, _rnd)["rows"]))

print("\n" + ("-" * 60))
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all tests passed")
