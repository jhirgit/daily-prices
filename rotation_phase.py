#!/usr/bin/env python3
"""
SPEC-86 Part B + C, daily: the phase of every sector-ladder row and its
row-specific checks, written to data/rotation_phase.json (contract: spec s10).

    python rotation_phase.py              # prices.db + one batched yfinance fetch
    python rotation_phase.py --no-fetch   # offline: drivers not in prices.db -> n/a

ONE IMPLEMENTATION. ``phase_frame`` / ``labels`` / ``measure`` / ``status_codes``
/ ``ext_frame`` below are the only implementation of spec s4-s5. They are
vectorised over a whole close history and every value at index i reads bars at
<= i only, so ``rotation_evidence.py`` imports them unchanged to walk 25+ years
of history, and the evidence describes exactly the states the board shows. The
daily run reads the LAST element of the same arrays.

WHAT THE WORDS MEAN (spec s2, binding). A phase is a STATE -- a description of
what the price has done over its own trailing windows -- never a forecast. Any
base rate attached here comes from ``data/rotation_evidence.json`` with its n.
No generated sentence contains buy, sell, should, will, likely, target or
recommend (test_rotation_phase.py enforces it on every row's text).

DATA. Row closes come from prices.db exactly as the ladder sees them
(``regime.build_panel`` on the SPY session axis; baskets via
``regime.basket_series``). A check input is read from the same panel when
prices.db holds at least ``PANEL_MIN_BARS`` sessions of it; otherwise it comes
from ONE batched yfinance download (2y of daily closes, adjusted), as-of
aligned onto the same axis. Those symbols are NOT added to tickers.txt (which
drives the per-name technicals). Holdings-based checks read worker 1's
``data/rotation_members.json`` (spec s10) and are ``n/a`` without it; the
extension check reads ``data/crowd.json``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

import regime as RG
import rotation_theses as TH

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
DB_PATH = os.path.join(HERE, "prices.db")
OUT_PATH = os.path.join(DATA_DIR, "rotation_phase.json")
EVIDENCE_PATH = os.path.join(DATA_DIR, "rotation_evidence.json")
MEMBERS_PATH = os.path.join(DATA_DIR, "rotation_members.json")
CROWD_PATH = os.path.join(DATA_DIR, "crowd.json")

# ---------------------------------------------------------------------------
# Spec s4 constants -- declared in SPEC-86 s4; a threshold changes only by a
# dated amendment to the spec.
# ---------------------------------------------------------------------------
TREND_W = 126          # OLS window for the trend slope and its t-stat (sessions)
T_TREND = 2.0          # up = t >= +2, down = t <= -2, flat otherwise
ACC_DZ = 0.25          # dead zone on a1 / a2 (units of the row's own 126d vol)
SMA_SHORT = 50
SMA_LONG = 200
STRUCT_W = 63          # 63d high/low vs the prior 63d high/low
VOL_W = 126            # the row's own volatility window
MIN_PHASE_BARS = TREND_W + 1   # p126 needs a 126-session lag

# Spec s5 constants.
CHECK_DZ = 0.5         # dead zone = 0.5 x the measure's own window volatility
SIGMA_LB = 252         # trailing sessions of daily changes behind that volatility
SIGMA_MIN = 126
PART_BAND = 0.10       # participation: share_up above 0.60 / below 0.40 (no holdings
                       # history exists to scale this by -- declared, not fitted)
CONC_NARROW = 1.5      # s3.3: above 1.5 the move is narrower than the fund
CONC_BROAD = 0.8
EXT_LB = 756           # 3y of sessions for the extension percentile
EXT_MIN = 252
EXT_HI = 0.90
EXT_LO = 0.10

# A check input is read from prices.db when it has at least this many sessions
# there; otherwise it joins the one batched yfinance fetch.
PANEL_MIN_BARS = 400
FETCH_PERIOD = "2y"
MAX_STALE = RG.MAX_STALE_SESSIONS

LABELS = [
    ("accelerating_up", "Accelerating up"),
    ("rolling_over", "Rolling over"),
    ("slowing_up", "Slowing up"),
    ("steady_up", "Steady up"),
    ("accelerating_down", "Accelerating down"),
    ("turning_up", "Turning up"),
    ("slowing_down", "Slowing down"),
    ("steady_down", "Steady down"),
    ("flat", "Flat"),
]
LABEL_KEYS = [k for k, _ in LABELS]
WORD = dict(LABELS)
TREND_WORD = {1: "up", -1: "down", 0: "flat"}
DIRECTIONAL = ("confirms", "diverges", "neutral", "leans_up", "leans_down")

BANNED = re.compile(r"\b(buy\w*|sell\w*|should|will|likely|target\w*|recommend\w*)\b", re.I)


# ===========================================================================
# vector helpers (trailing-only by construction)
# ===========================================================================

def _arr(x):
    """list-with-None / array -> float ndarray with NaN."""
    return np.array([np.nan if v is None else v for v in x], dtype=float) \
        if isinstance(x, list) else np.asarray(x, dtype=float)


def _log(x):
    x = np.asarray(x, dtype=float)
    out = np.full(x.shape, np.nan)
    ok = x > 0
    out[ok] = np.log(x[ok])
    return out


def _lag(x, k):
    out = np.full(len(x), np.nan)
    if k < len(x):
        out[k:] = x[:len(x) - k]
    return out


def _roll(x, w, how, min_periods=None):
    s = pd.Series(x)
    r = s.rolling(w, min_periods=w if min_periods is None else min_periods)
    return getattr(r, how)(ddof=1).to_numpy() if how == "std" else getattr(r, how)().to_numpy()


# ===========================================================================
# spec s4 -- the phase
# ===========================================================================

def phase_frame(close):
    """Every s4 quantity for every session of `close` (NaN before it is
    computable). Index i reads closes at <= i only.

    t126    OLS t-stat of log price on time over the last 126 sessions (the
            plain OLS standard error, as the spec declares it)
    slope   that slope, annualised (x252)
    p21/p63/p126   annualised log returns (x252/w)
    sigma   126d annualised volatility of daily log returns
    a1, a2  (p21 - p63)/sigma, (p63 - p126)/sigma
    above50 close > SMA50 (1/0, NaN before the SMA exists)
    vs200   close / SMA200 - 1
    lower_high / higher_low   the last 63d high (low) vs the prior 63d high (low)
    trend   +1 / 0 / -1 by t126 against T_TREND
    """
    c = _arr(close)
    n = len(c)
    lp = _log(c)
    f = {}

    slope = np.full(n, np.nan)
    tst = np.full(n, np.nan)
    W = TREND_W
    if n >= W:
        win = sliding_window_view(lp, W)
        x = np.arange(W, dtype=float) - (W - 1) / 2.0
        sxx = float((x * x).sum())
        ybar = win.mean(axis=1)
        b = (win * x).sum(axis=1) / sxx
        ssy = ((win - ybar[:, None]) ** 2).sum(axis=1)
        s2 = np.maximum(ssy - b * b * sxx, 0.0) / (W - 2)
        with np.errstate(divide="ignore", invalid="ignore"):
            t = b / np.sqrt(s2 / sxx)
        t[s2 == 0] = np.nan
        slope[W - 1:] = b * 252.0
        tst[W - 1:] = t
    f["t126"] = tst
    f["slope"] = slope

    for w in (21, 63, 126):
        f[f"p{w}"] = (lp - _lag(lp, w)) * 252.0 / w
    r = np.full(n, np.nan)
    r[1:] = lp[1:] - lp[:-1]
    f["sigma"] = _roll(r, VOL_W, "std") * math.sqrt(252.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        f["a1"] = (f["p21"] - f["p63"]) / f["sigma"]
        f["a2"] = (f["p63"] - f["p126"]) / f["sigma"]

    sma50 = _roll(c, SMA_SHORT, "mean")
    sma200 = _roll(c, SMA_LONG, "mean")
    above = np.where(np.isnan(sma50) | np.isnan(c), np.nan, (c > sma50).astype(float))
    f["above50"] = above
    with np.errstate(divide="ignore", invalid="ignore"):
        f["vs200"] = c / sma200 - 1.0

    hi = _roll(c, STRUCT_W, "max")
    lo = _roll(c, STRUCT_W, "min")
    hi_prev = _lag(hi, STRUCT_W)
    lo_prev = _lag(lo, STRUCT_W)
    f["lower_high"] = np.where(np.isnan(hi) | np.isnan(hi_prev), np.nan, (hi < hi_prev).astype(float))
    f["higher_low"] = np.where(np.isnan(lo) | np.isnan(lo_prev), np.nan, (lo > lo_prev).astype(float))

    trend = np.full(n, np.nan)
    ok = ~np.isnan(tst)
    trend[ok] = np.where(tst[ok] >= T_TREND, 1.0, np.where(tst[ok] <= -T_TREND, -1.0, 0.0))
    f["trend"] = trend
    return f


def labels(f):
    """Spec s4 label codes (index into LABEL_KEYS; -1 = not computable), first
    match wins, in the spec's order."""
    need = ("trend", "a1", "a2", "p21", "above50", "lower_high", "higher_low")
    n = len(f["trend"])
    valid = np.ones(n, dtype=bool)
    for k in need:
        valid &= ~np.isnan(f[k])
    tr = np.nan_to_num(f["trend"])
    a1 = np.nan_to_num(f["a1"])
    a2 = np.nan_to_num(f["a2"])
    p21 = np.nan_to_num(f["p21"])
    ab = np.nan_to_num(f["above50"]) > 0.5
    lh = np.nan_to_num(f["lower_high"]) > 0.5
    hl = np.nan_to_num(f["higher_low"]) > 0.5
    up, dn, fl = tr > 0.5, tr < -0.5, np.abs(tr) < 0.5
    conds = [
        up & (a1 > ACC_DZ) & (a2 >= -ACC_DZ) & ab,           # accelerating up
        up & (p21 < 0) & ~ab & lh,                           # rolling over
        up & (a1 < -ACC_DZ) & (ab | (~ab & ~lh)),            # slowing up
        up,                                                  # steady up
        dn & (a1 < -ACC_DZ) & (a2 <= ACC_DZ) & ~ab,          # accelerating down
        (dn | fl) & (p21 > 0) & ab & hl,                     # turning up
        dn & (a1 > ACC_DZ) & ~ab,                            # slowing down
        dn,                                                  # steady down
        fl,                                                  # flat
    ]
    code = np.select(conds, list(range(len(conds))), default=-1)
    code[~valid] = -1
    return code


