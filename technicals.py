#!/usr/bin/env python3
"""
Regime-gated technical indicators over the stored daily bar history.

The design deliberately holds two papers in tension:

  Chio (2022), arXiv:2206.12282 -- "A comparative study of the MACD-base
    trading strategies". Backtests MACD(12,26,9) on Dow/Nasdaq/S&P constituents,
    2015-2021. Naked MACD is close to a coin flip (win rate 0.37-0.41 on the
    crossover rules). What lifts it is *price-pressure* information: pairing it
    with RSI or MFI, or folding volume and intraday volatility into the
    indicator itself -- his VPVMA. The caveat the paper admits: it never
    benchmarks against buy-and-hold, over a period when buy-and-hold did well.

  Hurwitz & Marwala (2011), arXiv:1110.3383 -- "Suitability of using technical
    indicators as potential strategies within intelligent trading systems".
    Generates synthetic series with known structure and shows each indicator
    only works when its own assumption holds: MACD assumes a trend, RSI assumes
    a cycle, and those assumptions are mutually exclusive. Their MACD earns 195%
    on trending data and fails outright on oscillating data. Every result is
    reported against buy-and-hold.

So: the regime is classified BEFORE any indicator is read, indicators whose
assumption the data fails are muted rather than displayed, and every strategy
is scored against buy-and-hold over the same window.

Nothing here is a recommendation. It describes what a series is doing.

Pure stdlib on purpose: CI installs only yfinance, and these are simple
recurrences that do not need pandas.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
from datetime import datetime, timezone

import regime  # cross-sectional regime/rotation/rank-receipt port (SPEC-8)

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "prices.db")
DEFAULT_TICKERS = os.path.join(HERE, "tickers.txt")
DEFAULT_OUT = os.path.join(HERE, "data")

# Chio uses the traditional (12,26,9) throughout. Both papers independently
# conclude that fitting these is a mistake -- his genetic algorithm returned a
# different "optimum" per index and he calls it overfitting; H&M explain why
# (the lookback is a frequency selector, and the dominant frequency moves).
# Fixed on purpose. Do not tune them.
MACD_FAST, MACD_SLOW, MACD_SIG = 12, 26, 9
RSI_N = 14
MFI_N = 14
VPVMA_BANDWIDTH = 0.1        # Chio, Table 8

# Regime window. ~90 trading days: long enough for a variance ratio at q=5 to
# mean something, short enough to still describe the *current* regime.
REGIME_WINDOW = 90
REGIME_VR_Q = 5

# Thresholds below are Monte-Carlo calibrated against a random-walk null
# (2,000 draws, 90-session windows, sigma=1.5%/day) rather than chosen by eye.
# Two results from that calibration drive the design:
#
#   1. R^2 >= 0.50 is worthless as a trend test: 44.7% of PURE RANDOM WALKS
#      clear it over 90 sessions. The null distribution is p50=0.44, p90=0.85,
#      p95=0.89. So the bar is set at the p90 of the null -- "more directional
#      than 90% of random walks" -- and the residual false-positive rate is
#      reported in the payload rather than hidden.
#
#   2. The variance ratio does NOT identify cycles at these horizons. A sinusoid
#      is locally smooth, so VR(5) on cyclical pseudodata runs 3.9-11.3, i.e.
#      far ABOVE 1, not below. VR is therefore reported as a diagnostic but is
#      not used to classify.
#
# What does separate a cycle from a random walk is smoothness: a clean swing
# crosses its own SMA20 about twice per period (4-6 crossings per 100 sessions),
# whereas a random walk chops across it constantly (median 11.3). So a cycle is
# "low drift AND smooth", not "low drift AND choppy".
R2_TREND_MIN = 0.85          # p90 of the random-walk null
R2_REVERT_MAX = 0.35         # no meaningful directional drift
CROSS_REVERT_MAX = 7.0       # SMA20 crossings per 100 sessions; cycles 4-6, RW ~11
CROSS_SMA = 20

# Empirical error rates of the gate itself under the random-walk null, from the
# same calibration. Surfaced in the payload so a reader can discount the label.
GATE_FALSE_TREND = 0.10      # random walks that get labelled "trending"
GATE_FALSE_REVERT = 0.08     # random walks that get labelled "mean_reverting"

# R^2 quantiles of the random-walk null at 5-point intervals (20,000 draws,
# 90-session windows). R^2 for a driftless random walk is scale-invariant, so
# this table does not depend on the volatility assumed when generating it.
# Used to translate a raw R^2 into "more directional than N% of random walks",
# which is the only reading of it that means anything.
R2_NULL_Q5 = [0.0, 0.0068, 0.0263, 0.0575, 0.0995, 0.1508, 0.2053, 0.2626,
              0.3202, 0.3806, 0.4387, 0.4962, 0.5506, 0.6046, 0.6552, 0.7040,
              0.7493, 0.7954, 0.8401, 0.8877]


def r2_vs_null_pct(r2):
    """Percentile of an observed R^2 within the random-walk null distribution."""
    if r2 is None:
        return None
    pct = 0
    for i, q in enumerate(R2_NULL_Q5):
        if r2 >= q:
            pct = i * 5
    return min(pct + 5, 100) if r2 >= R2_NULL_Q5[-1] else pct

MIN_BARS = 260               # below this a name gets no technicals at all

# --- momentum structure (the screener-card families, collapsed to three facts) ---
# A commercial setup card shows ~12 chips (MA FAN, ATR SQZ, CLOSE HI, 52W HI,
# VDU, HH/HL, W.EMA, OBV+, NR7, U/D VOL, SQUEEZE, SPRING ...) and scores N/12.
# Most of those chips restate one fact -- "price went up" -- so a count
# manufactures confidence out of correlation. Collapsed here to three families
# that are actually orthogonal:
#   1. trend STRUCTURE   (EMA stack order, confirmed swing pivots, close position)
#   2. VOLATILITY state  (ATR% ranked within the name's own trailing year --
#                         subsumes ATR SQZ / SQUEEZE / NR7, which all measure
#                         "range is contracting")
#   3. PARTICIPATION     (up-day vs down-day volume, OBV direction, relative
#                         volume -- Chio's price-pressure finding generalized)
# Deliberately no composite score. Consciously skipped: SPRING (needs a defined
# trading range; fuzzy heuristic), W.EMA (redundant with the daily 200), NR7
# (a noisier single-day version of the ATR percentile).
ATR_N = 14
STACK_EMAS = (20, 50, 200)
ATR_RANK_WIN = 252           # rank today's ATR% within the name's own year
CONTRACT_MAX = 0.20          # at/below own 20th percentile = "coiled"
EXPAND_MIN = 0.80            # at/above own 80th percentile = "wide"
UDVOL_CONFIRM = 1.2          # up-day volume outweighs down-day volume by >=20%
SWING_K = 5                  # a pivot needs 5 sessions on each side to confirm


# --------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------

def sma(xs, n):
    """Simple moving average; None until n points exist."""
    out, run = [None] * len(xs), 0.0
    for i, x in enumerate(xs):
        run += x
        if i >= n:
            run -= xs[i - n]
        if i >= n - 1:
            out[i] = run / n
    return out


def ema(xs, n):
    """EMA seeded with the SMA of the first n points. alpha = 2/(n+1), Chio 2.1-1."""
    out = [None] * len(xs)
    if len(xs) < n:
        return out
    a = 2.0 / (n + 1.0)
    prev = sum(xs[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(xs)):
        prev = a * xs[i] + (1 - a) * prev
        out[i] = prev
    return out


def stdev(xs):
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


# --------------------------------------------------------------------------
# indicators
# --------------------------------------------------------------------------

def macd(closes, fast=MACD_FAST, slow=MACD_SLOW, sig=MACD_SIG):
    """MACD line / signal / histogram. Chio 2.1-2 .. 2.1-4."""
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [(f - s) if (f is not None and s is not None) else None
            for f, s in zip(ef, es)]
    seed = next((i for i, v in enumerate(line) if v is not None), len(line))
    tail = list(ema(line[seed:], sig)) if seed < len(line) else []
    signal = [None] * seed + tail
    signal += [None] * (len(line) - len(signal))
    hist = [(l - s) if (l is not None and s is not None) else None
            for l, s in zip(line, signal)]
    return line, signal, hist


def rsi(closes, n=RSI_N):
    """Wilder RSI. Chio 2.3-1 .. 2.3-6: simple mean seeds it, then (prev*13 + x)/14."""
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    out[n] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
        out[i + 1] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    return out


def typical_price(h, l, c):
    return (h + l + c) / 3.0


def mfi(highs, lows, closes, vols, n=MFI_N):
    """Money Flow Index -- volume-weighted RSI. Chio 2.4-1 .. 2.4-4."""
    out = [None] * len(closes)
    tp = [typical_price(h, l, c) for h, l, c in zip(highs, lows, closes)]
    pos = [0.0] * len(tp)
    neg = [0.0] * len(tp)
    for i in range(1, len(tp)):
        flow = tp[i] * (vols[i] or 0)
        if tp[i] > tp[i - 1]:
            pos[i] = flow
        elif tp[i] < tp[i - 1]:
            neg[i] = flow
    for i in range(n, len(tp)):
        p = sum(pos[i - n + 1:i + 1])
        q = sum(neg[i - n + 1:i + 1])
        out[i] = 100.0 if q == 0 else 100.0 - 100.0 / (1 + p / q)
    return out


def vpvma(highs, lows, closes, opens, vols,
          fast=MACD_FAST, slow=MACD_SLOW, sig=MACD_SIG):
    """
    Chio's VPVMA (section 4.1): volume-weighted typical price over a fast and a
    slow window, each scaled by that day's intraday dispersion, then EMA-smoothed
    and differenced. Folds volume + volatility into the MACD form.

    Two deviations from the paper as printed, both apparent typos:
      * eq 4.2-6 writes EMA(SVWMA * DV, s) for the LONG leg, which would make it
        identical to the short leg. LVWMA is used here, plainly the intent.
      * Table 8's sell rule repeats "VPVMA(t-1) <= VPVMAS(t-1)" from the buy
        rule; taken literally the strategy can never sell. Mirrored to >= here.
    """
    tp = [typical_price(h, l, c) for h, l, c in zip(highs, lows, closes)]
    dv = [stdev([h, l, c, o]) for h, l, c, o in zip(highs, lows, closes, opens)]

    def vwma(win):
        out = [None] * len(tp)
        num = 0.0
        den = 0.0
        for i in range(len(tp)):
            v = vols[i] or 0
            num += tp[i] * v
            den += v
            if i >= win:
                pv = vols[i - win] or 0
                num -= tp[i - win] * pv
                den -= pv
            if i >= win - 1:
                out[i] = (num / den) if den else tp[i]
        return out

    svw, lvw = vwma(fast), vwma(slow)
    start = slow - 1
    s_series = [svw[i] * dv[i] for i in range(start, len(tp))]
    l_series = [lvw[i] * dv[i] for i in range(start, len(tp))]
    es, el = ema(s_series, fast), ema(l_series, slow)

    line = [None] * len(tp)
    for k in range(len(s_series)):
        if es[k] is not None and el[k] is not None:
            line[start + k] = es[k] - el[k]

    seed = next((i for i, v in enumerate(line) if v is not None), len(line))
    tail = list(sma(line[seed:], sig)) if seed < len(line) else []
    sigline = [None] * seed + tail
    sigline += [None] * (len(line) - len(sigline))
    return line, sigline


# --------------------------------------------------------------------------
# regime gate  (Hurwitz & Marwala)
# --------------------------------------------------------------------------

def r2_loglinear(closes):
    """R^2 of an OLS fit of ln(price) on time. How cleanly directional the move is."""
    ys = [math.log(c) for c in closes if c and c > 0]
    n = len(ys)
    if n < 10:
        return None
    xs = list(range(n))
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return (sxy * sxy) / (sxx * syy)


def variance_ratio(closes, q=REGIME_VR_Q):
    """
    Lo-MacKinlay variance ratio: Var of q-day returns over q * Var of 1-day
    returns. ~1 random walk, >1 persistent/trending, <1 mean-reverting. This is
    the direct statistical form of the question H&M pose about which assumption
    actually holds for a given series.
    """
    lp = [math.log(c) for c in closes if c and c > 0]
    if len(lp) < q * 4:
        return None
    r1 = [lp[i] - lp[i - 1] for i in range(1, len(lp))]
    rq = [lp[i] - lp[i - q] for i in range(q, len(lp))]
    v1, vq = stdev(r1) ** 2, stdev(rq) ** 2
    # Guard the degenerate case: a noiseless series has v1 at float-noise level,
    # and vq/(q*v1) is then pure garbage rather than a small number.
    if v1 <= 1e-12:
        return None
    return vq / (q * v1)


def sma_cross_rate(closes, n=CROSS_SMA):
    """
    Sign changes of (close - SMA(n)) per 100 sessions. A smooth swing crosses
    about twice per cycle; a random walk chops across constantly; a clean trend
    never crosses at all. This is what separates a cycle from noise, which the
    variance ratio does not do at these horizons.
    """
    s = sma(closes, n)
    d = [c - m for c, m in zip(closes, s) if m is not None]
    if len(d) < 3:
        return None
    x = sum(1 for i in range(1, len(d)) if (d[i] > 0) != (d[i - 1] > 0))
    return 100.0 * x / len(d)


def classify_regime(closes, window=REGIME_WINDOW):
    """
    Which indicator family, if any, has its assumption satisfied right now.

    This is the gate. An indicator read outside its regime is noise with a
    number attached -- the whole point of the H&M paper.

    The label is a description of the last `window` sessions, not a forecast,
    and it carries a real error rate against a random-walk null (see
    GATE_FALSE_TREND / GATE_FALSE_REVERT). Over 90 sessions a random walk
    genuinely can look like a trend; that is a fact about markets, not a bug,
    so the rate is published alongside the label.
    """
    seg = closes[-window:]
    if len(seg) < 40:
        return {"regime": "insufficient_history", "r2": None, "vr": None,
                "cross_rate": None, "trend_ok": False, "oscillator_ok": False,
                "window": len(seg)}
    r2 = r2_loglinear(seg)
    vr = variance_ratio(seg)                 # diagnostic only, not classifying
    cross = sma_cross_rate(seg)
    if r2 is None or cross is None:
        return {"regime": "undetermined", "r2": r2, "vr": vr,
                "cross_rate": cross, "trend_ok": False, "oscillator_ok": False,
                "window": len(seg)}
    # Order matters: a strong drift is a trend regardless of its smoothness.
    if r2 >= R2_TREND_MIN:
        regime = "trending"
    elif r2 < R2_REVERT_MAX and cross <= CROSS_REVERT_MAX:
        regime = "mean_reverting"
    else:
        regime = "neither"
    return {
        "regime": regime,
        "r2": round(r2, 4),
        # "this drift is cleaner than N% of random walks" -- the only reading of
        # a raw R^2 that carries information.
        "r2_vs_null_pct": r2_vs_null_pct(r2),
        "vr": None if vr is None else round(vr, 4),
        "cross_rate": round(cross, 2),
        "slope": "up" if seg[-1] >= seg[0] else "down",
        "trend_ok": regime == "trending",             # MACD, VPVMA readable
        "oscillator_ok": regime == "mean_reverting",  # RSI, MFI readable
        "false_positive_rate": (GATE_FALSE_TREND if regime == "trending" else
                                GATE_FALSE_REVERT if regime == "mean_reverting" else None),
        "window": len(seg),
    }


# --------------------------------------------------------------------------
# momentum -- explicitly the non-intraday horizons
# --------------------------------------------------------------------------

def momentum(closes, adj=None):
    """
    Longer-horizon momentum, where the durable published evidence sits:
    Jegadeesh-Titman 12-1 cross-sectional and Moskowitz-Ooi-Pedersen
    time-series momentum. Neither paper in the pair above is intraday; both are
    daily. The useful axis is therefore horizon and cross-section, not
    resolution.

    Returns are computed on ADJUSTED closes so dividend payers are not
    understated against non-payers -- this book holds both gold miners and
    no-dividend semis, and ranking them on raw close would systematically
    penalise the miners. Price *levels* (52-week high/low, distance from the
    moving averages) stay on the raw close, because those are the prices you
    actually see and trade against.

    Note this differs from the backtest, which uses raw close deliberately:
    Chio's argument that "no one can buy the stock at Adj Close" is right for
    simulating fills, and wrong for comparing total return across names.
    """
    adj = adj or closes

    def ret(a, b):
        if len(adj) > a and adj[-a - 1]:
            return adj[-b - 1] / adj[-a - 1] - 1
        return None

    out = {
        "mom_12_1": ret(252, 21),   # skip the most recent month (short reversal)
        "mom_6_1": ret(126, 21),
        "ret_12m": ret(252, 0),
        "ret_3m": ret(63, 0),
        "ret_1m": ret(21, 0),
        "dist_52w_high": None,
        "dist_52w_low": None,
    }
    if len(closes) >= 252:
        hi, lo = max(closes[-252:]), min(closes[-252:])
        out["dist_52w_high"] = (closes[-1] / hi - 1) if hi else None
        out["dist_52w_low"] = (closes[-1] / lo - 1) if lo else None
    s200, s50 = sma(closes, 200), sma(closes, 50)
    out["vs_200dma"] = (closes[-1] / s200[-1] - 1) if s200[-1] else None
    out["vs_50dma"] = (closes[-1] / s50[-1] - 1) if s50[-1] else None
    out["tsmom_12m"] = None if out["ret_12m"] is None else (1 if out["ret_12m"] > 0 else -1)
    return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in out.items()}


# --------------------------------------------------------------------------
# momentum structure
# --------------------------------------------------------------------------

def atr_pct_series(highs, lows, closes, n=ATR_N):
    """Wilder ATR(14) as a percent of close, full series (None until warm)."""
    m = len(closes)
    out = [None] * m
    if m < n + 1:
        return out
    trs = [highs[0] - lows[0]]
    for i in range(1, m):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    prev = sum(trs[1:n + 1]) / n
    if closes[n]:
        out[n] = 100.0 * prev / closes[n]
    for i in range(n + 1, m):
        prev = (prev * (n - 1) + trs[i]) / n
        if closes[i]:
            out[i] = 100.0 * prev / closes[i]
    return out


def pct_rank_trailing(vals, win=ATR_RANK_WIN):
    """Percentile of the LAST value within the trailing `win` values, inclusive.
    None until a full window exists -- a rank against a partial year is a
    different statistic and would silently mean something else."""
    if not vals or vals[-1] is None:
        return None
    tail = [v for v in vals[-win:] if v is not None]
    if len(tail) < win:
        return None
    v = vals[-1]
    return sum(1 for x in tail if x <= v) / len(tail)


def ema_stack(closes):
    """Order of close vs EMA20/50/200 -- trend structure as a STATE description.
    This is not the statistical trend test (classify_regime is); over 90
    sessions plenty of random walks also produce an aligned stack."""
    e = {n: ema(closes, n) for n in STACK_EMAS}
    last = {n: e[n][-1] for n in STACK_EMAS}
    if any(v is None for v in last.values()):
        return {"state": None, "ema200_slope_63d": None}
    c = closes[-1]
    if c > last[20] > last[50] > last[200]:
        state = "aligned_up"
    elif c < last[20] < last[50] < last[200]:
        state = "aligned_down"
    else:
        state = "mixed"
    s200 = e[200]
    slope = None
    if len(s200) > 63 and s200[-64]:
        slope = s200[-1] / s200[-64] - 1
    return {"state": state, "ema200_slope_63d": slope}


def swing_label(highs, lows, k=SWING_K):
    """HH/HL vs LH/LL from the last two confirmed pivot highs and lows.
    A pivot needs k bars on each side, so this read lags by k sessions --
    confirmed structure only, nothing repaints."""
    ph, pl = [], []
    for i in range(k, len(highs) - k):
        wh = highs[i - k:i + k + 1]
        if highs[i] == max(wh) and wh.count(highs[i]) == 1:
            ph.append(highs[i])
        wl = lows[i - k:i + k + 1]
        if lows[i] == min(wl) and wl.count(lows[i]) == 1:
            pl.append(lows[i])
    if len(ph) < 2 or len(pl) < 2:
        return None
    hh, hl = ph[-1] > ph[-2], pl[-1] > pl[-2]
    return "HH/HL" if (hh and hl) else "LH/LL" if (not hh and not hl) else "mixed"


def close_position(highs, lows, closes, n=20):
    """Where in its daily range the name has been closing, 0..1, 20d average.
    Buyers pressing into the close live near 1; the card's CLOSE HI chip."""
    num = den = 0.0
    for i in range(max(0, len(closes) - n), len(closes)):
        rng = highs[i] - lows[i]
        if rng > 0:
            num += (closes[i] - lows[i]) / rng
            den += 1
    return (num / den) if den else None


