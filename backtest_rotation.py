#!/usr/bin/env python3
"""
backtest_rotation.py -- walk-forward backtest of the rules the Technicals tab
publishes, on the ETF universe that actually has price history.

Jake, 2026-09-16: "should add technicals in there too, maybe what returns look
like if I sector/regime rotate using those signals".

WHAT THIS IS
------------
`eng-docs/recon/metrics-eval-2026-09-16.md` graded the PER-NAME board metrics
(anchors, DCF, filing-delta, insider, earnings) and found almost all of them
too young to test. This file does the other half: the RULES -- the sector
rotation ladder, the offense/defense divergence tell, the risk-appetite
composite, the style ladder and the #91 excess-return bond ladder -- tested on
the ETF field they are computed over, which is the one part of the stack with
enough history to answer at all.

NOTHING IS RE-IMPLEMENTED. Every rule is evaluated by calling the SAME
functions `technicals.py` calls to build `technicals.json.regime`:
`regime.build_panel`, `regime.sector_ladder`, `regime.basket_series`,
`regime.build_regime` (for the composite state series), `regime.excess_series`
/ `regime.matched_treasury_series` / `regime.bond_curve_rows` /
`regime.bond_curve_read` for #91, and the `REG_ETFS` / `REG_STYLE` /
`REG_BONDS` / `BOND_DURATION` configs. A rule that needs the full series dict
is fed a HISTORICAL WINDOW -- `series[:i+1]` -- so the answer it returns is the
answer the tab would have printed on date i, and nothing after i can reach it.

NO LOOK-AHEAD, stated exactly
-----------------------------
A decision taken on bar `d` (from a ladder / composite computed on bars
`0..d`) earns the returns of bars `d+1 .. d_next`. Bar `d`'s own return
belongs to the PREVIOUS decision. `test_backtest_rotation.py` pins this by
mutating every bar after `d` and asserting the decision set is unchanged.

CONVENTIONS (stated once, applied everywhere)
---------------------------------------------
* Equal weight, DAILY-REBALANCED inside a holding period -- the portfolio's
  daily return is the plain mean of its members' daily returns. This is
  exactly `regime.basket_series`' convention, which is what the ladder's own
  basket sleds already use, so the sled and the sleeve are measured the same
  way. Turnover therefore counts only MEMBERSHIP changes at rebalance dates.
* A sled with no bar on a session sits out that session (same rule again).
* An empty selection holds BIL where BIL has bars, 0.0% before that.
* Total (adjusted-close) returns throughout; no transaction costs, no taxes,
  no slippage -- the same assumption `technicals.backtest` makes, and the
  reason turnover is reported beside every result.
* Basket sleds (OPTICS / POWERSEMI / HYPERSCALE / NEOCLOUD / QUANTUM) are
  held as their equal-weight basket index. They are ladder rows, so a rule
  that picks them has to be able to hold them.

RULE #18: this file reads `prices.db` and `data/technicals.json` only. No
holdings, quantities or dollar positions are read, computed or emitted.

Usage
-----
    ./.venv/Scripts/python.exe backtest_rotation.py --stats
    ./.venv/Scripts/python.exe backtest_rotation.py \
        --out ../../eng-docs/analysis/rotation-backtest-2026-09-16.csv
    ./.venv/Scripts/python.exe backtest_rotation.py --selfcheck
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import regime  # noqa: E402  -- the published engine; never a copy of it

TRADING_DAYS = 252.0
END_DATE = "2026-09-15"

# Below this many rebalance periods a bootstrap has nothing to resample: the
# resampled means are drawn from a handful of distinct values and the p-value it
# prints is an artefact of the block structure, not a statement about the tape.
# Such rows are reported with their numbers and a "can't tell yet (n=..)" verdict.
MIN_PERIODS = 10

# The 11 GICS sector SPDRs. The selection-look-ahead control: this list was
# fixed by State Street in 1998 (XLC 2018, XLRE 2015), not by anyone who had
# seen the 2024-2026 tape, so rule 1 run on it alone carries no universe
# selection bias.
SPDR11 = ["XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"]


# ==========================================================================
# series / return plumbing
# ==========================================================================

def sled_series(series, etfs):
    """{ticker: the series the ladder ranks} -- a basket entry's equal-weight
    index, every other entry's own adjusted-close series. Same expression
    `regime.sector_ladder` uses internally, hoisted so the backtest can also
    compute the sled's REALISED returns."""
    out = {}
    for e in etfs:
        if e.get("basket"):
            out[e["t"]] = regime.basket_series(series, e["basket"])
        else:
            out[e["t"]] = series.get(e["t"])
    return out


def daily_returns(s):
    """Simple daily returns; None where either close is missing or the base is
    non-positive. Index i is the return EARNED on bar i (i-1 -> i)."""
    n = len(s) if s else 0
    out = [None] * n
    for i in range(1, n):
        a, b = s[i - 1], s[i]
        out[i] = (b / a - 1.0) if (a is not None and b is not None and a > 0) else None
    return out


def ew_bar_return(rets, members, i):
    """Equal-weight mean of the members that printed a return on bar i.
    Members with no bar sit out. 0.0 when nothing printed."""
    acc = 0.0
    cnt = 0
    for m in members:
        r = rets.get(m)
        if not r or i >= len(r):
            continue
        v = r[i]
        if v is not None:
            acc += v
            cnt += 1
    return (acc / cnt) if cnt else 0.0


def window(series, tickers, i):
    """The historical window a rule is allowed to see on bar i: every needed
    series truncated to bars 0..i inclusive."""
    return {t: (series[t][:i + 1] if series.get(t) else series.get(t)) for t in tickers}


def needed_tickers(etfs):
    need = set()
    for e in etfs:
        need.add(e["t"])
        for m in (e.get("basket") or []):
            need.add(m)
    return need