def run_start(codes, i):
    """First index of the run of equal codes ending at i."""
    j = i
    while j > 0 and codes[j - 1] == codes[i]:
        j -= 1
    return j


# ===========================================================================
# spec s5 -- the checks
# ===========================================================================

def measure(kind, xs, window, unit=None, threshold=0.0):
    """The measure m (window change) and its window volatility sdw for a
    price-based check, over the whole history. `xs` are the aligned input
    arrays (already FX-converted). Trailing-only.

      driver  log change of X (a yield: change in bp)
      ratio   log change of A/B
      curve   change in bp of (long - short)
      level   (A - B - threshold) itself; sdw = the std of that level over the
              trailing window (a level has no 'change' to scale by)
    For every kind except level, sdw = std(daily changes, trailing SIGMA_LB,
    min SIGMA_MIN) x sqrt(window), so dz = CHECK_DZ x sdw is 'half of the
    measure's own window volatility' -- comparable bars for copper and the curve.
    """
    if kind == "level":
        a, b = _arr(xs[0]), _arr(xs[1])
        lev = a - b - threshold
        return lev, _roll(lev, window, "std", min_periods=max(10, window // 2))
    if kind == "driver":
        x = _arr(xs[0])
        v = x * 100.0 if unit == "bp" else _log(x)
    elif kind == "ratio":
        v = _log(xs[0]) - _log(xs[1])
    elif kind == "curve":
        v = (_arr(xs[0]) - _arr(xs[1])) * 100.0
    else:
        raise ValueError(f"not a price-based kind: {kind}")
    m = v - _lag(v, window)
    d = np.full(len(v), np.nan)
    d[1:] = v[1:] - v[:-1]
    sd = _roll(d, SIGMA_LB, "std", min_periods=SIGMA_MIN)
    return m, sd * math.sqrt(window)


def status_codes(m, sdw, s):
    """+1 when s*m > +dz, -1 when s*m < -dz, 0 inside the dead zone, NaN when
    not computable. dz = CHECK_DZ x sdw."""
    m = np.asarray(m, float)
    sdw = np.asarray(sdw, float)
    out = np.full(len(m), np.nan)
    ok = ~np.isnan(m) & ~np.isnan(sdw) & (sdw > 0)
    z = np.zeros(len(m))
    z[ok] = s * m[ok] / sdw[ok]
    out[ok] = np.where(z[ok] > CHECK_DZ, 1.0, np.where(z[ok] < -CHECK_DZ, -1.0, 0.0))
    return out


def status_word(code, trend):
    """Spec s5's direction-aware table. `code` from status_codes; `trend` +1/0/-1."""
    if code is None or trend is None or (isinstance(code, float) and math.isnan(code)) \
            or (isinstance(trend, float) and math.isnan(trend)):
        return "n/a"
    code, trend = int(code), int(trend)
    if code == 0:
        return "neutral"
    if trend > 0:
        return "confirms" if code > 0 else "diverges"
    if trend < 0:
        return "diverges" if code > 0 else "confirms"
    return "leans_up" if code > 0 else "leans_down"


def status_word_array(codes, trend):
    """Vectorised status_word: 1 confirms, -1 diverges, 0 neutral/lean-neutral,
    2 leans_up, -2 leans_down, NaN n/a. (Used by the calibration.)"""
    codes = np.asarray(codes, float)
    trend = np.asarray(trend, float)
    out = np.full(len(codes), np.nan)
    ok = ~np.isnan(codes) & ~np.isnan(trend)
    c, t = codes[ok], trend[ok]
    w = np.where(c == 0, 0.0,
                 np.where(t > 0, np.where(c > 0, 1.0, -1.0),
                          np.where(t < 0, np.where(c > 0, -1.0, 1.0),
                                   np.where(c > 0, 2.0, -2.0))))
    out[ok] = w
    return out


def ext_frame(close):
    """ext200 = close/SMA200 - 1, and its percentile within the row's own
    trailing EXT_LB sessions (share of the trailing values <= today's, today
    included; NaN until EXT_MIN values exist). Trailing-only."""
    c = _arr(close)
    n = len(c)
    sma = _roll(c, SMA_LONG, "mean")
    with np.errstate(divide="ignore", invalid="ignore"):
        ext = c / sma - 1.0
    pct = np.full(n, np.nan)
    if n:
        pad = np.concatenate([np.full(EXT_LB - 1, np.nan), ext])
        win = sliding_window_view(pad, EXT_LB)          # row i = ext[i-EXT_LB+1 .. i]
        cnt = np.sum(~np.isnan(win), axis=1)
        with np.errstate(invalid="ignore"):
            le = np.sum(win <= ext[:, None], axis=1)
        ok = (cnt >= EXT_MIN) & ~np.isnan(ext)
        pct[ok] = le[ok] / cnt[ok]
    return ext, pct


def ext_quantiles(ext, pct_levels=None):
    """The trailing-EXT_LB distribution of ext200 at the last bar, as quantiles
    (the evidence file stores these so the daily run can place today's value in
    a 3y range that prices.db's two years cannot hold)."""
    if pct_levels is None:
        pct_levels = [i / 20.0 for i in range(21)]
    tail = np.asarray(ext, float)[-EXT_LB:]
    tail = tail[~np.isnan(tail)]
    if len(tail) < EXT_MIN:
        return None
    return [round(float(np.quantile(tail, q)), 5) for q in pct_levels]


def pct_from_quantiles(x, q):
    """Percentile of x within a distribution given as 21 quantiles (0,5,...,100%)."""
    if x is None or q is None or (isinstance(x, float) and math.isnan(x)):
        return None
    levels = np.linspace(0.0, 1.0, len(q))
    qa = np.asarray(q, float)
    if x <= qa[0]:
        return 0.0
    if x >= qa[-1]:
        return 1.0
    return float(np.interp(x, qa, levels))


# ===========================================================================
# text helpers
# ===========================================================================

def _t(s):
    return s.replace(" -- ", " — ") if isinstance(s, str) else s


def _num(x, d=4):
    if x is None:
        return None
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(xf) or math.isinf(xf):
        return None
    return round(xf, d)


def _signed(x, fmt="{:+.1f}"):
    return fmt.format(x).replace("-", "−")


def _pct(x):
    return _signed(x * 100.0) + "%"


def _md(d):
    return f"{int(d[5:7])}/{int(d[8:10])}"


def _ordinal(k):
    k = int(k)
    if 10 <= k % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(k % 10, "th")
    return f"{k}{suf}"


def short_of(c):
    """The compact symbol expression of a price-based check ('GDX/GLD', '^TNX')."""
    k = c["kind"]
    if k in ("ratio",):
        return f"{c['inputs'][0]}/{c['inputs'][1]}"
    if k in ("curve", "level"):
        return f"{c['inputs'][0]} − {c['inputs'][1]}"
    return c["inputs"][0] if c["inputs"] else c["id"]


def value_text(c, m, cur=None):
    """Plain value string for a price-based check's current measure."""
    k, w = c["kind"], c["window"]
    if m is None or (isinstance(m, float) and math.isnan(m)):
        return "n/a"
    if k == "level":
        state = "above" if m + c.get("threshold", 0.0) > 0 else "below"
        return f"{_signed(m, '{:+.2f}')} pts ({c['inputs'][0]} {state} {c['inputs'][1]})"
    if k == "curve":
        now = f", now {_signed(cur, '{:+.0f}')}bp" if cur is not None else ""
        return f"{_signed(m, '{:+.0f}')}bp over {w}d{now}"
    if c.get("unit") == "bp":
        now = f", now {cur:.2f}%" if cur is not None else ""
        return f"{_signed(m, '{:+.0f}')}bp over {w}d{now}"
    return f"{_pct(math.exp(m) - 1.0)} over {w}d"


# ===========================================================================
# holdings-based checks (worker 1's rotation_members.json, spec s10)
# ===========================================================================

def _norm(t):
    return (t or "").strip().upper()


def _hold_index(mrow):
    return {_norm(h.get("t")): h for h in (mrow.get("holdings") or [])}


def conc_ratio(mrow, names, R, sig_w, key="r63"):
    """Spec s3.3 concentration for a named set (or the top 3 by weight when
    names is empty): (their share of the window's contribution) / (their share
    of start-of-window weight). Start weights are backed out as s3.2 does:
    w0 = w (1+R)/(1+r). None with a reason when the move is under 1 sigma of the
    row's own window volatility, or the names are not in the reported holdings."""
    hold = mrow.get("holdings") or []
    if not hold:
        return None, "no holdings reported", []
    if names:
        idx = _hold_index(mrow)
        pick = [idx[_norm(n)] for n in names if _norm(n) in idx]
    else:
        pick = sorted(hold, key=lambda h: -(h.get("w") or 0))[:3]
    if not pick:
        return None, "none of these names is in the fund's reported holdings", []
    who = [h.get("t") for h in pick]
    if R is None or sig_w is None or not math.isfinite(R):
        return None, "row return unavailable", who
    if abs(R) < sig_w:
        return None, "too small a move to attribute (under 1σ of its own 63d volatility)", who
    cs = 0.0
    w0 = 0.0
    for h in pick:
        r = (h.get("r") or {}).get(key)
        c = (h.get("c") or {}).get(key)
        w = h.get("w")
        if r is None or c is None or w is None or r <= -1:
            return None, f"{h.get('t')} has no {key[1:]}d return", who
        cs += c
        w0 += w * (1.0 + R) / (1.0 + r)
    if w0 <= 0:
        return None, "no starting weight", who
    return (cs / R) / w0, None, who


def split_contrib(mrow, groups, key="r63"):
    """Contribution (in return units) of each named group over `key`; a group
    with members None is the rest of the fund (R minus the named groups, so the
    groups always sum to the ladder row's own return)."""
    R = (mrow.get("R") or {}).get(key)
    idx = _hold_index(mrow)
    out = []
    named_sum = 0.0
    missing_c = False
    for g in groups:
        if g["members"] is None:
            out.append({"name": g["name"], "c": None, "found": None})
            continue
        found = [idx[_norm(m)] for m in g["members"] if _norm(m) in idx]
        cs = 0.0
        for h in found:
            c = (h.get("c") or {}).get(key)
            if c is None:
                missing_c = True
                continue
            cs += c
        named_sum += cs
        out.append({"name": g["name"], "c": cs if found else None,
                    "found": [h.get("t") for h in found]})
    for o in out:
        if o["found"] is None:
            o["c"] = (R - named_sum) if R is not None else None
    return R, out, missing_c


# ===========================================================================
# the daily build
# ===========================================================================

def load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def align_to_axis(dates, s):
    """As-of align a date-indexed Series onto the SPY session axis (latest value
    on or before each session), stopping MAX_STALE sessions past its last real
    value -- the same rule regime.build_panel applies."""
    s = s.dropna()
    if s.empty:
        return np.full(len(dates), np.nan)
    idx = pd.Index(pd.to_datetime(dates))
    s.index = pd.to_datetime(s.index).tz_localize(None) if getattr(s.index, "tz", None) else pd.to_datetime(s.index)
    out = s.reindex(s.index.union(idx)).ffill().reindex(idx).to_numpy(dtype=float)
    last = s.index[-1]
    last_pos = int(np.searchsorted(idx.values, np.datetime64(last), side="right")) - 1
    if last_pos + MAX_STALE + 1 < len(out):
        out[last_pos + MAX_STALE + 1:] = np.nan
    return out


def fetch_yf(symbols, period=FETCH_PERIOD, start=None):
    """One batched yfinance download of adjusted daily closes. Returns
    {sym: Series} and {sym: error}."""
    import warnings
    warnings.filterwarnings("ignore")
    import yfinance as yf
    got, errs = {}, {}
    if not symbols:
        return got, errs
    kw = {"start": start} if start else {"period": period}
    try:
        df = yf.download(sorted(symbols), auto_adjust=True, progress=False, threads=True, **kw)
    except Exception as e:  # network / API failure: every symbol is n/a, never a crash
        return got, {s: f"yfinance download failed: {e.__class__.__name__}" for s in symbols}
    close = df["Close"] if isinstance(df.columns, pd.MultiIndex) else df[["Close"]].rename(columns={"Close": sorted(symbols)[0]})
    for s in symbols:
        if s in close.columns and close[s].notna().sum() > 0:
            got[s] = close[s].dropna()
        else:
            errs[s] = "no bars returned"
    return got, errs


class Inputs:
    """Resolves a check input symbol to an array on the axis: a ladder basket ->
    regime.basket_series over prices.db; a symbol with >= PANEL_MIN_BARS in
    prices.db -> the panel; otherwise the batched yfinance fetch."""

    def __init__(self, dates, series, fetched):
        self.dates = dates
        self.series = series
        self.fetched = fetched
        self.cache = {}

    def get(self, sym):
        if sym in self.cache:
            return self.cache[sym]
        if sym in TH.BASKETS:
            b = RG.basket_series(self.series, TH.BASKETS[sym])
            a = _arr(b) if b else None
        elif sym in self.series and sum(v is not None for v in self.series[sym]) >= PANEL_MIN_BARS:
            a = _arr(self.series[sym])
        elif sym in self.fetched:
            a = align_to_axis(self.dates, self.fetched[sym].copy())
        elif sym in self.series:
            a = _arr(self.series[sym])
        else:
            a = None
        self.cache[sym] = a
        return a

    def row(self, t):
        """A ladder row's own closes: prices.db only (baskets via basket_series),
        exactly what the ladder ranks -- never the yfinance fetch."""
        if t in TH.BASKETS:
            return self.get(t)
        return _arr(self.series[t]) if t in self.series else None

    def needs_fetch(self, syms):
        out = set()
        for s in syms:
            if s in TH.BASKETS:
                continue
            if s not in self.series or sum(v is not None for v in self.series[s]) < PANEL_MIN_BARS:
                out.add(s)
        return out


def check_inputs(c, inp):
    xs = []
    for s in c["inputs"]:
        a = inp.get(s)
        if a is None:
            return None, f"no price series for {s}"
        xs.append(a)
    if c.get("fx"):
        fx = inp.get(c["fx"])
        if fx is None:
            return None, f"no FX series {c['fx']} to convert {c['inputs'][0]}"
        xs[0] = xs[0] * fx
    return xs, None


def evidence_cell(ev, family, kind, trend, t=None):
    """Pick the kind x family cell for this trend (falling back to the pooled
    'all' cell when the family cell has under 20 episodes on either side)."""
    if t == "SPY":
        return {"tag": "uncalibrated",
                "note": "SPY is the benchmark: outcomes are measured relative to it"}
    if not ev:
        return {"tag": "uncalibrated", "note": "rotation_evidence.json not present"}
    tw = TREND_WORD.get(trend)
    if tw is None:
        return {"tag": "uncalibrated", "note": "no trend"}
    fam = (((ev.get("checks") or {}).get("by_family") or {}).get(family) or {}).get(kind, {}).get(tw)
    allc = (((ev.get("checks") or {}).get("all") or {}).get(kind) or {}).get(tw)
    pool = f"{kind} × {family}, {tw}-trends"
    cell = fam
    if not fam or min(fam.get("n_conf") or 0, fam.get("n_div") or 0) < 20:
        cell, pool = allc, f"{kind}, all families, {tw}-trends"
    if not cell:
        return {"tag": "uncalibrated", "note": "no calibrated cell for this kind"}
    out = {"tag": cell.get("tag", "descriptive"), "n_conf": cell.get("n_conf"),
           "n_div": cell.get("n_div"), "med_conf": cell.get("med_conf"), "med_div": cell.get("med_div"),
           "diff": cell.get("diff"), "ci": cell.get("ci"), "ci_time": cell.get("ci_time"), "pool": pool}
    if cell.get("note"):
        out["note"] = cell["note"]
    return out


def phase_evidence(ev, t, family, label):
    if not ev or not label:
        return None
    row = (((ev.get("rows") or {}).get(t) or {}).get("labels") or {}).get(label) or {}
    if TH.primary_of(t) == "SPY":
        # the pooled cells are relative to SPY; for SPY itself only its own
        # absolute history means anything
        return {"episodes": row.get("episodes", 0), "persist": row.get("persist"),
                "med_rel63": None, "med_abs63": row.get("med_abs63"), "days": row.get("days", 0),
                "pool": "SPY's own history (absolute: SPY is the benchmark)",
                "row": {"episodes": row.get("episodes", 0), "days": row.get("days", 0),
                        "med_abs63": row.get("med_abs63"), "persist": row.get("persist")}}
    ph = ev.get("phase") or {}
    fam = ((ph.get("by_family") or {}).get(family) or {}).get(label)
    allc = (ph.get("all") or {}).get(label)
    cell, pool = fam, f"family: {family}"
    if not fam or (fam.get("episodes") or 0) < 20:
        cell, pool = allc, "all families"
    if not cell:
        return None
    return {"episodes": cell.get("episodes"), "persist": cell.get("persist"),
            "med_rel63": cell.get("med_rel63"), "days": cell.get("days"),
            "share_pos": cell.get("share_pos"), "share_abs_pos": cell.get("share_abs_pos"),
            "ci90": cell.get("ci90"), "ci90_time": cell.get("ci90_time"), "pool": pool,
            "row": {"episodes": row.get("episodes", 0), "days": row.get("days", 0),
                    "med_rel63": row.get("med_rel63"), "persist": row.get("persist")}}


def build_checks(t, trend_now, fam, inp, mrow, crow, ev, sig_ann, ext_q, row_close, errors,
                 members_loaded=None, ext_n=None):
    """Evaluate row t's check list at the last session. `trend_now` is the
    row's OWN trend (+1/0/-1 or None)."""
    if members_loaded is None:
        members_loaded = mrow is not None
    no_members = ("rotation_members.json not present" if not members_loaded
                  else f"no holdings for {t} in rotation_members.json")
    out = []
    for c in TH.checks_for(t):
        k = c["kind"]
        rec = {"id": c["id"], "kind": k, "label": _t(c["label"]), "window": c["window"],
               "s": c["s"], "m": None, "z": None, "status": "n/a", "value": "n/a", "note": ""}
        if c.get("inputs") and k in ("driver", "ratio", "curve", "level"):
            rec["inputs"] = list(c["inputs"])
        if k in ("driver", "ratio", "curve", "level"):
            xs, why = check_inputs(c, inp)
            if xs is None:
                rec["note"] = why
                errors.setdefault(t, []).append(f"{c['id']}: {why}")
            else:
                m, sdw = measure(k, xs, c["window"], c.get("unit"), c.get("threshold", 0.0))
                mv, sv = m[-1], sdw[-1]
                cur = None
                if k == "curve":
                    cur = (xs[0][-1] - xs[1][-1]) * 100.0
                elif c.get("unit") == "bp":
                    cur = xs[0][-1]
                if math.isnan(mv) or math.isnan(sv) or sv <= 0:
                    rec["note"] = "not enough history for this window"
                else:
                    z = mv / sv
                    rec["m"], rec["z"] = _num(mv, 5), _num(z, 3)
                    rec["value"] = value_text(c, mv, None if cur is None or math.isnan(cur) else cur)
                    if c["s"] == 0:
                        rec["status"] = "info"
                        rec["note"] = _t(c.get("why", ""))
                    else:
                        # the same status_codes the calibration walks history with
                        code = status_codes(m[-1:], sdw[-1:], c["s"])[0]
                        rec["status"] = status_word(code, trend_now)
                        if rec["status"] == "diverges":
                            rec["note"] = _t(c["up_div"] if trend_now > 0 else c["dn_div"])
            if c["s"] == 0:
                rec["evidence"] = {"tag": "uncalibrated", "note": "informational: no expected sign"}
            else:
                rec["evidence"] = evidence_cell(ev, fam, k, trend_now, TH.primary_of(t))
        elif k == "participation":
            p63 = ((mrow or {}).get("participation") or {}).get("r63") or {}
            p21 = ((mrow or {}).get("participation") or {}).get("r21") or {}
            su = p63.get("share_up")
            if mrow is None:
                rec["note"] = no_members
            elif su is None:
                rec["note"] = "no 63d participation for this row"
            else:
                rec["m"] = _num(su, 4)
                code = 1 if su > 0.5 + PART_BAND else (-1 if su < 0.5 - PART_BAND else 0)
                rec["status"] = status_word(code, trend_now)
                v = f"{su * 100:.0f}% of covered weight up over 63d"
                if p63.get("n_up") is not None and p63.get("n"):
                    v += f" · {p63['n_up']} of {p63['n']} names"
                if p21.get("share_up") is not None:
                    v += f" · {p21['share_up'] * 100:.0f}% over 21d"
                rec["value"] = v
                if rec["status"] == "diverges":
                    rec["note"] = _t(c["up_div"] if trend_now > 0 else c["dn_div"])
            rec["evidence"] = {"tag": "uncalibrated", "note": "no holdings history: cannot be calibrated"}
        elif k == "concentration":
            rec["status"] = "info"
            rec["note"] = _t(c.get("why", ""))
            if mrow is None:
                rec["status"], rec["note"] = "n/a", no_members
            else:
                R = (mrow.get("R") or {}).get("r63")
                sig_w = sig_ann * math.sqrt(63 / 252.0) if sig_ann is not None and math.isfinite(sig_ann) else None
                ratio = None
                why = None
                who = []
                p63 = (mrow.get("participation") or {}).get("r63") or {}
                if not c["inputs"] and "conc_top3" in p63:
                    # the top 3 by weight: worker 1's own number (and its own 1-sigma
                    # gate), so this check agrees with the holdings panel above it
                    ratio = p63.get("conc_top3")
                    who = [h.get("t") for h in sorted(mrow.get("holdings") or [],
                                                      key=lambda h: -(h.get("w") or 0))[:3]]
                    if ratio is None:
                        why = re.sub(r"^n/a\s*-+\s*", "", p63.get("conc_note") or "not computable")
                else:
                    ratio, why, who = conc_ratio(mrow, c["inputs"], R, sig_w)
                rec["who"] = who
                if ratio is None:
                    rec["value"] = "n/a — " + (why or "not computable")
                else:
                    rec["m"] = _num(ratio, 3)
                    shape = ("narrower than the fund" if ratio > CONC_NARROW else
                             "broader than the fund" if ratio < CONC_BROAD else "in line with the fund")
                    rec["value"] = f"{ratio:.1f}× their weight share of the 63d move ({shape})"
            rec["evidence"] = {"tag": "uncalibrated", "note": "no holdings history: cannot be calibrated"}
        elif k == "split":
            rec["status"] = "info"
            rec["note"] = _t(c.get("why", ""))
            if mrow is None:
                rec["status"], rec["note"] = "n/a", no_members
            else:
                R, groups, missing = split_contrib(mrow, c["groups"])
                if R is None:
                    rec["status"], rec["value"] = "n/a", "n/a — row return unavailable"
                else:
                    bits = []
                    for g in groups:
                        if g["c"] is None:
                            bits.append(f"{g['name']} —")
                        else:
                            bits.append(f"{g['name']} {_signed(g['c'] * 100, '{:+.1f}')}pp")
                    rec["value"] = " · ".join(bits) + f" of the 63d {_pct(R)}"
                    rec["groups"] = [{"name": g["name"], "c": _num(g["c"], 5), "found": g["found"]}
                                     for g in groups]
                    if missing:
                        rec["note"] += " (some members have no 63d return; they sit in the rest)"
            rec["evidence"] = {"tag": "uncalibrated", "note": "no holdings history: cannot be calibrated"}
        elif k == "extension":
            rec["status"] = "info"
            # The value AND its percentile come from the same series: the row's
            # own adjusted closes (ext_frame), the basis the evidence file's 3y
            # quantiles were built on. crowd.json's ext_200d is on raw closes and
            # carries no baskets, so it is kept beside it for reference only.
            ext_c = (crow or {}).get("ext_200d")
            if ext_c is not None:
                rec["crowd_ext_200d"] = _num(ext_c, 4)
            ext_own, pct_own = ext_frame(row_close)
            ext_now = float(ext_own[-1]) if len(ext_own) else float("nan")
            if math.isnan(ext_now):
                rec["status"], rec["note"] = "n/a", "no 200d mean yet"
            else:
                rec["m"] = _num(ext_now, 4)
                pct = pct_from_quantiles(ext_now, ext_q)
                if pct is not None:
                    yrs = (ext_n or EXT_LB) / 252.0
                    basis = "of its 3y range" if yrs >= 2.95 else f"of its {yrs:.1f}y range"
                else:
                    pv = pct_own[-1] if len(pct_own) else float("nan")
                    if not math.isnan(pv):
                        pct = float(pv)
                        n_ok = int(np.sum(~np.isnan(ext_own[-EXT_LB:])))
                        basis = f"of its {n_ok / 252:.1f}y range in prices.db"
                v = f"{_pct(ext_now)} vs its 200d mean"
                if pct is not None:
                    rec["pct"] = _num(pct, 3)
                    rec["pct_basis"] = basis
                    v += f" · {_ordinal(round(pct * 100))} pct {basis}"
                    if pct >= EXT_HI:
                        rec["note"] = "stretched"
                    elif pct <= EXT_LO:
                        rec["note"] = "washed out"
                rec["value"] = v
            if trend_now in (1, -1):
                rec["evidence"] = evidence_cell(ev, fam, "extension", trend_now, TH.primary_of(t))
                # the cell compares the flagged zone (stretched in an up-trend,
                # washed out in a down-trend) with every other day of that trend;
                # say which side of it today sits on, so an 'edge' tag is never
                # read as applying to a row that is not in the zone
                p_ = rec.get("pct")
                if p_ is not None and rec["evidence"].get("tag") != "uncalibrated":
                    zone = "stretched" if trend_now > 0 else "washed out"
                    inz = p_ >= EXT_HI if trend_now > 0 else p_ <= EXT_LO
                    rec["evidence"]["today"] = zone if inz else f"not {zone}"
                    rec["evidence"]["med_today"] = rec["evidence"].get("med_conf" if inz else "med_div")
            else:
                rec["evidence"] = {"tag": "uncalibrated",
                                   "note": "extension is calibrated in up- and down-trends only"}
        out.append(rec)
    return out


def _check_phrase(t, rec, trend_now):
    c = next(x for x in TH.checks_for(t) if x["id"] == rec["id"])
    phrase = c["up_div"] if trend_now > 0 else c["dn_div"]
    sym = short_of(c) if c["kind"] in ("driver", "ratio", "curve", "level") else None
    val = f" ({sym} {rec['value']})" if sym and rec["value"] != "n/a" else (
        f" ({rec['value']})" if rec["value"] != "n/a" else "")
    return _t(phrase), _t(phrase) + val


def read_line(t, phase, checks, reason=None):
    """Spec s8: <phase> since <date> -- <k> of <n> checks confirm; <the most
    informative divergence>; <extension qualifier>. A state, never a forecast."""
    if phase is None:
        return f"Phase n/a — {reason or 'not enough history'}."
    trend = {"up": 1, "down": -1, "flat": 0}[phase["trend"]]
    head = f"{phase['word']} since {_md(phase['since'])}"
    dirs = [c for c in checks if c["status"] in DIRECTIONAL]
    n = len(dirs)
    parts = []
    if trend > 0:
        k = sum(c["status"] == "confirms" for c in dirs)
        parts.append(f"{head} — {k} of {n} checks confirm")
    elif trend < 0:
        k = sum(c["status"] == "confirms" for c in dirs)
        if phase["label"] == "turning_up":
            parts.append(f"{head} — the 126d down-trend intact, {k} of {n} checks still confirm it")
        else:
            parts.append(f"{head} — {k} of {n} checks confirm the down-trend")
    else:
        ku = sum(c["status"] == "leans_up" for c in dirs)
        kd = sum(c["status"] == "leans_down" for c in dirs)
        parts.append(f"{head} — no 126d trend; {ku} of {n} checks lean up, {kd} lean down")

    divs = [c for c in dirs if c["status"] == "diverges"]
    divs.sort(key=lambda c: (0 if (c.get("evidence") or {}).get("tag") == "edge" else 1,
                             -abs(c.get("z") or (0.5 if c["kind"] == "participation" else 0))))
    if divs:
        _, full = _check_phrase(t, divs[0], trend)
        txt = full
        if len(divs) > 1:
            p2, _ = _check_phrase(t, divs[1], trend)
            txt += f"; also {p2}"
        parts.append(txt)
    elif trend != 0:
        narrow = [c for c in checks if c["kind"] == "concentration" and c.get("m") is not None
                  and c["m"] > CONC_NARROW]
        if narrow:
            c0 = max(narrow, key=lambda c: c["m"])
            who = " + ".join(c0.get("who") or []) or "the top names"
            parts.append(f"nothing diverging, but leadership narrow: {who} made "
                         f"{c0['m']:.1f}× their weight share of the 63d move")
        else:
            parts.append("nothing diverging")
    else:
        leans = [c for c in dirs if c["status"] in ("leans_up", "leans_down") and c.get("z") is not None]
        if leans:
            c0 = max(leans, key=lambda c: abs(c["z"]))
            cfg = next(x for x in TH.checks_for(t) if x["id"] == c0["id"])
            sym = short_of(cfg) if cfg["kind"] in ("driver", "ratio", "curve", "level") else c0["label"]
            parts.append(f"strongest lean {'up' if c0['status'] == 'leans_up' else 'down'}: "
                         f"{sym} {c0['value']}")
    ext = next((c for c in checks if c["kind"] == "extension"), None)
    if ext is not None and ext.get("pct") is not None:
        p = ext["pct"]
        basis = ext.get("pct_basis") or "of its 3y range"
        if p >= EXT_HI:
            parts.append(f"stretched: {_ordinal(round(p * 100))} pct {basis}")
        elif p <= EXT_LO:
            parts.append(f"washed out: {_ordinal(round(p * 100))} pct {basis}")
    return "; ".join(parts) + "."


def phase_record(f, codes, dates, i):
    code = int(codes[i])
    if code < 0:
        return None
    j = run_start(codes, i)
    first_valid = int(np.argmax(codes >= 0))
    lab = LABEL_KEYS[code]
    return {"label": lab, "word": WORD[lab], "since": dates[j],
            "since_floor": bool(j == first_valid),
            "trend": TREND_WORD[int(f["trend"][i])],
            "t126": _num(f["t126"][i], 2), "slope": _num(f["slope"][i], 4),
            "p21": _num(f["p21"][i], 4), "p63": _num(f["p63"][i], 4), "p126": _num(f["p126"][i], 4),
            "sigma": _num(f["sigma"][i], 4), "a1": _num(f["a1"][i], 3), "a2": _num(f["a2"][i], 3),
            "above50": bool(f["above50"][i] > 0.5),
            "vs200": _num(f["vs200"][i], 4),
            "lower_high": bool(f["lower_high"][i] > 0.5),
            "higher_low": bool(f["higher_low"][i] > 0.5)}


def build(dates, series, fetched, members, crowd, ev, fetch_errors=None):
    """Assemble the s10 payload from an axis + panel (+ fetched drivers)."""
    inp = Inputs(dates, series, fetched)
    errors = {}
    if fetch_errors:
        errors["_fetch"] = {k: v for k, v in sorted(fetch_errors.items())}
    mrows = (members or {}).get("rows") or {}
    crows = (crowd or {}).get("tickers") or {}
    rows = {}
    for e in RG.REG_ETFS:
        t = e["t"]
        p = TH.primary_of(t)
        th = TH.THESES[p]
        fam = th["family"]
        close = inp.row(t)
        rec = {"primary": p, "family": fam, "bet": _t(th["bet"]),
               "phase": None, "checks": [], "read": "",
               "topping": _t(th["topping"]), "bottoming": _t(th["bottoming"]),
               "phase_evidence": None}
        if th.get("caveat"):
            rec["caveat"] = _t(th["caveat"])
        if p != t:
            rec["checks_from"] = p
        reason = None
        trend_now = None
        sig_ann = None
        if close is None or not np.any(~np.isnan(close)):
            reason = "no price series in prices.db"
            close = np.full(len(dates), np.nan)
        else:
            f = phase_frame(close)
            codes = labels(f)
            nbar = int(np.sum(~np.isnan(close)))
            if codes[-1] < 0:
                if np.isnan(close[-1]):
                    reason = "the series has stopped printing (stale)"
                elif nbar < MIN_PHASE_BARS:
                    reason = f"{nbar} sessions of history; the phase needs {MIN_PHASE_BARS}"
                else:
                    reason = "not computable on the last session"
            else:
                rec["phase"] = phase_record(f, codes, dates, len(dates) - 1)
                trend_now = int(f["trend"][-1])
            s = f["sigma"][-1]
            sig_ann = None if math.isnan(s) else float(s)
        ev_row = (((ev or {}).get("rows") or {}).get(t) or {})
        rec["checks"] = build_checks(t, trend_now, fam, inp, mrows.get(t), crows.get(t), ev,
                                     sig_ann, ev_row.get("ext_q"), close, errors,
                                     members_loaded=members is not None, ext_n=ev_row.get("ext_n"))
        rec["read"] = read_line(t, rec["phase"], rec["checks"], reason)
        if rec["phase"] is not None:
            rec["phase_evidence"] = phase_evidence(ev, t, fam, rec["phase"]["label"])
        else:
            rec["phase_reason"] = reason
        rows[t] = rec
    return rows, errors


def generated_strings(rows):
    """Every piece of generated / configured prose in the payload (for the
    banned-words test)."""
    out = []
    for t, r in rows.items():
        out += [r.get("read") or "", r.get("bet") or "", r.get("topping") or "",
                r.get("bottoming") or "", r.get("caveat") or "", r.get("phase_reason") or ""]
        if r.get("phase"):
            out.append(r["phase"].get("word") or "")
        for c in r.get("checks") or []:
            out += [c.get("label") or "", c.get("value") or "", c.get("note") or "",
                    c.get("pct_basis") or "", (c.get("evidence") or {}).get("note") or "",
                    (c.get("evidence") or {}).get("pool") or "",
                    (c.get("evidence") or {}).get("today") or ""]
            out += [g.get("name") or "" for g in c.get("groups") or []]
        pe = r.get("phase_evidence") or {}
        out.append(pe.get("pool") or "")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--no-fetch", action="store_true", help="skip the yfinance driver fetch")
    a = ap.parse_args(argv)

    conn = sqlite3.connect(a.db)
    dates, series = RG.build_panel(conn)
    conn.close()
    inp = Inputs(dates, series, {})
    need = set()
    for t in [e["t"] for e in RG.REG_ETFS]:
        for c in TH.checks_for(t):
            if c["kind"] in ("driver", "ratio", "curve", "level"):
                need.update(c["inputs"])
                if c.get("fx"):
                    need.add(c["fx"])
    to_fetch = inp.needs_fetch(need)
    fetched, ferr = ({}, {s: "fetch skipped (--no-fetch)" for s in to_fetch}) if a.no_fetch \
        else fetch_yf(to_fetch)

    ev = load_json(EVIDENCE_PATH)
    members = load_json(MEMBERS_PATH)
    crowd = load_json(CROWD_PATH)
    rows, errors = build(dates, series, fetched, members, crowd, ev, ferr)
    if members is None:
        errors["_members"] = "data/rotation_members.json not present: participation, concentration and split are n/a"
    if ev is None:
        errors["_evidence"] = "data/rotation_evidence.json not present: evidence tags are 'uncalibrated'"

    bad = [s for s in generated_strings(rows) if BANNED.search(s)]
    if bad:
        print("BANNED WORD in generated text:", bad[:5], file=sys.stderr)
        return 1

    out = {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "asof": dates[-1],
           "evidence_asof": (ev or {}).get("asof"),
           # edges vs cells tested: at 90% one-sided about 5% of the tested cells clear by
           # chance, so the board quotes both numbers next to any edge tag
           "evidence_summary": ({"cells_tested": ev.get("cells_tested"),
                                 "edges": len(ev.get("edges") or []),
                                 "chance_share": 0.05} if ev else None),
           "members_asof": (members or {}).get("asof"),
           "method": ("SPEC-86 s4-s8. Phase = the row's own 126d log-price OLS slope t-stat "
                      "(up >= +2, down <= -2), annualised 21/63/126d paces, acceleration in units "
                      "of its own 126d volatility (dead zone 0.25), the 50d mean and the 63d "
                      "high/low structure; a STATE, not a forecast. Checks: s = +1 means the "
                      "measure rising supports the up-trend; status is direction-aware; dead zone "
                      "= 0.5 x the measure's own window volatility (participation: a fixed "
                      "0.40-0.60 band). Extension = close / 200d mean - 1 on the row's own "
                      "adjusted closes, ranked in its trailing 3y (quantiles from "
                      "rotation_evidence.json; prices.db's shorter history when absent); "
                      "crowd.json's raw-close ext_200d rides along as crowd_ext_200d. "
                      "Evidence tags come from rotation_evidence.json (edge only "
                      "when both the episode-block and the calendar-block 90% intervals exclude 0 "
                      "in the expected direction with >= 20 episodes a side)."),
           "rows": rows,
           "errors": errors}
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1, allow_nan=False)
        fh.write("\n")
    os.replace(tmp, a.out)

    counts = {}
    for t, r in rows.items():
        if r["primary"] == t:
            k = r["phase"]["label"] if r["phase"] else "n/a"
            counts[k] = counts.get(k, 0) + 1
    print(f"rotation_phase: {len(rows)} rows as of {dates[-1]}; primaries by phase: "
          + ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])))
    if ferr:
        print(f"  driver fetch errors: {len(ferr)} ({', '.join(sorted(ferr)[:6])}...)")
    print(f"Wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