def ud_vol_ratio(closes, vols, n=50):
    """Up-day volume / down-day volume over n sessions. >1 = accumulation-ish.
    Capped at 9.99 (a zero-down-day window is 'all buyers', not infinity)."""
    up = dn = 0.0
    for i in range(max(1, len(closes) - n), len(closes)):
        v = float(vols[i] or 0)
        if closes[i] > closes[i - 1]:
            up += v
        elif closes[i] < closes[i - 1]:
            dn += v
    if up + dn == 0:
        return None
    return 9.99 if dn == 0 else min(up / dn, 9.99)


def obv_read(closes, vols, look=63):
    """On-balance volume vs price over `look` sessions: confirms, or diverges.
    Bullish divergence = price down while OBV rises (accumulation into
    weakness); bearish = price up on fading volume."""
    if len(closes) <= look or sum((v or 0) for v in vols[-look:]) == 0:
        return None
    obv, series = 0.0, [0.0]
    for i in range(1, len(closes)):
        v = float(vols[i] or 0)
        if closes[i] > closes[i - 1]:
            obv += v
        elif closes[i] < closes[i - 1]:
            obv -= v
        series.append(obv)
    d_obv = series[-1] - series[-1 - look]
    d_px = closes[-1] - closes[-1 - look]
    if d_px > 0 and d_obv < 0:
        return "bearish_divergence"
    if d_px < 0 and d_obv > 0:
        return "bullish_divergence"
    return "confirms"