def first_field_idx(sleds, frac=0.95, lag=126, start=0):
    """First bar on which at least `frac` of the present sleds can compute a
    `lag`-bar return -- i.e. the first date the ladder's own 63/126 blend is a
    real number for the field rather than a fallback to r63. The honest start
    of the walk, and the date the memo quotes."""
    ts = [t for t, s in sleds.items() if s]
    if not ts:
        return None
    n = len(sleds[ts[0]])
    for i in range(max(lag, start), n):
        ok = sum(1 for t in ts if sleds[t][i] is not None and sleds[t][i - lag] is not None)
        if ok >= frac * len(ts):
            return i
    return None


def first_bar_idx(s):
    for i, v in enumerate(s or []):
        if v is not None:
            return i
    return None


# ==========================================================================
# portfolio engine
# ==========================================================================

def run_decisions(rets, decisions, end_idx, cash_rets=None):
    """Walk an equity curve from a list of (bar_index, [members]) decisions.

    A decision at bar d is effective for bars d+1..d_next INCLUSIVE. Bar d's
    own return still belongs to the previous decision -- that one-bar offset is
    the whole of the no-look-ahead guarantee on the return side.

    Returns (idx0, equity, period_returns, period_bounds, empty_periods) where
    equity[k] is the level at bar idx0+k and equity[0] == 1.0."""
    idx0 = decisions[0][0]
    eq = [1.0]
    bounds = []
    empty = 0
    for k, (d, members) in enumerate(decisions):
        nxt = decisions[k + 1][0] if k + 1 < len(decisions) else end_idx
        if nxt <= d:
            continue
        bounds.append((d, nxt))
        if not members:
            empty += 1
        for i in range(d + 1, nxt + 1):
            if members:
                r = ew_bar_return(rets, members, i)
            elif cash_rets is not None:
                r = cash_rets[i] if (i < len(cash_rets) and cash_rets[i] is not None) else 0.0
            else:
                r = 0.0
            eq.append(eq[-1] * (1.0 + r))
    per = []
    for (a, b) in bounds:
        per.append(eq[b - idx0] / eq[a - idx0] - 1.0)
    return idx0, eq, per, bounds, empty


def run_weighted(rets_a, rets_b, weights_by_bar, idx0, end_idx):
    """Two-asset exposure dial. weights_by_bar[i] is the weight on asset A for
    the return EARNED on bar i (so the caller must already have offset it by
    one bar). Returns (equity)."""
    eq = [1.0]
    for i in range(idx0 + 1, end_idx + 1):
        w = weights_by_bar[i]
        ra = rets_a[i] if rets_a[i] is not None else 0.0
        rb = (rets_b[i] if (rets_b and rets_b[i] is not None) else 0.0)
        eq.append(eq[-1] * (1.0 + w * ra + (1.0 - w) * rb))
    return eq


def buy_and_hold(rets, ticker, idx0, end_idx):
    eq = [1.0]
    r = rets[ticker]
    for i in range(idx0 + 1, end_idx + 1):
        v = r[i] if r[i] is not None else 0.0
        eq.append(eq[-1] * (1.0 + v))
    return eq


def ew_curve(rets, members, idx0, end_idx):
    eq = [1.0]
    for i in range(idx0 + 1, end_idx + 1):
        eq.append(eq[-1] * (1.0 + ew_bar_return(rets, members, i)))
    return eq


def period_returns(eq, idx0, bounds):
    return [eq[b - idx0] / eq[a - idx0] - 1.0 for (a, b) in bounds]


# ==========================================================================
# statistics
# ==========================================================================

def cagr(eq, n_sessions):
    if n_sessions <= 0 or eq[-1] <= 0:
        return None
    years = n_sessions / TRADING_DAYS
    return eq[-1] ** (1.0 / years) - 1.0


def ann_vol(eq):
    rs = [eq[i] / eq[i - 1] - 1.0 for i in range(1, len(eq))]
    if len(rs) < 2:
        return None
    m = sum(rs) / len(rs)
    var = sum((x - m) ** 2 for x in rs) / (len(rs) - 1)
    return math.sqrt(var) * math.sqrt(TRADING_DAYS)


def max_drawdown(eq):
    peak = eq[0]
    mdd = 0.0
    for v in eq:
        if v > peak:
            peak = v
        dd = v / peak - 1.0
        if dd < mdd:
            mdd = dd
    return mdd


def hit_rate(strat_per, bench_per):
    n = min(len(strat_per), len(bench_per))
    if not n:
        return None, 0
    w = sum(1 for i in range(n) if strat_per[i] > bench_per[i])
    return w / n, n


def turnover(decisions):
    """One-way turnover per rebalance: 0.5 * sum |w_new - w_old| over the union.
    Weights are equal inside each selection, and the daily rebalance inside a
    period keeps them equal, so this counts membership changes only."""
    outs = []
    prev = None
    for (_d, members) in decisions:
        cur = {t: 1.0 / len(members) for t in members} if members else {}
        if prev is not None:
            keys = set(cur) | set(prev)
            outs.append(0.5 * sum(abs(cur.get(k, 0.0) - prev.get(k, 0.0)) for k in keys))
        prev = cur
    return (sum(outs) / len(outs)) if outs else None


def median(a):
    return regime.median(a)


# ==========================================================================
# bootstrap
# ==========================================================================

