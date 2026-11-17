#!/usr/bin/env python3
"""
Seller-exhaustion readings for three universes, written to data/exhaustion.json.

    python exhaustion.py              # prices.db only; no network
    python exhaustion.py --check      # validate an existing data/exhaustion.json

Owner, 2026-11-17: "how can we measure 'seller exhaustion' ie indicators of
market bottoms" -- then "work on the exhaustion piece too".

WHAT IS MEASURED, per universe and per session (settled daily closes only):

  capitulation  % of members above their 20/50/200-day averages; % at a 20-day
                and a 252-day closing low; the down-volume share (dollar volume
                of members that closed down / all member dollar volume) and how
                many of the last 10 sessions had a share >= 0.90; the median
                member's volume against its own prior 50-day average on a day
                the universe fell; the proxy ETF's close location in its day's
                range; the median member's distance from its 50-day average and
                its 14-day Wilder RSI.
  drying up     a new 60-day closing low in the equal-weight universe index made
                with FEWER members at 20-day lows than at the previous 60-day low.
  turn          a Zweig-style breadth thrust; an O'Neil-style follow-through day.

Every continuous reading carries an EXPANDING-window percentile (only data up
to that day), oriented so a HIGH percentile always means MORE exhausted. Each
capitulation reading has ONE extreme rule, fixed in RULES below before any
backtest was run. capitulation_count is how many are on; there is no blended
score. The backtest asks whether a cluster of those flags was followed by better
1/3/6-month returns than any day, and than simply buying a 10% drawdown.

DISPLAY ONLY. Nothing here is a signal the board acts on.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

import regime as RG

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
DB_PATH = os.path.join(HERE, "prices.db")
TICKERS_PATH = os.path.join(HERE, "tickers.txt")
OUT_PATH = os.path.join(DATA_DIR, "exhaustion.json")
SCHEMA = 1

# ==========================================================================
# CONSTANTS -- fixed IN ADVANCE (2026-11-17), before any backtest was run.
# Do not tune these against the backtest below; that is the hindsight trap the
# sector "top third" rotation study fell into (backtest_rotation.py).
# ==========================================================================
MIN_N = {"coverage": 15, "semis": 15, "market": 8}   # members with data, or the session is null
MIN_SHARE = 0.5          # ...and at least this share of today's live members (composition guard)
MIN_PCTILE_OBS = 252     # a percentile needs a year of its own observations first
HIST_SESSIONS = 252      # sessions of history emitted per universe
EMA_SPAN = 10            # breadth thrust: 10-session EMA of advancers / (advancers + decliners)
THRUST_LO, THRUST_HI, THRUST_WIN = 0.40, 0.615, 10
LOW_WIN = 60             # the "60-day low" behind the divergence and the follow-through day
DIV_GAP = 5              # sessions off the low between two lows for them to be separate lows
FTD_MIN, FTD_MAX, FTD_RET = 4, 12, 0.015
RECENT = 10              # turn_signals look back this many sessions
DECLUSTER = 20           # a new episode needs this many sessions without the condition
HORIZONS = (21, 63, 126)
DD_HORIZON = 63
NAIVE_DD = -0.10         # the naive alternative: proxy at least 10% under its 252-day high
COMBO_K = 3              # "capitulation then a turn": K chosen in advance, not the best K
COMBO_WIN = 20
MIN_EPISODES = 10        # fewer independent episodes than this: "too few to conclude"
Z_BAR = 2.0              # |mean - unconditional mean| / standard error needed to call a difference

# One rule per capitulation indicator. orient "low" = a LOW value is exhausted.
# rule ("abs", level): value <= level (low) / >= level (high); ("pct", p): the
# oriented expanding percentile >= p. Classic absolute levels are used where a
# published convention exists; elsewhere the reading's own 95th percentile.
RULES = [
    ("above_20d", "Share above their 20-day average", "low", ("abs", 0.15),
     "Classic short-term oversold breadth level: 15% or fewer above the 20-day."),
    ("above_50d", "Share above their 50-day average", "low", ("abs", 0.20),
     "Classic intermediate washout level: 20% or fewer above the 50-day."),
    ("above_200d", "Share above their 200-day average", "low", ("pct", 95),
     "No level is standard for a list this size and this growth-heavy; its own 95th percentile."),
    ("low_20d", "Share at a 20-day closing low", "high", ("abs", 0.40),
     "40% or more of members at a 20-day closing low is a broad liquidation day."),
    ("low_252d", "Share at a 252-day closing low", "high", ("pct", 95),
     "Yearly lows are rare and list-dependent; its own 95th percentile."),
    ("down_vol", "Down-volume share (dollar volume)", "high", ("abs", 0.90),
     "The 90% downside day (Lowry): nine-tenths of the money traded in names that fell."),
    ("down90_10d", "90% down days in the last 10 sessions", "high", ("abs", 2),
     "Two or more 90% days close together is the classic capitulation cluster."),
    ("vol_surge", "Median relative volume on a down day", "high", ("pct", 95),
     "Volume against each name's own 50-day average, only on days the universe fell; its own 95th percentile."),
    ("close_loc", "Proxy close location in the day's range", "low", ("abs_down", 0.10, -0.01),
     "Proxy down 1% or more AND closing in the bottom tenth of its range: selling into the close."),
    ("dist_50d", "Median distance from the 50-day average", "low", ("pct", 95),
     "How stretched below trend the typical name is; its own 95th percentile."),
    ("rsi14", "Median 14-day RSI (Wilder)", "low", ("abs", 30.0),
     "The conventional oversold line, applied to the median member."),
]
CAP_KEYS = [r[0] for r in RULES]
RULE = {r[0]: r for r in RULES}

# The ETF-set universe: ladder sector/industry rows only (GICS nodes), one per
# twin group, no baskets, no regions/broad/themes, and not SLV (a metal, not equities).
MARKET_EXCLUDE = {"SLV"}
# ETFs that sit inside single-stock sections of tickers.txt and are not ladder rows.
ETF_EXTRA = {"ARKX"}
ETF_SECTION = re.compile(r"\bETFs?\b|macro|benchmark", re.I)
SMH_PROXY = "SMH"


# ==========================================================================
# universes
# ==========================================================================

def _is_single_listing(t):
    """US-listed single security symbol: no index (^), future (=F), FX (=X),
    crypto (-USD) or foreign-exchange suffix (.KS/.T: other calendars, other currencies)."""
    return not (t.startswith("^") or "=" in t or t.endswith("-USD") or "." in t)


def universes_from(tickers_path=TICKERS_PATH):
    """{key: {label, members, proxy, rule}} from tickers.txt section headers,
    the way technicals.load_groups reads them."""
    import technicals as TE
    groups = TE.load_groups(tickers_path)
    etf = ({e["t"] for e in RG.REG_ETFS} | {e["t"] for e in RG.REG_STYLE}
           | {e["t"] for e in RG.REG_BONDS} | ETF_EXTRA)
    single = sorted(t for t, g in groups.items()
                    if _is_single_listing(t) and t not in etf and not ETF_SECTION.search(g["group"]))
    semis = sorted(t for t in single if groups[t]["proxy"] == SMH_PROXY)
    sem_sections = sorted({groups[t]["group"] for t in semis})
    seen, market = set(), []
    for e in RG.REG_ETFS:
        if e.get("basket") or e.get("sector") in ("Broad", "Regions", "Themes") or e["t"] in MARKET_EXCLUDE:
            continue
        g = e.get("grp")
        if g and g in seen:
            continue
        if g:
            seen.add(g)
        market.append(e["t"])
    return {
        "coverage": {"label": "Coverage single stocks", "members": single, "proxy": "SPY",
                     "rule": "Every US-listed single stock in tickers.txt: no ETFs, indices, futures, FX, "
                             "crypto or foreign-exchange listings. Proxy SPY."},
        "semis": {"label": "Semis and AI hardware", "members": semis, "proxy": SMH_PROXY,
                  "rule": "The single stocks technicals.py proxies to SMH, i.e. the sections: "
                          + "; ".join(sem_sections) + ". Proxy SMH."},
        "market": {"label": "Sector and industry ETFs", "members": market, "proxy": "SPY",
                   "rule": "The rotation ladder's GICS sector and industry ETF rows, one per twin group, "
                           "no baskets, no broad/region/theme rows, not SLV. A small N: each member "
                           "moves a share reading by several points, and members overlap heavily."},
    }


# ==========================================================================
# loading
# ==========================================================================

def load_bars(conn, tickers, axis):
    """{ticker: {"c","rc","h","l","v"}} as lists on the axis. NO forward fill:
    a session with no bar is None and the name sits that session out."""
    pos = {d: i for i, d in enumerate(axis)}
    n = len(axis)
    out = {}
    for t in tickers:
        rows = conn.execute(
            "SELECT date, high, low, close, adj_close, volume FROM daily_prices "
            "WHERE ticker=? AND close IS NOT NULL ORDER BY date", (t,)).fetchall()
        if not rows:
            continue
        b = {k: [None] * n for k in ("c", "rc", "h", "l", "v")}
        for d, h, l, c, ac, v in rows:
            i = pos.get(d)
            if i is None:
                continue
            b["c"][i] = ac if ac is not None else c
            b["rc"][i] = c
            b["h"][i] = h
            b["l"][i] = l
            b["v"][i] = v
        out[t] = b
    return out


# ==========================================================================
# per-member features (computed on the name's OWN bars, scattered to the axis)
# ==========================================================================

def wilder_rsi(closes, n=14):
    """Wilder RSI on a dense list of closes: the first average is the simple mean
    of the first n changes, then avg = (avg*(n-1) + x)/n. None until n changes."""
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    g = l = 0.0
    for k in range(1, n + 1):
        d = closes[k] - closes[k - 1]
        g += max(d, 0.0)
        l += max(-d, 0.0)
    ag, al = g / n, l / n

    def val(ag, al):
        if al == 0:
            return 100.0 if ag > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + ag / al)
    out[n] = val(ag, al)
    for k in range(n + 1, len(closes)):
        d = closes[k] - closes[k - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
        out[k] = val(ag, al)
    return out


def member_features(b, n):
    """Per-session features of one member on the axis (None where undefined)."""
    idx = [i for i in range(n) if b["c"][i] is not None and b["c"][i] > 0]
    c = [b["c"][i] for i in idx]
    f = {k: [None] * n for k in ("ret", "a20", "a50", "a200", "l20", "l252", "dv", "rv", "d50", "rsi")}
    rsi = wilder_rsi(c, 14)
    pre = [0.0]
    for x in c:
        pre.append(pre[-1] + x)
    vols = [b["v"][i] or 0 for i in idx]
    vpre = [0.0]
    for x in vols:
        vpre.append(vpre[-1] + x)
    for k, i in enumerate(idx):
        x = c[k]
        if k >= 1:
            f["ret"][i] = x / c[k - 1] - 1.0
        for w, key in ((20, "a20"), (50, "a50"), (200, "a200")):
            if k + 1 >= w:
                sma = (pre[k + 1] - pre[k + 1 - w]) / w
                f[key][i] = 1 if x > sma else 0
                if w == 50:
                    f["d50"][i] = x / sma - 1.0
        if k + 1 >= 20:
            f["l20"][i] = 1 if x <= min(c[k - 19:k + 1]) else 0
        if k + 1 >= 252:
            f["l252"][i] = 1 if x <= min(c[k - 251:k + 1]) else 0
        rc, v = b["rc"][i], b["v"][i]
        if rc is not None and v is not None and v > 0:
            f["dv"][i] = rc * v
        if k >= 50 and v is not None:
            avg = (vpre[k] - vpre[k - 50]) / 50.0
            if avg > 0:
                f["rv"][i] = v / avg
        f["rsi"][i] = rsi[k]
    return f


def _median(a):
    b = sorted(a)
    if not b:
        return None
    m = len(b) // 2
    return b[m] if len(b) % 2 else (b[m - 1] + b[m]) / 2.0


# ==========================================================================
# universe aggregation
# ==========================================================================

def ema_reset(arr, span):
    """EMA seeded on the first value after any None; None stays None (a gap
    restarts it rather than bridging over a stretch with no reading)."""
    a = 2.0 / (span + 1.0)
    out, prev = [None] * len(arr), None
    for i, v in enumerate(arr):
        if v is None:
            prev = None
            continue
        prev = v if prev is None else a * v + (1 - a) * prev
        out[i] = prev
    return out


def is_window_low(arr, i, w):
    """arr[i] is the lowest of the last w values (inclusive); all must exist."""
    if i < w - 1:
        return False
    win = arr[i - w + 1:i + 1]
    if any(v is None for v in win):
        return False
    return arr[i] <= min(win)


def divergence_series(idx, low20, w=LOW_WIN, gap=DIV_GAP):
    """Per session: (fired, today's share at 20-day lows, the share at the prior
    60-day low, that low's index). Fires when the index makes a w-session closing
    low, an earlier w-session low exists within w sessions that is separated from
    today's run of lows by at least `gap` sessions that were not lows, and the
    share at 20-day lows is now BELOW its reading on that earlier low's last day
    (the lowest close of that earlier episode)."""
    n = len(idx)
    is_low = [is_window_low(idx, i, w) for i in range(n)]
    out = [None] * n
    for i in range(n):
        if idx[i] is None:
            continue
        if not is_low[i]:
            out[i] = (False, low20[i], None, None)
            continue
        k = i - 1
        while k >= 0 and k >= i - w and is_low[k]:      # today's own run of lows
            k -= 1
        off = 0
        while k >= 0 and k >= i - w and not is_low[k]:  # the bounce in between
            off += 1
            k -= 1
        j = k if (k >= 0 and k >= i - w and is_low[k] and off >= gap) else None
        if j is None or low20[i] is None or low20[j] is None:
            out[i] = (False, low20[i], None if j is None else low20[j], j)
        else:
            out[i] = (low20[i] < low20[j], low20[i], low20[j], j)
    return out


def thrust_series(ema, lo=THRUST_LO, hi=THRUST_HI, win=THRUST_WIN):
    """Fires on the session the EMA crosses above `hi` when it was below `lo`
    at some point in the previous `win` sessions."""
    out = [False] * len(ema)
    for i in range(1, len(ema)):
        e, p = ema[i], ema[i - 1]
        if e is None or p is None or not (e > hi >= p):
            continue
        out[i] = any(x is not None and x < lo for x in ema[max(0, i - win):i])
    return out


def ftd_series(c, v, w=LOW_WIN, dmin=FTD_MIN, dmax=FTD_MAX, r=FTD_RET):
    """(fired, sessions since the low) per session: dmin..dmax sessions after the
    most recent w-session closing low (a lower close would itself be a newer low,
    so the rally attempt is unbroken), the proxy up >= r on volume above the
    prior session's."""
    n = len(c)
    out = [(False, None)] * n
    last_low = None
    for i in range(n):
        if is_window_low(c, i, w):
            last_low = i
        if last_low is None or i == 0 or c[i] is None or c[i - 1] is None:
            continue
        k = i - last_low
        fired = (dmin <= k <= dmax and c[i - 1] > 0 and c[i] / c[i - 1] - 1.0 >= r
                 and v[i] is not None and v[i - 1] is not None and v[i] > v[i - 1])
        out[i] = (bool(fired), k)
    return out


def expanding_pct(vals, orient, min_obs=MIN_PCTILE_OBS):
    """(percentile 0-100, obs) per session against every earlier-or-same value
    of the series (expanding; never a later one). Mid-rank for ties. Oriented:
    for 'high' the share of history BELOW the value, for 'low' the share ABOVE,
    so a high percentile always means more exhausted. None under min_obs."""
    s, out = [], [None] * len(vals)
    for i, v in enumerate(vals):
        if v is None:
            continue
        bisect.insort(s, v)
        m = len(s)
        lt = bisect.bisect_left(s, v)
        le = bisect.bisect_right(s, v)
        if orient == "high":
            p = (lt + 0.5 * (le - lt)) / m
        else:
            p = ((m - le) + 0.5 * (le - lt)) / m
        out[i] = (100.0 * p if m >= min_obs else None, m)
    return out


def flag_of(key, val, pct, proxy_ret=None):
    """The fixed extreme rule. None when the rule cannot be evaluated."""
    _, _, orient, rule, _ = RULE[key]
    if rule[0] == "pct":
        return None if pct is None else pct >= rule[1]
    if val is None:
        return None
    if rule[0] == "abs_down":
        if proxy_ret is None:
            return None
        return val <= rule[1] and proxy_ret <= rule[2]
    return val <= rule[1] if orient == "low" else val >= rule[1]


def compute_universe(axis, bars, members, proxy, min_n, min_share=MIN_SHARE):
    """Every per-session series of one universe. `bars` maps ticker -> bars on
    the axis; `members` is the member list; `proxy` the proxy ticker."""
    n = len(axis)
    feats = [member_features(bars[t], n) for t in members if t in bars]
    pb = bars.get(proxy)
    live = [sum(1 for f in feats if f["ret"][i] is not None) for i in range(n)]
    # The share floor is of the MEMBER LIST (a fixed input), not of today's live
    # count, so appending later sessions can never change an earlier one.
    need = max(min_n, int(math.ceil(min_share * len(feats))))
    ok = [x >= need for x in live]

    def frac(key):
        out = [None] * n
        for i in range(n):
            if not ok[i]:
                continue
            xs = [f[key][i] for f in feats if f[key][i] is not None]
            if len(xs) >= min_n:
                out[i] = sum(xs) / len(xs)
        return out

    def med(key, cond=None):
        out = [None] * n
        for i in range(n):
            if not ok[i] or (cond is not None and not cond[i]):
                continue
            xs = [f[key][i] for f in feats if f[key][i] is not None]
            if len(xs) >= min_n:
                out[i] = _median(xs)
        return out

    S = {"live": live, "need": need}
    for key, fk in (("above_20d", "a20"), ("above_50d", "a50"), ("above_200d", "a200"),
                    ("low_20d", "l20"), ("low_252d", "l252")):
        S[key] = frac(fk)

    ew_ret, adr, dvs = [None] * n, [None] * n, [None] * n
    for i in range(n):
        if not ok[i]:
            continue
        rs = [f["ret"][i] for f in feats if f["ret"][i] is not None]
        ew_ret[i] = sum(rs) / len(rs)
        up = sum(1 for x in rs if x > 0)
        dn = sum(1 for x in rs if x < 0)
        adr[i] = up / (up + dn) if up + dn else None
        tot = down = 0.0
        for f in feats:
            r, d = f["ret"][i], f["dv"][i]
            if r is None or d is None:
                continue
            tot += d
            if r < 0:
                down += d
        dvs[i] = down / tot if tot > 0 else None
    S["ew_ret"], S["adr"], S["down_vol"] = ew_ret, adr, dvs
    S["down90_10d"] = [None if dvs[i] is None else
                       sum(1 for x in dvs[max(0, i - 9):i + 1] if x is not None and x >= 0.90)
                       for i in range(n)]
    S["vol_surge"] = med("rv", [r is not None and r < 0 for r in ew_ret])
    S["dist_50d"] = med("d50")
    S["rsi14"] = med("rsi")

    # equal-weight index (base 100 on the first live session; None where not live)
    ew, lvl = [None] * n, None
    for i in range(n):
        if ew_ret[i] is None:
            lvl = None
            continue
        lvl = 100.0 if lvl is None else lvl * (1.0 + ew_ret[i])
        ew[i] = lvl
    S["ew"] = ew

    # proxy
    pc = pb["c"] if pb else [None] * n
    pret = [None] * n
    for i in range(1, n):
        if pc[i] is not None and pc[i - 1]:
            pret[i] = pc[i] / pc[i - 1] - 1.0
    cl = [None] * n
    if pb:
        for i in range(n):
            if not ok[i]:      # a proxy reading counts only on the universe's own live sessions
                continue
            h, l, c = pb["h"][i], pb["l"][i], pb["rc"][i]
            if h is not None and l is not None and c is not None and h > l:
                cl[i] = (c - l) / (h - l)
    S["proxy_c"], S["proxy_ret"], S["close_loc"] = pc, pret, cl

    # turn signals
    S["ad_ema"] = ema_reset(adr, EMA_SPAN)
    S["thrust"] = thrust_series(S["ad_ema"])
    S["div"] = divergence_series(ew, S["low_20d"])
    S["ftd"] = ftd_series(pc, pb["v"] if pb else [None] * n)

    # percentiles + flags + count
    S["pct"], S["flag"] = {}, {}
    for key in CAP_KEYS:
        orient = RULE[key][2]
        pcs = expanding_pct(S[key], orient)
        S["pct"][key] = pcs
        S["flag"][key] = [flag_of(key, S[key][i], None if pcs[i] is None else pcs[i][0],
                                  pret[i] if key == "close_loc" else None) for i in range(n)]
    S["cap"] = [sum(1 for k in CAP_KEYS if S["flag"][k][i]) for i in range(n)]
    S["cap_eval"] = [sum(1 for k in CAP_KEYS if S["flag"][k][i] is not None) for i in range(n)]
    return S


# ==========================================================================
# backtest
# ==========================================================================

def decluster(cond, gap=DECLUSTER, lo=0, hi=None):
    """First session of each episode: cond True, and no True in the previous
    `gap` sessions. Only sessions lo..hi start episodes; the quiet-gap test
    looks back before lo too."""
    hi = len(cond) - 1 if hi is None else hi
    out, last = [], None
    for i in range(len(cond)):
        if cond[i]:
            if (last is None or i - last > gap) and lo <= i <= hi:
                out.append(i)
            last = i
    return out


def fwd(c, i, h):
    j = i + h
    if j >= len(c) or c[i] is None or c[j] is None or c[i] <= 0:
        return None
    return c[j] / c[i] - 1.0


def max_dd(c, i, h):
    j = i + h
    if j >= len(c) or c[i] is None or c[i] <= 0:
        return None
    win = [x for x in c[i + 1:j + 1] if x is not None]
    if not win:
        return None
    return min(0.0, min(win) / c[i] - 1.0)


def _pctile(sorted_a, q):
    if not sorted_a:
        return None
    k = (len(sorted_a) - 1) * q
    f = int(math.floor(k))
    cc = min(f + 1, len(sorted_a) - 1)
    return sorted_a[f] + (sorted_a[cc] - sorted_a[f]) * (k - f)


def stats(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return {"n": 0, "mean": None, "median": None, "hit": None, "p10": None, "sd": None}
    s = sorted(xs)
    m = sum(s) / len(s)
    sd = math.sqrt(sum((x - m) ** 2 for x in s) / (len(s) - 1)) if len(s) > 1 else None
    return {"n": len(s), "mean": m, "median": _median(s), "hit": sum(1 for x in s if x > 0) / len(s),
            "p10": _pctile(s, 0.10), "sd": sd}


def verdict(ev, base, min_n=MIN_EPISODES):
    """Plain verdict of an event cell against a baseline cell, with the z of the
    mean difference on the events' own standard error."""
    if ev["n"] < min_n:
        return "too few to conclude", None
    if ev["sd"] is None or ev["sd"] == 0 or base["mean"] is None:
        return "no measurable difference", None
    z = (ev["mean"] - base["mean"]) / (ev["sd"] / math.sqrt(ev["n"]))
    if z >= Z_BAR:
        return "above", z
    if z <= -Z_BAR:
        return "below", z
    return "no measurable difference", z


def event_cells(S, axis, starts, uncond, naive=None):
    """Outcome table for a list of episode starts."""
    out = {"episodes": len(starts), "dates": [axis[i] for i in starts]}
    for series_key, name in (("proxy_c", "proxy"), ("ew", "ew")):
        c = S[series_key]
        cell = {}
        for h in HORIZONS:
            st = stats([fwd(c, i, h) for i in starts])
            v, z = verdict(st, uncond[name]["h%d" % h])
            row = {"n": st["n"], "mean": st["mean"], "median": st["median"], "hit": st["hit"],
                   "p10": st["p10"], "verdict": v, "z": z}
            if naive is not None:
                nv = naive[name]["h%d" % h]
                if st["n"] < MIN_EPISODES or nv["n"] < MIN_EPISODES:
                    row["vs_naive"] = "too few to conclude"
                elif st["sd"] and nv["sd"]:
                    se = math.sqrt(st["sd"] ** 2 / st["n"] + nv["sd"] ** 2 / nv["n"])
                    z2 = (st["mean"] - nv["mean"]) / se
                    row["vs_naive"] = "above" if z2 >= Z_BAR else ("below" if z2 <= -Z_BAR else "no measurable difference")
                    row["z_naive"] = z2
                else:
                    row["vs_naive"] = "no measurable difference"
            cell["h%d" % h] = row
        dd = stats([max_dd(c, i, DD_HORIZON) for i in starts])
        cell["maxdd%d" % DD_HORIZON] = {"n": dd["n"], "mean": dd["mean"], "median": dd["median"], "p10": dd["p10"]}
        out[name] = cell
    return out


def _raw_cells(S, starts):
    """Un-verdicted stats (for the naive baseline the others are compared with)."""
    out = {}
    for series_key, name in (("proxy_c", "proxy"), ("ew", "ew")):
        out[name] = {"h%d" % h: stats([fwd(S[series_key], i, h) for i in starts]) for h in HORIZONS}
    return out


def backtest_universe(S, axis):
    n = len(axis)
    # the period: from the first session every capitulation rule can be evaluated
    start = next((i for i in range(n) if S["cap_eval"][i] == len(CAP_KEYS)), None)
    if start is None:
        return {"start": None, "end": None, "note": "no session where every rule can be evaluated yet"}
    end = n - 1
    rng = range(start, end + 1)
    uncond = {}
    ucell = {}
    for series_key, name in (("proxy_c", "proxy"), ("ew", "ew")):
        c = S[series_key]
        uncond[name], ucell[name] = {}, {}
        for h in HORIZONS:
            st = stats([fwd(c, i, h) for i in rng])
            uncond[name]["h%d" % h] = st
            ucell[name]["h%d" % h] = {"n": st["n"], "n_eff": int(math.ceil(st["n"] / h)) if st["n"] else 0,
                                      "mean": st["mean"], "median": st["median"], "hit": st["hit"], "p10": st["p10"]}
        dd = stats([max_dd(c, i, DD_HORIZON) for i in rng])
        ucell[name]["maxdd%d" % DD_HORIZON] = {"n": dd["n"], "mean": dd["mean"], "median": dd["median"], "p10": dd["p10"]}

    pc = S["proxy_c"]
    dd252 = [None] * n
    for i in range(251, n):
        w = pc[i - 251:i + 1]
        if pc[i] is not None and all(x is not None for x in w):
            dd252[i] = pc[i] / max(w) - 1.0
    naive_starts = decluster([x is not None and x <= NAIVE_DD for x in dd252], lo=start, hi=end)
    naive_raw = _raw_cells(S, naive_starts)
    naive = event_cells(S, axis, naive_starts, uncond)

    caps = []
    for K in range(2, len(CAP_KEYS) + 1):
        st = decluster([x >= K for x in S["cap"]], lo=start, hi=end)
        cell = event_cells(S, axis, st, uncond, naive_raw)
        cell["k"] = K
        caps.append(cell)

    thr = [bool(x) for x in S["thrust"]]
    ftd = [x[0] for x in S["ftd"]]
    div = [bool(x and x[0]) for x in S["div"]]
    anyturn = [a or b or c for a, b, c in zip(thr, ftd, div)]
    cap_starts = decluster([x >= COMBO_K for x in S["cap"]], lo=start, hi=end)
    combo_cond = [False] * n
    for s in cap_starts:
        for t in range(s, min(n, s + COMBO_WIN + 1)):
            if anyturn[t]:
                combo_cond[t] = True
                break
    turn = {
        "thrust": event_cells(S, axis, decluster(thr, lo=start, hi=end), uncond, naive_raw),
        "ftd": event_cells(S, axis, decluster(ftd, lo=start, hi=end), uncond, naive_raw),
        "divergence": event_cells(S, axis, decluster(div, lo=start, hi=end), uncond, naive_raw),
        "cap%d_then_turn" % COMBO_K: event_cells(S, axis, decluster(combo_cond, lo=start, hi=end), uncond, naive_raw),
    }
    return {"start": axis[start], "end": axis[end], "sessions": end - start + 1,
            "unconditional": ucell, "naive_dd10": naive, "capitulation": caps, "turn": turn}


def backtest_read(bt):
    """Two or three plain sentences, written from the numbers, whatever they say."""
    parts = []
    for key in ("coverage", "semis", "market"):
        u = bt["universes"].get(key) or {}
        if not u.get("start"):
            parts.append("%s: no testable period yet." % key)
            continue
        cells = []
        for c in u["capitulation"]:
            r = c["proxy"]["h63"]
            if c["episodes"] == 0:
                continue
            cells.append("K%d %d ep, 63d %s/%s" % (c["k"], c["episodes"], r["verdict"], r.get("vs_naive", "-")))
        nv = u["naive_dd10"]["proxy"]["h63"]
        parts.append("%s (%s to %s): %s; the 10%%-drawdown rule %d ep, 63d %s vs any day."
                     % (key, u["start"], u["end"], "; ".join(cells) or "no capitulation episodes",
                        u["naive_dd10"]["episodes"], nv["verdict"]))
    return parts


# ==========================================================================
# payload
# ==========================================================================

def _num(x, d=4):
    if x is None:
        return None
    if isinstance(x, bool):
        return x
    x = float(x)
    if not math.isfinite(x):
        return None
    return round(x, d)


def _round_tree(o, d=4):
    if isinstance(o, dict):
        return {k: _round_tree(v, d) for k, v in o.items()}
    if isinstance(o, list):
        return [_round_tree(v, d) for v in o]
    if isinstance(o, float):
        return _num(o, d)
    return o


def universe_payload(key, spec, S, axis):
    n = len(axis)
    i = n - 1
    today = {}
    for k in CAP_KEYS:
        p = S["pct"][k][i]
        first = next((j for j in range(n) if S["pct"][k][j] is not None), None)
        today[k] = {"value": S[k][i], "pctile": None if p is None else p[0],
                    "pctile_obs": None if p is None else p[1],
                    "pctile_since": None if first is None else axis[first],
                    "extreme": S["flag"][k][i]}
    dv = S["div"][i]
    today["divergence"] = {"fired": bool(dv and dv[0]), "low_20d_now": dv[1] if dv else None,
                           "low_20d_prior_low": dv[2] if dv else None,
                           "prior_low_date": axis[dv[3]] if dv and dv[3] is not None else None}
    today["thrust"] = {"fired": bool(S["thrust"][i]), "ema": S["ad_ema"][i], "adv_ratio": S["adr"][i]}
    fi = S["ftd"][i]
    today["ftd"] = {"fired": bool(fi[0]), "sessions_since_low": fi[1]}

    def last_fire(arr):
        js = [j for j in range(n) if arr[j]]
        return axis[js[-1]] if js else None
    lo = max(0, n - RECENT)
    turn = []
    for name, arr in (("divergence", [bool(x and x[0]) for x in S["div"]]),
                      ("thrust", [bool(x) for x in S["thrust"]]),
                      ("ftd", [x[0] for x in S["ftd"]])):
        today[name]["last_fired"] = last_fire(arr)
        js = [j for j in range(lo, n) if arr[j]]
        if js:
            turn.append({"signal": name, "date": axis[js[-1]]})

    hist = []
    for j in range(max(0, n - HIST_SESSIONS), n):
        hist.append({"d": axis[j], "above_20d": S["above_20d"][j], "above_50d": S["above_50d"][j],
                     "above_200d": S["above_200d"][j], "low_20d": S["low_20d"][j],
                     "low_252d": S["low_252d"][j], "down_vol": S["down_vol"][j],
                     "dist_50d": S["dist_50d"][j], "rsi14": S["rsi14"][j], "ad_ema": S["ad_ema"][j],
                     "cap": S["cap"][j], "px": S["proxy_c"][j], "ew": S["ew"][j]})
    first_live = next((j for j in range(n) if S["ew_ret"][j] is not None), None)
    return {
        "label": spec["label"], "rule": spec["rule"], "proxy": spec["proxy"],
        "n": len([t for t in spec["members"]]), "n_live": S["live"][i], "min_live": S["need"],
        "first_session": None if first_live is None else axis[first_live],
        "members": spec["members"] if key == "market" else None,
        "today": today,
        "capitulation_count": S["cap"][i],
        "capitulation_of": S["cap_eval"][i],
        "capitulation_on": [k for k in CAP_KEYS if S["flag"][k][i]],
        "turn_signals": turn,
        "history": hist,
    }


METHOD = [
    "Settled daily closes only (adjusted close for returns and averages; the day's high, low and close as "
    "printed for the proxy's close location; dollar volume = close x volume). Intraday is out of scope.",
    "Each member's averages, lows, relative volume and RSI are computed on its own bars; a session with no "
    "bar for a name is skipped for that name, never filled.",
    "A universe's session is null when fewer than the minimum members have a bar (15 for the stock "
    "universes, 8 for the ETF set) or fewer than half of today's live members do, so the readings never "
    "silently change meaning as the list grows.",
    "Percentiles are expanding-window: each session is ranked only against the same reading on that session "
    "and earlier ones (mid-rank for ties), oriented so a HIGH percentile always means MORE exhausted. None "
    "until a reading has 252 observations; pctile_obs and pctile_since give the window behind today's figure.",
    "Extreme rules are fixed in advance in the thresholds block and were not tuned on the backtest.",
    "capitulation_count is how many of the capitulation rules are on; capitulation_of is how many could be "
    "evaluated. turn_signals lists which of divergence, breadth thrust and follow-through day fired in the "
    "last 10 sessions. There is no blended score.",
    "The ETF set is small: each member moves a share reading by about two points, and its history only "
    "starts when the sector ETFs' price history does.",
    "The latest session's volume is the figure the nightly fetch sees shortly after the close; the provider "
    "often revises it the next day, so today's relative-volume and follow-through readings can move on rerun.",
    "Display only. Nothing here is a forecast or an instruction.",
]

BT_NOTES = [
    "Episodes: the first session the condition is on after 20 sessions without it. Outcomes: the forward "
    "21/63/126-session return of the proxy and of the equal-weight universe index, and the worst further "
    "drop within 63 sessions.",
    "Baselines: every session of the same period (unconditional; n_eff = n / horizon is the rough count of "
    "independent windows) and the naive rule 'proxy 10% or more under its 252-day high', de-clustered the "
    "same way.",
    "verdict compares the episodes' mean with the baseline mean on the episodes' own standard error: 'above' "
    "or 'below' needs |z| >= 2; under 10 episodes it says 'too few to conclude'. Episodes 20 sessions apart "
    "still share most of a 63- or 126-session window, so even these z values overstate independence.",
    "Look-ahead: flags use expanding percentiles and fixed thresholds only; the test suite proves a flag on "
    "a date does not change when later data is appended.",
    "Survivorship: the universes are today's ticker list, so names that were delisted, acquired or dropped "
    "are missing and names were added partly because they did well. Past breadth therefore reads healthier "
    "than the real universe was, and the equal-weight index's forward returns are biased upward for "
    "episodes and baseline alike; the comparison with the baseline is less biased than either level.",
    "History depth: prices.db starts 2016-08 for most long-lived names; the test period begins once every "
    "rule has a year of its own observations (the relative-volume rule only counts down days, so about "
    "2019-04), which leaves roughly seven and a half years and a handful of real drawdowns (2020, 2022, "
    "2025). The sector ETFs start 2024-09, so the ETF set has no testable period yet.",
]


def build(axis, bars, specs):
    unis, bts = {}, {}
    for key, spec in specs.items():
        S = compute_universe(axis, bars, spec["members"], spec["proxy"], MIN_N.get(key, 15))
        unis[key] = universe_payload(key, spec, S, axis)
        bts[key] = backtest_universe(S, axis)
    bt = {"horizons": list(HORIZONS), "decluster": DECLUSTER, "min_episodes": MIN_EPISODES,
          "z_bar": Z_BAR, "combo_k": COMBO_K, "combo_window": COMBO_WIN,
          "notes": BT_NOTES, "universes": bts}
    bt["summary"] = backtest_read(bt)
    thresholds = {k: {"label": lab, "orient": o, "rule": list(rule), "why": why}
                  for k, lab, o, rule, why in RULES}
    thresholds["_turn"] = {
        "divergence": "Equal-weight index at a 60-session closing low, an earlier 60-session low within 60 "
                      "sessions separated by at least 5 sessions off the low, and the share at 20-day lows "
                      "now below its reading on that earlier low.",
        "thrust": "10-session EMA of advancers/(advancers+decliners) crosses above 0.615 having been below "
                  "0.40 within the previous 10 sessions (Zweig's levels).",
        "ftd": "4 to 12 sessions after the proxy's most recent 60-session closing low, the proxy up 1.5% or "
               "more on volume above the prior session's.",
    }
    return {"schema": SCHEMA, "asof": axis[-1] if axis else None,
            "method": METHOD, "thresholds": thresholds, "universes": unis, "backtest": bt}


def finish(payload, read_text):
    payload["backtest"]["read"] = read_text
    return _round_tree(payload)


def auto_read(payload):
    """The plain conclusion, generated from the cells so a rerun cannot leave
    stale prose behind. Three sentences."""
    bt = payload["backtest"]["universes"]
    stock = [k for k in ("coverage", "semis") if bt.get(k, {}).get("start")]
    v = {"above": 0, "below": 0, "no measurable difference": 0, "too few to conclude": 0}
    vn = {"above": 0, "below": 0, "no measurable difference": 0, "too few to conclude": 0}
    lean = tested = 0
    for k in stock:
        u = bt[k]
        cells = list(u["capitulation"]) + list(u["turn"].values())
        for c in cells:
            for nm in ("proxy", "ew"):
                base = u["unconditional"][nm]
                for h in HORIZONS:
                    r = c[nm]["h%d" % h]
                    v[r["verdict"]] += 1
                    vn[r.get("vs_naive") or "too few to conclude"] += 1
                    if r["verdict"] != "too few to conclude":
                        tested += 1
                        lean += (r["mean"] or 0) > (base["h%d" % h]["mean"] or 0)
    s1 = ("In the two stock universes, %d capitulation and turn-signal cells (rule x horizon x proxy or "
          "equal-weight index) had 10 or more independent episodes: %d beat the any-day forward return by two "
          "standard errors, %d trailed it and %d showed no measurable difference (the average was higher in %d "
          "of them, which is a lean, not evidence); %d more cells had too few episodes to conclude anything."
          % (tested, v["above"], v["below"], v["no measurable difference"], lean, v["too few to conclude"]))
    testable_n = vn["above"] + vn["below"] + vn["no measurable difference"]
    if vn["above"] == 0:
        s2 = ("Against simply buying any 10%% drawdown of the proxy there is no measurable edge: %d testable "
              "cells, none better (%d no difference, %d worse)." % (testable_n, vn["no measurable difference"], vn["below"]))
    else:
        s2 = ("Against simply buying any 10%% drawdown of the proxy, %d of %d testable cells were better, %d worse "
              "and %d no different." % (vn["above"], testable_n, vn["below"], vn["no measurable difference"]))
    s3 = ("The ETF set has history only from 2024-09 and cannot be tested yet; read today's counts as a "
          "description of how washed-out the tape is, not as a timing signal.")
    return " ".join([s1, s2, s3])


# ==========================================================================
# CLI
# ==========================================================================

GUARD = re.compile(r"(held|holdings?|cost basis|basis|custodian|broker|account|sleeve weight|book value|realized)", re.I)


def check(path=OUT_PATH, max_bytes=400 * 1024):
    """Problems with an existing payload, as a list of strings (empty = fine)."""
    probs = []
    try:
        size = os.path.getsize(path)
        with open(path, encoding="utf-8") as f:
            txt = f.read()
        j = json.loads(txt)
    except (OSError, ValueError) as e:
        return ["unreadable: %s" % type(e).__name__]
    if size > max_bytes:
        probs.append("size %d bytes over %d" % (size, max_bytes))
    for k in ("schema", "asof", "generated_at", "method", "thresholds", "universes", "backtest"):
        if k not in j:
            probs.append("missing key %s" % k)
    if j.get("schema") != SCHEMA:
        probs.append("schema %r" % j.get("schema"))
    for key in ("coverage", "semis", "market"):
        u = (j.get("universes") or {}).get(key)
        if not u:
            probs.append("missing universe %s" % key)
            continue
        for k in ("label", "n", "proxy", "today", "capitulation_count", "capitulation_on", "turn_signals", "history"):
            if k not in u:
                probs.append("%s missing %s" % (key, k))
        for k in CAP_KEYS:
            if k not in (u.get("today") or {}):
                probs.append("%s.today missing %s" % (key, k))
        if len(u.get("history") or []) > HIST_SESSIONS:
            probs.append("%s history too long" % key)
    if not (j.get("backtest") or {}).get("read"):
        probs.append("backtest.read empty")
    if "NaN" in txt or "Infinity" in txt:
        probs.append("non-finite number")
    m = GUARD.search(txt)
    if m:
        probs.append("guard word %r in a public payload" % m.group(0))
    return probs


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--tickers", default=TICKERS_PATH)
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--check", action="store_true", help="validate an existing payload and exit")
    a = ap.parse_args(argv)
    if a.check:
        probs = check(a.out)
        for p in probs:
            print("exhaustion --check: " + p, file=sys.stderr)
        print("exhaustion --check: %s" % ("ok" if not probs else "%d problem(s)" % len(probs)))
        return 1 if probs else 0
    specs = universes_from(a.tickers)
    conn = sqlite3.connect(a.db)
    axis = RG.reference_dates(conn)
    need = sorted({t for s in specs.values() for t in s["members"]} | {s["proxy"] for s in specs.values()})
    bars = load_bars(conn, need, axis)
    conn.close()
    if not axis:
        print("exhaustion: empty axis", file=sys.stderr)
        return 1
    body = build(axis, bars, specs)
    payload = finish(body, auto_read(body))
    payload["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"), sort_keys=True, allow_nan=False)
    os.replace(tmp, a.out)
    u = payload["universes"]
    print("exhaustion: asof %s, %s, %d bytes" % (
        payload["asof"], ", ".join("%s n=%d cap=%s/%s" % (k, u[k]["n"], u[k]["capitulation_count"],
                                                          u[k]["capitulation_of"]) for k in u),
        os.path.getsize(a.out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