def momentum_structure(highs, lows, closes, vols):
    """The three families, plus the conjunction flag `in_setup` =
    stacked up + volatility coiled + buyers outweighing sellers."""
    stack = ema_stack(closes)
    atrp = atr_pct_series(highs, lows, closes)
    rank = pct_rank_trailing(atrp)
    vol_state = (None if rank is None else
                 "contracted" if rank <= CONTRACT_MAX else
                 "expanded" if rank >= EXPAND_MIN else "normal")
    has_vol = any(vols[-50:])
    ud = ud_vol_ratio(closes, vols) if has_vol else None
    s10 = sma([float(v or 0) for v in vols], 10)
    s50 = sma([float(v or 0) for v in vols], 50)
    v10v50 = (s10[-1] / s50[-1]) if (s10[-1] and s50[-1]) else None
    rvol = (float(vols[-1] or 0) / s50[-1]) if s50[-1] else None
    in_setup = (stack["state"] == "aligned_up" and vol_state == "contracted"
                and ud is not None and ud >= UDVOL_CONFIRM)

    def r(v, d=4):
        return None if v is None else round(v, d)

    return {
        "stack": stack["state"],
        "ema200_slope_63d": r(stack["ema200_slope_63d"]),
        "swing": swing_label(highs, lows),
        "close_pos_20": r(close_position(highs, lows, closes), 3),
        "atr_pct": r(atrp[-1], 2),
        "atr_pct_rank_1y": r(rank, 3),
        "vol_state": vol_state,
        "ud_vol_50d": r(ud, 2),
        "vol_10v50": r(v10v50, 2),
        "rvol": r(rvol, 2),
        "obv_63d": obv_read(closes, vols),
        "in_setup": bool(in_setup),
    }