def bootstrap(excess, B=10000, seed=20260916, block=1):
    """Resample the per-period excess returns and ask whether the mean is
    distinguishable from zero at this n. `block` > 1 is a circular block
    bootstrap (consecutive periods stay together, so serial dependence in the
    tape survives the resample). p is two-sided: 2*min(P(mean<=0), P(mean>=0)),
    capped at 1."""
    n = len(excess)
    if n < 3:
        return {"n": n, "mean": (sum(excess) / n if n else None), "p": None,
                "lo": None, "hi": None, "block": block, "B": 0}
    rng = random.Random(seed)
    means = []
    block = max(1, min(block, n // 4))   # a block bootstrap needs >=4 blocks to mean anything
    nb = int(math.ceil(n / float(block)))
    for _ in range(B):
        if block <= 1:
            s = sum(excess[rng.randrange(n)] for _ in range(n))
            means.append(s / n)
        else:
            acc = []
            for _b in range(nb):
                st = rng.randrange(n)
                for k in range(block):
                    acc.append(excess[(st + k) % n])
            acc = acc[:n]
            means.append(sum(acc) / len(acc))
    means.sort()
    obs = sum(excess) / n
    le = sum(1 for m in means if m <= 0.0) / float(B)
    ge = sum(1 for m in means if m >= 0.0) / float(B)
    p = min(1.0, 2.0 * min(le, ge))
    return {"n": n, "mean": obs, "p": p,
            "lo": means[int(0.025 * B)], "hi": means[int(0.975 * B) - 1],
            "block": block, "B": B}


def sign_test_pos(excess):
    n = len(excess)
    if not n:
        return None, 0
    return sum(1 for x in excess if x > 0), n


# ==========================================================================
# result assembly
# ==========================================================================

RESULT_FIELDS = [
    "rule", "strategy", "universe", "benchmark", "start", "end", "n_sessions",
    "n_decisions", "rebal_sessions", "avg_holdings", "empty_periods",
    "cagr", "vol", "max_dd", "hit_rate", "turnover_per_rebal", "turnover_ann",
    "bench_cagr", "bench_vol", "bench_max_dd", "excess_cagr", "dd_vs_bench",
    "mean_period_excess", "periods_beat", "boot_p_iid", "boot_p_block",
    "boot_ci_lo", "boot_ci_hi", "verdict",
]


def _r(v, d=4):
    return None if v is None else round(v, d)


def make_result(rule, name, universe, bench_name, dates, idx0, end_idx,
                eq, bench_eq, per, bench_per, decisions, rebal, empty=0,
                block=4, avg_hold=None):
    n_sessions = end_idx - idx0
    exc = [per[i] - bench_per[i] for i in range(min(len(per), len(bench_per)))]
    b_iid = bootstrap(exc, block=1)
    b_blk = bootstrap(exc, block=block)
    hr, hn = hit_rate(per, bench_per)
    tpr = turnover(decisions) if decisions else None
    per_year = (TRADING_DAYS / rebal) if rebal else None
    c = cagr(eq, n_sessions)
    bc = cagr(bench_eq, n_sessions)
    npos, nn = sign_test_pos(exc)
    if b_iid["p"] is None or len(exc) < MIN_PERIODS:
        verdict = "can't tell yet (n=%d)" % len(exc)
    elif b_iid["p"] < 0.05 and b_blk["p"] < 0.05:
        verdict = "distinguishable from noise"
    elif b_iid["p"] < 0.10:
        verdict = "marginal"
    else:
        verdict = "indistinguishable from noise"
    return {
        "rule": rule, "strategy": name, "universe": universe,
        "benchmark": bench_name,
        "start": dates[idx0], "end": dates[end_idx], "n_sessions": n_sessions,
        "n_decisions": len(decisions) if decisions else None,
        "rebal_sessions": rebal, "avg_holdings": _r(avg_hold, 2),
        "empty_periods": empty,
        "cagr": _r(c), "vol": _r(ann_vol(eq)), "max_dd": _r(max_drawdown(eq)),
        "hit_rate": _r(hr, 3),
        "turnover_per_rebal": _r(tpr, 3),
        "turnover_ann": _r(tpr * per_year, 2) if (tpr is not None and per_year) else None,
        "bench_cagr": _r(bc), "bench_vol": _r(ann_vol(bench_eq)),
        "bench_max_dd": _r(max_drawdown(bench_eq)),
        "excess_cagr": _r(c - bc) if (c is not None and bc is not None) else None,
        "dd_vs_bench": _r(max_drawdown(eq) - max_drawdown(bench_eq)),
        "mean_period_excess": _r(b_iid["mean"], 5),
        "periods_beat": ("%d/%d" % (npos, nn)) if nn else None,
        "boot_p_iid": _r(b_iid["p"], 4), "boot_p_block": _r(b_blk["p"], 4),
        "boot_ci_lo": _r(b_iid["lo"], 5), "boot_ci_hi": _r(b_iid["hi"], 5),
        "verdict": verdict,
    }


# ==========================================================================
# selectors -- each reads ONE ladder result, exactly as the tab renders it
# ==========================================================================

def field_rows(lad):
    """The ladder's own FIELD: non-twin, non-stale rows with a blend. Twins
    (SOXX beside SMH, GDXJ/RING beside GDX, ...) carry third=None by
    construction, so the dedup the tab does is the dedup the backtest gets."""
    return [r for r in lad["rows"]
            if r.get("twin_of") is None and not r.get("stale") and r.get("blend") is not None]


def sel_blend_top(lad):
    rows = field_rows(lad)                      # already sorted by blend desc
    k = int(math.ceil(len(rows) / 3.0))
    return [r["t"] for r in rows[:k]]


def sel_blend_bottom(lad):
    rows = field_rows(lad)
    k = int(math.ceil(len(rows) / 3.0))
    return [r["t"] for r in rows[len(rows) - k:]]


def sel_r63_streak5(lad):
    return [r["t"] for r in lad["rows"] if r.get("third") == "top" and (r.get("streak") or 0) >= 5]


def sel_r63_ex21bottom(lad):
    return [r["t"] for r in lad["rows"]
            if r.get("third") == "top" and r.get("third21") != "bottom"]


# ==========================================================================
# the walk
# ==========================================================================

def walk_ladder(series, etfs, start_idx, end_idx, rebal, need=None):
    """Compute the ladder on every rebalance bar, from that bar's HISTORICAL
    WINDOW only. Returns [(bar_index, ladder)]. One ladder, many selectors --
    the ladder is the expensive part and the rules differ only in how they read
    it."""
    need = need or needed_tickers(etfs)
    out = []
    i = start_idx
    while i <= end_idx:
        out.append((i, regime.sector_ladder(window(series, need, i), etfs)))
        i += rebal
    return out


def decisions_from(ladders, selector):
    return [(i, selector(lad)) for (i, lad) in ladders]


# ==========================================================================
# rules
# ==========================================================================

def rule1(ctx, results):
    """Sector ladder -- top third, three selection rules, two cadences, plus
    the bottom-third diagnostic sleeve."""
    series, dates = ctx["series"], ctx["dates"]
    end = ctx["end_idx"]
    sl = sled_series(series, regime.REG_ETFS)
    rets = {t: daily_returns(s) for t, s in sl.items() if s}
    start = first_field_idx(sl, frac=0.95, lag=126)
    ctx["rule1_start"] = start
    ctx["rule1_sleds"] = sl
    ctx["rule1_rets"] = rets
    need = needed_tickers(regime.REG_ETFS)
    bil = daily_returns(series.get("BIL"))
    spy_rets = daily_returns(series["SPY"])

    for rebal in (5, 21):
        ladders = walk_ladder(series, regime.REG_ETFS, start, end, rebal, need)
        if rebal == 21:
            ctx["ladders21"] = ladders
        if rebal == 5:
            ctx["ladders5"] = ladders
        allfield = sorted({r["t"] for (_i, lad) in ladders for r in field_rows(lad)})
        for label, selector in (
            ("top third by blend", sel_blend_top),
            ("top third by r63, streak>=5", sel_r63_streak5),
            ("top third by r63, ex bottom-third 21d", sel_r63_ex21bottom),
            ("bottom third by blend (diagnostic)", sel_blend_bottom),
        ):
            dec = decisions_from(ladders, selector)
            if rebal == 21 and selector is sel_blend_top:
                cnt = {}
                for (_d, ms) in dec:
                    for m in ms:
                        cnt[m] = cnt.get(m, 0) + 1
                ctx["rule1_picks"] = sorted(cnt.items(), key=lambda kv: -kv[1])
                ctx["rule1_field_size"] = len(field_rows(ladders[-1][1]))
            idx0, eq, per, bounds, empty = run_decisions(rets, dec, end, cash_rets=bil)
            avg_hold = sum(len(m) for (_d, m) in dec) / float(len(dec))
            for bname, beq in (("SPY", buy_and_hold({"SPY": spy_rets}, "SPY", idx0, end)),
                               ("EW universe", ew_curve(rets, allfield, idx0, end))):
                bper = period_returns(beq, idx0, bounds)
                results.append(make_result(
                    "1", "%s (%dd)" % (label, rebal), "REG_ETFS field (%d sleds)" % len(allfield),
                    bname, dates, idx0, end, eq, beq, per, bper, dec, rebal,
                    empty=empty, avg_hold=avg_hold))


def rule1_spdr(ctx, results):
    """The selection-look-ahead control: the same rule on the 11 GICS sector
    SPDRs, a list nobody in this project chose."""
    series, dates, end = ctx["series"], ctx["dates"], ctx["end_idx"]
    etfs = [e for e in regime.REG_ETFS if e["t"] in SPDR11]
    sl = sled_series(series, etfs)
    rets = {t: daily_returns(s) for t, s in sl.items() if s}
    start = first_field_idx(sl, frac=1.0, lag=126)
    spy_rets = daily_returns(series["SPY"])
    bil = daily_returns(series.get("BIL"))
    for rebal in (5, 21):
        ladders = walk_ladder(series, etfs, start, end, rebal)
        for label, selector in (("top third by blend", sel_blend_top),
                                ("bottom third by blend (diagnostic)", sel_blend_bottom)):
            dec = decisions_from(ladders, selector)
            idx0, eq, per, bounds, empty = run_decisions(rets, dec, end, cash_rets=bil)
            avg_hold = sum(len(m) for (_d, m) in dec) / float(len(dec))
            for bname, beq in (("SPY", buy_and_hold({"SPY": spy_rets}, "SPY", idx0, end)),
                               ("EW 11 SPDRs", ew_curve(rets, SPDR11, idx0, end))):
                bper = period_returns(beq, idx0, bounds)
                results.append(make_result(
                    "1c", "SPDR11 %s (%dd)" % (label, rebal), "11 GICS sector SPDRs",
                    bname, dates, idx0, end, eq, beq, per, bper, dec, rebal,
                    empty=empty, avg_hold=avg_hold))


def rule2(ctx, results):
    """Offense/defense divergence as a gate, both polarities."""
    series, dates, end = ctx["series"], ctx["dates"], ctx["end_idx"]
    rets = dict(ctx["rule1_rets"])
    rets["SPY"] = daily_returns(series["SPY"])
    bil = daily_returns(series.get("BIL"))
    for rebal, ladders in ((5, ctx["ladders5"]), (21, ctx["ladders21"])):
        allfield = sorted({r["t"] for (_i, lad) in ladders for r in field_rows(lad)})
        ndiv = sum(1 for (_i, lad) in ladders if lad["divergence"])
        for label, on_div in (("divergence -> SPY, else top third", True),
                              ("divergence -> top third, else SPY", False)):
            dec = []
            for (i, lad) in ladders:
                d = bool(lad["divergence"])
                hold_spy = (d == on_div)
                dec.append((i, ["SPY"] if hold_spy else sel_blend_top(lad)))
            idx0, eq, per, bounds, empty = run_decisions(rets, dec, end, cash_rets=bil)
            avg_hold = sum(len(m) for (_d, m) in dec) / float(len(dec))
            base_dec = decisions_from(ladders, sel_blend_top)
            _i0, base_eq, _bp, _bb, _be = run_decisions(rets, base_dec, end, cash_rets=bil)
            for bname, beq in (("SPY", buy_and_hold(rets, "SPY", idx0, end)),
                               ("ungated top third", base_eq),
                               ("EW universe", ew_curve(rets, allfield, idx0, end))):
                bper = period_returns(beq, idx0, bounds)
                results.append(make_result(
                    "2", "%s (%dd)" % (label, rebal),
                    "REG_ETFS field; divergence on %d/%d decisions" % (ndiv, len(ladders)),
                    bname, dates, idx0, end, eq, beq, per, bper, dec, rebal,
                    empty=empty, avg_hold=avg_hold))


def state_weight(st):
    return {"risk-on": 1.0, "neutral": 0.5, "defensive": 0.0}.get(st, 0.5)


def rule3(ctx, results):
    """The published composite as an exposure dial: 100 / 50 / 0% on SPY (and
    on QQQ), cash = BIL, re-set on every state change."""
    series, dates = ctx["series"], ctx["dates"]
    end = ctx["end_idx"]
    state = ctx["state"]
    bil = daily_returns(series.get("BIL"))
    bil0 = first_bar_idx(series.get("BIL"))
    fa = ctx["all_legs_idx"]

    for wname, start in (("2y window (cash = BIL)", bil0), ("full window (cash = 0%)", fa)):
        cash = bil if start >= bil0 else None
        for risk in ("SPY", "QQQ"):
            rr = daily_returns(series[risk])
            # weight applied to bar i comes from the state on bar i-1
            w = [0.0] * len(dates)
            for i in range(start + 1, end + 1):
                w[i] = state_weight(state[i - 1])
            eq = run_weighted(rr, cash, w, start, end)
            # THE control that decides whether rule 3 is timing or just less
            # equity: the same average exposure held CONSTANT. If the dial only
            # matches this, it is a de-risking knob, not a signal.
            avg_w = sum(w[start + 1:end + 1]) / float(end - start)
            flat_w = [avg_w] * len(dates)
            flat_eq = run_weighted(rr, cash, flat_w, start, end)
            bh_eq = buy_and_hold({risk: rr}, risk, start, end)
            # decision dates: the start bar plus every state change in the window
            dec_idx = [start] + [i for i in range(start + 1, end + 1) if state[i] != state[i - 1]]
            bounds = []
            for k, d in enumerate(dec_idx):
                nxt = dec_idx[k + 1] if k + 1 < len(dec_idx) else end
                if nxt > d:
                    bounds.append((d, nxt))
            per = period_returns(eq, start, bounds)
            # turnover: |change in exposure| at each state change
            dec = [(d, ["%s@%.0f" % (risk, state_weight(state[d]) * 100)]) for d in dec_idx]
            for bname, beq in (("%s buy-and-hold" % risk, bh_eq),
                               ("static %.0f%% %s" % (avg_w * 100, risk), flat_eq)):
                bper = period_returns(beq, start, bounds)
                results.append(make_result(
                    "3", "composite dial 100/50/0 on %s -- %s" % (risk, wname),
                    "composite (8 voting legs, k=%d); avg exposure %.0f%%"
                    % (ctx["k_last"], avg_w * 100),
                    bname, dates, start, end, eq, beq, per, bper,
                    dec, 1, block=4, avg_hold=avg_w))


def rule4(ctx, results):
    """Style ladder: growth-vs-value / size top third, monthly, vs IWB."""
    series, dates, end = ctx["series"], ctx["dates"], ctx["end_idx"]
    sl = sled_series(series, regime.REG_STYLE)
    rets = {t: daily_returns(s) for t, s in sl.items() if s}
    start = first_field_idx(sl, frac=1.0, lag=126)
    ctx["rule4_start"] = start
    bil = daily_returns(series.get("BIL"))
    ladders = walk_ladder(series, regime.REG_STYLE, start, end, 21)
    allfield = sorted({r["t"] for (_i, lad) in ladders for r in field_rows(lad)})
    for label, selector in (("style top third by blend", sel_blend_top),
                            ("style bottom third (diagnostic)", sel_blend_bottom)):
        dec = decisions_from(ladders, selector)
        idx0, eq, per, bounds, empty = run_decisions(rets, dec, end, cash_rets=bil)
        avg_hold = sum(len(m) for (_d, m) in dec) / float(len(dec))
        for bname, beq in (("IWB", buy_and_hold(rets, "IWB", idx0, end)),
                           ("EW 10 style boxes", ew_curve(rets, allfield, idx0, end))):
            bper = period_returns(beq, idx0, bounds)
            results.append(make_result(
                "4", "%s (21d)" % label, "REG_STYLE (%d boxes)" % len(allfield),
                bname, dates, idx0, end, eq, beq, per, bper, dec, 21,
                empty=empty, avg_hold=avg_hold))


def bond_excess_panel(series, upto=None):
    """The #91 excess-return series for the nine spread rows, built exactly as
    `regime.bond_field` builds them: each row divided by a duration-matched
    Treasury index interpolated off the five rungs."""
    ex = {}
    for e in regime.REG_BOND_SPREAD:
        raw = series.get(e["t"])
        m = regime.matched_treasury_series(series, regime.BOND_DURATION[e["t"]])
        s = regime.excess_series(raw, m)
        if s and any(v is not None for v in s):
            ex[e["t"]] = s if upto is None else s[:upto + 1]
    return ex


def rule5(ctx, results):
    """#91 bond ladder on EXCESS return + the Treasury rung the curve read
    favours, monthly, vs AGG."""
    series, dates, end = ctx["series"], ctx["dates"], ctx["end_idx"]
    need = {e["t"] for e in regime.REG_BONDS}
    rets = {t: daily_returns(series[t]) for t in need if series.get(t)}
    ex_full = bond_excess_panel(series)
    start = first_field_idx(ex_full, frac=1.0, lag=126)
    ctx["rule5_start"] = start
    if start is None or start >= end:
        return
    bil = rets.get("BIL")
    dec = []
    rungs = {"rallying": "TLT", "selling off": "BIL", "flat": None}
    reads = []
    i = start
    while i <= end:
        win = window(series, need, i)
        tr = {r["t"]: r for r in regime.sector_ladder(win, regime.REG_BONDS)["rows"]}
        curve_rows = regime.bond_curve_rows(tr, lambda v, d=6: v)
        cread = regime.bond_curve_read(curve_rows)
        exw = bond_excess_panel(win)
        lad = regime.sector_ladder(exw, regime.REG_BOND_SPREAD)
        pick = sel_blend_top(lad)
        rung = rungs.get(cread["duration"])
        if rung:
            pick = pick + [rung]
        reads.append(cread["duration"])
        dec.append((i, pick))
        i += 21
    idx0, eq, per, bounds, empty = run_decisions(rets, dec, end, cash_rets=bil)
    avg_hold = sum(len(m) for (_d, m) in dec) / float(len(dec))
    allfield = sorted(ex_full)
    for bname, beq in (("AGG", buy_and_hold(rets, "AGG", idx0, end)),
                       ("EW 9 spread rows", ew_curve(rets, allfield, idx0, end))):
        bper = period_returns(beq, idx0, bounds)
        results.append(make_result(
            "5", "bond excess top third + curve rung (21d)",
            "REG_BOND_SPREAD (9) on excess vs duration-matched UST; curve reads %s"
            % "/".join(sorted(set(reads))),
            bname, dates, idx0, end, eq, beq, per, bper, dec, 21,
            empty=empty, avg_hold=avg_hold))


def rule6(tech_path):
    """Summarise the per-ticker `backtest` block already in technicals.json.
    Nothing is re-run: this reads the numbers `technicals.backtest` wrote."""
    with open(tech_path, "r", encoding="utf-8") as fh:
        tech = json.load(fh)
    tk = tech.get("tickers") or {}
    sigs = ["macd_crossover_sig", "macd_mfi", "macd_rsi", "macd_sig_above_zero", "vpvma"]
    out = {"n_tickers": 0, "generated_at": tech.get("generated_at"), "signals": {}}
    names = [t for t in tk if tk[t].get("backtest")]
    out["n_tickers"] = len(names)
    for s in sigs:
        beats, exc, trades, zero, wr = [], [], [], 0, []
        beat_trades, lose_trades = [], []
        beat_zero, beat_bh, lose_bh = 0, [], []
        pairs = []
        for t in names:
            b = tk[t]["backtest"].get(s)
            if not b:
                continue
            e = b.get("excess_vs_hold")
            n = b.get("n_trades") or 0
            exc.append(e)
            trades.append(n)
            pairs.append((n, e))
            if n == 0:
                zero += 1
            if b.get("win_rate") is not None:
                wr.append(b["win_rate"])
            if b.get("beat_hold"):
                beats.append(t)
                beat_trades.append(n)
                beat_bh.append(b.get("buy_hold_return"))
                if n == 0:
                    beat_zero += 1
            else:
                lose_trades.append(n)
                lose_bh.append(b.get("buy_hold_return"))
        out["signals"][s] = {
            "n": len(exc), "n_beat": len(beats),
            "pct_beat": round(len(beats) / len(exc), 3) if exc else None,
            "median_excess_pp": round(median(exc) * 100, 1) if exc else None,
            "mean_excess_pp": round(sum(exc) / len(exc) * 100, 1) if exc else None,
            "median_trades": median(trades),
            "median_trades_beat": median(beat_trades) if beat_trades else None,
            "median_trades_lose": median(lose_trades) if lose_trades else None,
            "n_zero_trade": zero,
            # THE trap check, both halves: do the winners trade MORE (the
            # multiple-comparisons story), or do they simply never trade and so
            # sit in cash while the NAME fell (the base-rate story)?
            "n_beat_with_zero_trades": beat_zero,
            "median_bh_of_beaters_pp": round(median(beat_bh) * 100, 1) if beat_bh else None,
            "median_bh_of_losers_pp": round(median(lose_bh) * 100, 1) if lose_bh else None,
            "rank_corr_trades_vs_excess": round(spearman(pairs), 3) if len(pairs) > 5 else None,
            "median_win_rate": round(median(wr), 3) if wr else None,
        }
    # multiple-comparisons: how many names have ANY of the five beating hold,
    # and what independence would predict from the five marginal rates.
    any_beat = 0
    for t in names:
        if any((tk[t]["backtest"].get(s) or {}).get("beat_hold") for s in sigs):
            any_beat += 1
    exp_none = 1.0
    for s in sigs:
        v = out["signals"][s]["pct_beat"] or 0.0
        exp_none *= (1.0 - v)
    out["n_any_signal_beats"] = any_beat
    out["pct_any_signal_beats"] = round(any_beat / len(names), 3) if names else None
    out["pct_any_if_independent"] = round(1.0 - exp_none, 3)
    # how many names FELL over their own backtest window -- the denominator a
    # zero-trade "beat" is really measuring
    bh_any = [tk[t]["backtest"][sigs[0]]["buy_hold_return"] for t in names
              if tk[t]["backtest"].get(sigs[0])]
    out["n_names_bh_negative"] = sum(1 for v in bh_any if v is not None and v < 0)
    out["n_names_bh"] = len(bh_any)
    return out


def spearman(pairs):
    """Spearman rank correlation of (x, y) pairs; average ranks on ties."""
    def ranks(vals):
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        r = [0.0] * len(vals)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    xs = ranks([p[0] for p in pairs])
    ys = ranks([p[1] for p in pairs])
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return num / (dx * dy) if (dx and dy) else 0.0


# ==========================================================================
# tape description (caveat a, as numbers)
# ==========================================================================

def tape_stats(series, dates, i0, i1, tickers):
    out = {}
    for t in tickers:
        s = series.get(t)
        if not s or s[i0] is None or s[i1] is None:
            continue
        eq = [1.0]
        r = daily_returns(s)
        for i in range(i0 + 1, i1 + 1):
            eq.append(eq[-1] * (1.0 + (r[i] if r[i] is not None else 0.0)))
        out[t] = {"total": round(eq[-1] - 1.0, 4), "cagr": _r(cagr(eq, i1 - i0)),
                  "vol": _r(ann_vol(eq)), "max_dd": _r(max_drawdown(eq))}
    return out


def drawdowns_over(series, i0, i1, thr=0.05):
    """Count of distinct peak-to-trough declines deeper than `thr`."""
    s = series
    peak = None
    n = 0
    in_dd = False
    trough = 0.0
    for i in range(i0, i1 + 1):
        v = s[i]
        if v is None:
            continue
        if peak is None or v > peak:
            if in_dd:
                in_dd = False
            peak = v
            trough = 0.0
        dd = v / peak - 1.0
        if dd < trough:
            trough = dd
        if trough <= -thr and not in_dd:
            in_dd = True
            n += 1
    return n


# ==========================================================================
# self-check: the composite state is causal
# ==========================================================================

def selfcheck(conn, dates, series, state, cut_frac=0.7):
    """Rebuild the published composite on a panel truncated mid-history and
    assert the state series matches the full-panel one bar for bar up to the
    cut. If any leg peeked forward this fails."""
    cut = int(len(dates) * cut_frac)
    sub = {t: (s[:cut + 1] if s else s) for t, s in series.items()}
    reg2 = regime.build_regime(conn, [], panel=(dates[:cut + 1], sub), round_floats=False)
    st2 = reg2["composite"]["state"]
    bad = [i for i in range(len(st2)) if st2[i] != state[i]]
    return {"cut_date": dates[cut], "n_compared": len(st2), "mismatches": len(bad),
            "first_bad": (dates[bad[0]] if bad else None)}


# ==========================================================================
# main
# ==========================================================================

def build_context(db):
    conn = sqlite3.connect(db)
    dates, series = regime.build_panel(conn)
    end_idx = len(dates) - 1
    if END_DATE in dates:
        end_idx = dates.index(END_DATE)
    reg = regime.build_regime(conn, [], panel=(dates[:end_idx + 1],
                                               {t: (s[:end_idx + 1] if s else s)
                                                for t, s in series.items()}),
                              round_floats=False)
    comp = reg["composite"]
    fa = max(L["first_active"] for L in reg["legs"] if L["voting"])
    return {
        "conn": conn, "dates": dates[:end_idx + 1],
        "series": {t: (s[:end_idx + 1] if s else s) for t, s in series.items()},
        "end_idx": end_idx, "state": comp["state"], "k_last": comp["k_last"],
        "all_legs_idx": fa, "regime_block": reg,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.path.join(_HERE, "prices.db"))
    ap.add_argument("--technicals", default=os.path.join(_HERE, "data", "technicals.json"))
    ap.add_argument("--out", help="write the results CSV here")
    ap.add_argument("--json-out", help="write the full result dict as JSON here")
    ap.add_argument("--stats", action="store_true", help="print the summary tables")
    ap.add_argument("--selfcheck", action="store_true",
                    help="prove the composite state series is causal")
    args = ap.parse_args(argv)

    ctx = build_context(args.db)
    dates = ctx["dates"]
    results = []
    rule1(ctx, results)
    rule1_spdr(ctx, results)
    rule2(ctx, results)
    rule3(ctx, results)
    rule4(ctx, results)
    rule5(ctx, results)
    bt = rule6(args.technicals)

    # caveat (a): what the tape was, as numbers
    i0 = ctx["rule1_start"]
    tape = {
        "window": [dates[i0], dates[ctx["end_idx"]]],
        "sessions": ctx["end_idx"] - i0,
        "assets": tape_stats(ctx["series"], dates, i0, ctx["end_idx"],
                             ["SPY", "QQQ", "IWM", "AGG", "GLD", "SMH", "XLU", "XLE"]),
        "spy_dd_over_5pct": drawdowns_over(ctx["series"]["SPY"], i0, ctx["end_idx"], 0.05),
        "spy_dd_over_10pct": drawdowns_over(ctx["series"]["SPY"], i0, ctx["end_idx"], 0.10),
        "composite_state_share": None,
    }
    st = ctx["state"]
    seg = st[i0:ctx["end_idx"] + 1]
    tape["composite_state_share"] = {
        s: round(sum(1 for x in seg if x == s) / len(seg), 3)
        for s in ("risk-on", "neutral", "defensive")}
    segl = st[ctx["all_legs_idx"]:ctx["end_idx"] + 1]
    tape["composite_state_share_full"] = {
        s: round(sum(1 for x in segl if x == s) / len(segl), 3)
        for s in ("risk-on", "neutral", "defensive")}
    # the two other windows the memo quotes: the full 2y ETF span and the 8.8y
    # span the composite itself covers
    b0 = first_bar_idx(ctx["series"]["BIL"])
    tape["two_year"] = {"window": [dates[b0], dates[ctx["end_idx"]]],
                        "sessions": ctx["end_idx"] - b0,
                        "assets": tape_stats(ctx["series"], dates, b0, ctx["end_idx"],
                                             ["SPY", "QQQ", "BIL", "AGG", "GLD"]),
                        "spy_dd_over_5pct": drawdowns_over(ctx["series"]["SPY"], b0,
                                                           ctx["end_idx"], 0.05),
                        "spy_dd_over_10pct": drawdowns_over(ctx["series"]["SPY"], b0,
                                                            ctx["end_idx"], 0.10)}
    fa = ctx["all_legs_idx"]
    tape["composite_window"] = {
        "window": [dates[fa], dates[ctx["end_idx"]]],
        "sessions": ctx["end_idx"] - fa,
        "assets": tape_stats(ctx["series"], dates, fa, ctx["end_idx"], ["SPY", "QQQ"]),
        "spy_dd_over_10pct": drawdowns_over(ctx["series"]["SPY"], fa, ctx["end_idx"], 0.10),
        "spy_dd_over_20pct": drawdowns_over(ctx["series"]["SPY"], fa, ctx["end_idx"], 0.20)}
    tape["rule1_picks_top20"] = ctx.get("rule1_picks", [])[:20]
    tape["rule1_field_size"] = ctx.get("rule1_field_size")
    tape["rule1_n_picks_ever"] = len(ctx.get("rule1_picks", []))

    payload = {"results": results, "per_ticker_backtest": bt, "tape": tape,
               "starts": {"sector_ladder": dates[ctx["rule1_start"]],
                          "style_ladder": dates[ctx["rule4_start"]],
                          "bond_ladder": (dates[ctx["rule5_start"]]
                                          if ctx.get("rule5_start") else None),
                          "composite_all_legs": dates[ctx["all_legs_idx"]],
                          "bil_first_bar": dates[first_bar_idx(ctx["series"]["BIL"])]},
               "end": dates[ctx["end_idx"]]}

    if args.selfcheck:
        payload["selfcheck"] = selfcheck(ctx["conn"], ctx["dates"], ctx["series"], st)

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=RESULT_FIELDS)
            w.writeheader()
            for row in results:
                w.writerow(row)
        print("wrote %s (%d rows)" % (args.out, len(results)))

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, sort_keys=True)
        print("wrote %s" % args.json_out)

    if args.stats or not (args.out or args.json_out):
        print_stats(payload)
    return payload