# --------------------------------------------------------------------------
# backtest -- always against buy-and-hold
# --------------------------------------------------------------------------

def backtest(closes, buys, sells):
    """
    Long-only, all-in / all-out, executed on the bar after the signal (Chio's
    stated assumption), no transaction costs (also his). Reports the strategy's
    total return AND buy-and-hold over the identical window, because a strategy
    return quoted without that comparison is exactly the number Chio's paper
    cannot justify.
    """
    pos, entry, trades, wins, eq = False, 0.0, 0, 0, 1.0
    for i in range(len(closes) - 1):
        px = closes[i + 1]
        if not pos and buys[i]:
            pos, entry = True, px
        elif pos and sells[i]:
            r = px / entry - 1
            eq *= (1 + r)
            trades += 1
            wins += 1 if r > 0 else 0
            pos = False
    if pos:
        r = closes[-1] / entry - 1
        eq *= (1 + r)
        trades += 1
        wins += 1 if r > 0 else 0
    bh = closes[-1] / closes[0] - 1
    return {
        "strategy_return": round(eq - 1, 4),
        "buy_hold_return": round(bh, 4),
        "excess_vs_hold": round((eq - 1) - bh, 4),
        "beat_hold": (eq - 1) > bh,
        "n_trades": trades,
        "win_rate": round(wins / trades, 3) if trades else None,
    }


def strategy_signals(closes, highs, lows, opens, vols):
    """Chio's rules, plus his VPVMA. Keys match the names in his tables."""
    ml, sl, _ = macd(closes)
    r = rsi(closes)
    m = mfi(highs, lows, closes, vols)
    vl, vs = vpvma(highs, lows, closes, opens, vols)
    n = len(closes)
    out = {}

    def ok(*vals):
        return all(v is not None for v in vals)

    # MACD signal-line crossover
    b, s = [False] * n, [False] * n
    for i in range(1, n):
        if ok(ml[i], sl[i], ml[i - 1], sl[i - 1]):
            b[i] = ml[i - 1] < sl[i - 1] and ml[i] > sl[i]
            s[i] = ml[i - 1] > sl[i - 1] and ml[i] < sl[i]
    out["macd_crossover_sig"] = (b, s)

    # MACD signal-line crossover above zero (Chio's best single-MACD rule)
    b, s = [False] * n, [False] * n
    for i in range(1, n):
        if ok(ml[i], sl[i], ml[i - 1], sl[i - 1]):
            b[i] = ml[i - 1] < sl[i - 1] and ml[i] > sl[i] and ml[i] > 0
            s[i] = ml[i - 1] > sl[i - 1] and ml[i] < sl[i] and ml[i] < 0
    out["macd_sig_above_zero"] = (b, s)

    # MACD + RSI (his highest win rate, 0.78-0.86; his worst accumulated profit)
    b, s = [False] * n, [False] * n
    for i in range(6, n):
        w = [r[j] for j in range(i - 5, i + 1)]
        if ok(ml[i], sl[i], *w):
            b[i] = ml[i] > sl[i] and all(x <= 35 for x in w)
            s[i] = ml[i] < sl[i] and all(x >= 70 for x in w)
    out["macd_rsi"] = (b, s)

    # MACD + MFI
    b, s = [False] * n, [False] * n
    for i in range(6, n):
        w = [m[j] for j in range(i - 5, i + 1)]
        if ok(ml[i], sl[i], *w):
            b[i] = ml[i] > sl[i] and all(x <= 25 for x in w)
            s[i] = ml[i] < sl[i] and all(x >= 70 for x in w)
    out["macd_mfi"] = (b, s)

    # VPVMA (Chio Table 8), with the mirrored sell condition noted in vpvma()
    b, s = [False] * n, [False] * n
    bw = VPVMA_BANDWIDTH
    for i in range(1, n):
        if ok(vl[i], vs[i], vl[i - 1], vs[i - 1]):
            b[i] = vl[i] > (1 + bw) * vs[i] and vl[i - 1] <= vs[i - 1]
            s[i] = vl[i] < (1 - bw * 2) * vs[i] and vl[i - 1] >= vs[i - 1]
    out["vpvma"] = (b, s)
    return out