def print_stats(payload):
    p = payload
    print("\n== windows ==")
    for k, v in p["starts"].items():
        print("  %-22s %s" % (k, v))
    print("  end                    %s" % p["end"])
    print("\n== tape (caveat a) ==")
    t = p["tape"]
    print("  %s -> %s  (%d sessions)" % (t["window"][0], t["window"][1], t["sessions"]))
    for k, v in t["assets"].items():
        print("   %-5s total %+7.1f%%  cagr %+6.1f%%  vol %5.1f%%  maxdd %6.1f%%"
              % (k, v["total"] * 100, (v["cagr"] or 0) * 100, (v["vol"] or 0) * 100,
                 (v["max_dd"] or 0) * 100))
    print("   SPY drawdowns >5%%: %d   >10%%: %d" % (t["spy_dd_over_5pct"], t["spy_dd_over_10pct"]))
    print("   composite state share (window): %s" % t["composite_state_share"])
    print("   composite state share (full):   %s" % t["composite_state_share_full"])
    for key in ("two_year", "composite_window"):
        w = t[key]
        print("  [%s] %s -> %s (%d sessions)" % (key, w["window"][0], w["window"][1], w["sessions"]))
        for k, v in w["assets"].items():
            print("     %-5s total %+7.1f%%  cagr %+6.1f%%  vol %5.1f%%  maxdd %6.1f%%"
                  % (k, v["total"] * 100, (v["cagr"] or 0) * 100, (v["vol"] or 0) * 100,
                     (v["max_dd"] or 0) * 100))
        print("     dd counts: %s" % {k: v for k, v in w.items() if k.startswith("spy_dd")})
    print("   rule 1 field %s sleds; %s distinct sleds ever picked; top 20 by pick count:"
          % (t["rule1_field_size"], t["rule1_n_picks_ever"]))
    print("     %s" % ", ".join("%s x%d" % (k, v) for k, v in t["rule1_picks_top20"]))
    if "selfcheck" in p:
        print("\n== selfcheck ==\n  %s" % p["selfcheck"])
    print("\n== results ==")
    hdr = ("%-4s %-50s %-20s %5s %7s %7s %7s %6s %6s %6s %5s %6s  %s"
           % ("rule", "strategy", "benchmark", "ndec", "cagr", "bcagr", "xcagr",
              "vol", "mdd", "bmdd", "hit", "turn", "boot p"))
    print(hdr)
    print("-" * len(hdr))
    for r in p["results"]:
        print("%-4s %-50s %-20s %5s %6.1f%% %6.1f%% %6.1f%% %5.1f%% %5.1f%% %5.1f%% %4s%% %6s  %s/%s %s"
              % (r["rule"], r["strategy"][:50], r["benchmark"][:20], r["n_decisions"],
                 (r["cagr"] or 0) * 100, (r["bench_cagr"] or 0) * 100,
                 (r["excess_cagr"] or 0) * 100, (r["vol"] or 0) * 100,
                 (r["max_dd"] or 0) * 100, (r["bench_max_dd"] or 0) * 100,
                 ("%.0f" % ((r["hit_rate"] or 0) * 100)),
                 r["turnover_per_rebal"],
                 r["boot_p_iid"], r["boot_p_block"], r["verdict"]))
    print("\n== per-ticker technicals.json backtest (rule 6) ==")
    b = p["per_ticker_backtest"]
    print("  %d tickers, generated %s; %d/%d names had a NEGATIVE buy-and-hold"
          % (b["n_tickers"], b["generated_at"], b["n_names_bh_negative"], b["n_names_bh"]))
    print("  %-22s %5s %6s %9s %9s %7s %8s %8s %8s %7s"
          % ("signal", "beat", "pct", "med exc", "mean exc", "med trd", "trd|beat",
             "trd|lose", "beat@0trd", "rho"))
    for s, v in b["signals"].items():
        print("  %-22s %5d %5.1f%% %7.1fpp %7.1fpp %7s %8s %8s %8s %7s"
              % (s, v["n_beat"], (v["pct_beat"] or 0) * 100, v["median_excess_pp"],
                 v["mean_excess_pp"], v["median_trades"], v["median_trades_beat"],
                 v["median_trades_lose"], v["n_beat_with_zero_trades"],
                 v["rank_corr_trades_vs_excess"]))
    for s, v in b["signals"].items():
        print("   %-22s median buy-and-hold of beaters %+8.1fpp vs losers %+8.1fpp"
              % (s, v["median_bh_of_beaters_pp"], v["median_bh_of_losers_pp"]))
    print("  any of 5 beats hold: %d/%d (%.1f%%); under independence %.1f%%"
          % (b["n_any_signal_beats"], b["n_tickers"], (b["pct_any_signal_beats"] or 0) * 100,
             (b["pct_any_if_independent"] or 0) * 100))


if __name__ == "__main__":
    main()