# --------------------------------------------------------------------------
# watchlist grouping -> relative-strength proxy
# --------------------------------------------------------------------------

# tickers.txt already carries the sector structure in its "# --- ... ---"
# headers, so the mapping is derived from it rather than duplicated here.
GROUP_PROXY = [
    (r"semiconduct|memory|storage|optic|network", "SMH"),
    (r"ai compute|datacenter", "SMH"),
    (r"power|energy", "SPY"),
    (r"miner|royalty|metals", "GDX"),
    (r"software", "IGV"),
    (r"macro|benchmark", None),
    (r"broad|thematic|crypto", "SPY"),
]


def load_groups(path=DEFAULT_TICKERS):
    """Map ticker -> (group label, relative-strength proxy) from tickers.txt."""
    groups, cur = {}, "Ungrouped"
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            hdr = re.match(r"^#\s*-{2,}\s*(.+?)\s*-{2,}\s*$", line.strip())
            if hdr:
                cur = hdr.group(1)
                continue
            sym = line.split("#", 1)[0].strip()
            if sym:
                proxy = "SPY"
                for pat, px in GROUP_PROXY:
                    if re.search(pat, cur, re.I):
                        proxy = px
                        break
                groups[sym.upper()] = {"group": cur, "proxy": proxy}
    return groups


def percentile_rank(value, population):
    """Fraction of the population at or below `value`, 0..1."""
    pop = [p for p in population if p is not None]
    if not pop or value is None:
        return None
    return round(sum(1 for p in pop if p <= value) / len(pop), 3)


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------

def load_bars(conn, ticker):
    rows = conn.execute(
        "SELECT date, open, high, low, close, volume, adj_close FROM daily_prices "
        "WHERE ticker=? AND close IS NOT NULL ORDER BY date",
        (ticker,),
    ).fetchall()
    return rows


def _load_evidence():
    """momentum_evidence.json if it has been generated; None otherwise."""
    try:
        with open(os.path.join(DEFAULT_OUT, "momentum_evidence.json"),
                  encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def build(db=DEFAULT_DB, tickers_file=DEFAULT_TICKERS):
    conn = sqlite3.connect(db)
    groups = load_groups(tickers_file)
    syms = [r[0] for r in conn.execute(
        "SELECT DISTINCT ticker FROM daily_prices ORDER BY ticker")]

    series, out, skipped = {}, {}, []
    for sym in syms:
        rows = load_bars(conn, sym)
        if len(rows) < MIN_BARS:
            skipped.append({"ticker": sym, "bars": len(rows), "reason": "insufficient_history"})
            continue
        dates = [r[0] for r in rows]
        opens = [r[1] if r[1] is not None else r[4] for r in rows]
        highs = [r[2] if r[2] is not None else r[4] for r in rows]
        lows = [r[3] if r[3] is not None else r[4] for r in rows]
        closes = [r[4] for r in rows]
        vols = [r[5] or 0 for r in rows]
        # adjusted closes drive return maths; raw closes drive levels and fills
        adjs = [r[6] if r[6] is not None else r[4] for r in rows]
        series[sym] = adjs

        ml, sl, hist = macd(closes)
        r = rsi(closes)
        m = mfi(highs, lows, closes, vols)
        vl, vs = vpvma(highs, lows, closes, opens, vols)
        gate = classify_regime(closes)

        sigs = strategy_signals(closes, highs, lows, opens, vols)
        bt = {k: backtest(closes, b, s) for k, (b, s) in sigs.items()}

        meta = groups.get(sym, {"group": "Ungrouped", "proxy": "SPY"})
        out[sym] = {
            "ticker": sym,
            "group": meta["group"],
            "proxy": meta["proxy"],
            "bars": len(closes),
            "first_date": dates[0],
            "last_date": dates[-1],
            "close": round(closes[-1], 4),
            "regime": gate,
            "indicators": {
                # Values are emitted regardless; `regime.trend_ok` /
                # `regime.oscillator_ok` say which of them are readable today.
                "macd": None if ml[-1] is None else round(ml[-1], 4),
                "macd_signal": None if sl[-1] is None else round(sl[-1], 4),
                "macd_hist": None if hist[-1] is None else round(hist[-1], 4),
                "macd_hist_prev": None if hist[-2] is None else round(hist[-2], 4),
                "rsi": None if r[-1] is None else round(r[-1], 2),
                "mfi": None if m[-1] is None else round(m[-1], 2),
                "vpvma": None if vl[-1] is None else round(vl[-1], 6),
                "vpvma_signal": None if vs[-1] is None else round(vs[-1], 6),
            },
            "momentum": momentum(closes, adjs),
            "setup": momentum_structure(highs, lows, closes, vols),
            "backtest": bt,
        }

    # cross-sectional ranks: universe-wide and within the tickers.txt group
    universe = [v["momentum"]["mom_12_1"] for v in out.values()]
    by_group = {}
    for v in out.values():
        by_group.setdefault(v["group"], []).append(v["momentum"]["mom_12_1"])
    for v in out.values():
        mom = v["momentum"]["mom_12_1"]
        v["momentum"]["rank_universe"] = percentile_rank(mom, universe)
        v["momentum"]["rank_group"] = percentile_rank(mom, by_group[v["group"]])
        # relative strength vs the group's proxy over 126 sessions
        px = v["proxy"]
        rs = None
        if px and px in series and len(series[px]) > 126 and len(series[v["ticker"]]) > 126:
            a, b = series[v["ticker"]], series[px]
            if a[-127] and b[-127]:
                rs = round((a[-1] / a[-127]) / (b[-1] / b[-127]) - 1, 6)
        v["momentum"]["rs_126_vs_proxy"] = rs

    # Cross-sectional regime layer (SPEC-8). Computed on the EQUITY trading-day
    # axis (SPY bars) from the same SQLite source -- resolves item #3 (the
    # client's crypto-inclusive union axis) at the source. Emitted ALONGSIDE the
    # per-ticker records (Phase 1): the browser still computes its own copy, so
    # this block can be diffed against the live client before the render flips.
    # The receipts pool is the emitted (>=MIN_BARS) technicals set, mirroring the
    # client's TECH.tickers filter.
    try:
        regime_block = regime.build_regime(conn, set(out.keys()))
        # Leg readability gate (v60, framework-review item 5): the composite's
        # EMA-slope legs (types ratio/trend) assume their input is TRENDING --
        # the same H&M assumption MACD carries. Run classify_regime (the
        # Monte-Carlo-calibrated gate above) on each such leg's continuous
        # input and DISCLOSE the result. Votes are NOT zeroed: at the p90 R^2
        # bar most 90-session windows are "neither", so gating the math would
        # delete the composite -- instead the client dims an unreadable leg's
        # vote. Attached HERE (not in regime.build_regime) deliberately: the
        # classifier is technicals.py's and already has its own H&M generator
        # tests, and keeping it out of build_regime keeps the JS-oracle parity
        # surface (regime_e2e_parity.py) exactly as it was.
        if isinstance(regime_block, dict) and regime_block.get("legs"):
            for _leg in regime_block["legs"]:
                if _leg.get("type") not in ("ratio", "trend"):
                    continue
                _tail = [v for v in _leg.get("cont", [])[-REGIME_WINDOW:]]
                if len(_tail) < REGIME_WINDOW or any(v is None or v <= 0 for v in _tail):
                    _leg["gate"] = {"regime": "insufficient_history", "r2": None,
                                    "readable": False}
                    continue
                _g = classify_regime(_tail)
                _leg["gate"] = {"regime": _g["regime"], "r2": _g.get("r2"),
                                "readable": bool(_g.get("trend_ok"))}
    except Exception as exc:  # never let the regime layer break the per-ticker output
        regime_block = {"error": f"{type(exc).__name__}: {exc}"}

    conn.close()
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "method": {
            "regime_gate": (
                f"Over the last {REGIME_WINDOW} sessions: trending if log-price R^2 >= "
                f"{R2_TREND_MIN}; mean_reverting if R^2 < {R2_REVERT_MAX} and SMA{CROSS_SMA} "
                f"crossings <= {CROSS_REVERT_MAX} per 100 sessions; else neither."
            ),
            "regime_calibration": (
                "Thresholds are Monte-Carlo calibrated against a random-walk null (2,000 draws, "
                f"90-session windows). R^2 >= {R2_TREND_MIN} is the p90 of that null -- note that "
                "R^2 >= 0.50 would be met by 44.7% of pure random walks, which is why the common "
                "'it looks trendy' threshold is useless. The Lo-MacKinlay variance ratio is "
                "reported as a diagnostic but does NOT classify: a sinusoid is locally smooth, so "
                "cyclical data returns VR of 3.9-11.3, far above 1, not below it."
            ),
            "regime_error_rates": {
                "false_trending_on_random_walk": GATE_FALSE_TREND,
                "false_mean_reverting_on_random_walk": GATE_FALSE_REVERT,
                "note": "Over 90 sessions a random walk can genuinely look like a trend. "
                        "These are the residual rates at the chosen thresholds.",
            },
            "gate_rationale": (
                "Hurwitz & Marwala 2011 (arXiv:1110.3383): MACD assumes a trend, RSI assumes a "
                "cycle, and those assumptions are mutually exclusive. Read outside its regime an "
                "indicator is noise with a number attached, so trend_ok / oscillator_ok gate them."
            ),
            "indicators": (
                "MACD(12,26,9), RSI(14), MFI(14), VPVMA per Chio 2022 (arXiv:2206.12282) s4.1. "
                "Parameters are fixed deliberately: Chio's genetic search returned a different "
                "'optimum' per index and he calls it overfitting."
            ),
            "backtest": (
                "Long-only, all-in/all-out, executed the bar after the signal, zero transaction "
                "cost -- Chio's own assumptions -- but ALWAYS reported against buy-and-hold over "
                "the same window, which his paper omits."
            ),
            "momentum_structure": (
                "Three orthogonal families per name: trend structure (close>EMA20>50>200, "
                "confirmed-pivot HH/HL, 20d close position), volatility state (Wilder ATR14% "
                "ranked within the name's own trailing year), participation (50d up/down-day "
                "volume ratio, OBV 63d, RVOL). in_setup = stacked + coiled(<=p20) + U/D>=1.2. "
                "Deliberately no N-of-12 composite: screener chips are heavily correlated and "
                "a count manufactures confidence. momentum_evidence carries the base rates of "
                "these states on this book's own history."
            ),
            "not_advice": "Descriptive only. No position sizing, no recommendation.",
        },
        "min_bars": MIN_BARS,
        # Event-study base rates for the setup states, generated by
        # momentum_evidence.py (slow, run on demand) and inlined here so the
        # dashboard can print evidence next to the states it displays.
        "momentum_evidence": _load_evidence(),
        "count": len(out),
        "skipped": skipped,
        "tickers": out,
        # Cross-sectional risk-appetite composite, sector-rotation ladder and
        # 12-1 rank receipts (SPEC-8). See regime.py; parity with the client
        # engine (regime_core.js) is pinned by test_technicals.py.
        "regime": regime_block,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Compute regime-gated technicals.")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--tickers", default=DEFAULT_TICKERS)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--stdout", action="store_true", help="print instead of writing")
    args = ap.parse_args()

    payload = build(args.db, args.tickers)
    if args.stdout:
        print(json.dumps(payload, indent=2))
        return 0
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "technicals.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    print(f"Wrote {payload['count']} tickers -> {path} "
          f"({len(payload['skipped'])} skipped for short history)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
