#!/usr/bin/env python3
"""
Server-side port of the client-side regime / rotation / rank-receipt engine.

This is the Python port of ``deploy-jr-dash/regime_core.js`` (the canonical
``RC`` object the Technicals tab runs in the browser). It moves the CROSS-
SECTIONAL layer -- the risk-appetite composite, the sector-rotation ladder and
the cross-sectional 12-1 rank receipts -- out of the browser and into the
daily-prices pipeline, so ``technicals.json`` carries a precomputed ``regime``
block and the client can eventually just paint arrays. See
``tools/SPEC-8-regime-python-port.md``.

PARITY CONTRACT
---------------
Every function below is a VERBATIM port of the same-named function in
``regime_core.js``. ``regime_core.js`` is the oracle: ``tools/regime_parity_gen.js``
runs the oracle on a shared fixture and freezes its output into
``parity/expected.json``; ``test_technicals.py`` re-runs THESE functions on the
same fixture and asserts an exact match (integer votes / states / tiers /
streaks) or ``abs < 1e-9`` (blend, sinceRet, base-rate medians). Do not "clean
up" the semantics below -- the null-carry EMA, the null-skipping SMA, the
stable tie-break in the ranks, the inclusive vote boundaries and the lookahead-
safe streak walk are all load-bearing for parity.

WHY NOT reuse technicals.py's sma/ema/ret
-----------------------------------------
technicals.py's ``sma``/``ema`` are POSITIONAL and assume a dense, null-free
per-ticker series (``ema`` seeds with the SMA of the first n points; ``sma``
crashes on ``None``). The cross-sectional panel here is RAGGED: every series is
reindexed onto the equity trading-day axis with leading ``None`` (name not yet
listed) and forward-filled gaps, exactly like ``RC.parseCsv``. The oracle's
``ema`` carries the last value across nulls and seeds on the first non-null bar;
its ``sma`` skips nulls and averages the last n NON-NULL values. Those null
semantics are what make the ragged panel line up, so the oracle helpers are
ported here rather than reused. (Documented divergence, surfaced in the report.)

THE #3 FIX (calendar-grid windows)
----------------------------------
``RC.parseCsv`` builds its date axis from the UNION of all tickers, so crypto
(BTC-USD/ETH-USD, 7 days/wk) injects weekend columns and every equity is
forward-filled across them -- the "21-session" slope and the 50/200-day
lookbacks silently become ~N calendar days. Here the axis is the EQUITY
trading-day reference (SPY's bars); every other series is as-of aligned onto it
(equities align natively; crypto/futures collapse to their latest close on or
before each SPY session; weekends are dropped). Because the lookbacks are then
true N-trading-day windows by construction, the drift is gone at the source.
"""

from __future__ import annotations

import math

# How many reference sessions a name's last REAL bar may trail the axis by
# before it is treated as DEAD rather than unchanged. Five is a trading week:
# long enough that a listing suspension or a feed hiccup rides through it,
# short enough that a delisting cannot spend eight weeks being ranked as a
# flat line (PBS, 2026-07-17 to 2026-09-13). Shared with technicals.build.
MAX_STALE_SESSIONS = 5

# ==========================================================================
# scalar helpers -- verbatim ports of regime_core.js (null-tolerant on purpose)
# ==========================================================================

def ema(arr, span):
    """EMA that CARRIES the last value across nulls and seeds on the first
    non-null bar (prev = v). Port of RC.ema. Differs from technicals.ema, which
    seeds with the SMA of the first n points -- see module docstring."""
    out = [None] * len(arr)
    a = 2.0 / (span + 1.0)
    prev = None
    for i in range(len(arr)):
        v = arr[i]
        if v is None:
            out[i] = prev
            continue
        prev = v if prev is None else a * v + (1 - a) * prev
        out[i] = prev
    return out


def sma(arr, n):
    """SMA over the last n NON-NULL values (nulls are skipped, not counted).
    Port of RC.sma. Differs from technicals.sma, which is positional and dense."""
    out = [None] * len(arr)
    run = 0.0
    q = []
    for i in range(len(arr)):
        v = arr[i]
        if v is None:
            out[i] = None
            continue
        q.append(v)
        run += v
        if len(q) > n:
            run -= q.pop(0)
        out[i] = (run / n) if len(q) == n else None
    return out


def ratio(num, den):
    """Elementwise num/den with null- and zero-denominator guards. Port of RC.ratio."""
    out = [None] * len(num)
    for i in range(len(num)):
        a, b = num[i], den[i]
        out[i] = None if (a is None or b is None or b == 0) else a / b
    return out


def median(a):
    """Median of the non-null values; None if empty. Even count -> mean of the
    two central values. Port of RC.median."""
    b = sorted(x for x in a if x is not None)
    if not b:
        return None
    m = len(b) // 2
    return b[m] if (len(b) % 2) else (b[m - 1] + b[m]) / 2.0


# ==========================================================================
# leg votes
# ==========================================================================

def ratio_leg_series(rat):
    """Per-bar +1/0/-1 vote on a continuous ratio: +1 above a rising 50-EMA,
    -1 below a falling one, else 0. First 21 bars are 0 (slope warmup). Port of
    RC.ratioLegSeries."""
    e = ema(rat, 50)
    out = [0] * len(rat)
    for i in range(len(rat)):
        v = rat[i]
        ev = e[i]
        ep = e[i - 21] if i >= 21 else None
        if v is None or ev is None or ep is None:
            out[i] = 0
            continue
        rising = ev > ep
        above = v > ev
        out[i] = 1 if (above and rising) else (-1 if (not above and not rising) else 0)
    return out


def trend_leg_series(px, invert):
    """Same rule as ratio_leg_series but on a price series, with an optional
    sign invert (the dollar leg reads risk-on when the dollar FALLS). Port of
    RC.trendLegSeries."""
    e = ema(px, 50)
    out = [0] * len(px)
    s = -1 if invert else 1
    for i in range(len(px)):
        v = px[i]
        ev = e[i]
        ep = e[i - 21] if i >= 21 else None
        if v is None or ev is None or ep is None:
            out[i] = 0
            continue
        rising = ev > ep
        above = v > ev
        out[i] = s if (above and rising) else (-s if (not above and not rising) else 0)
    return out


# ==========================================================================
# breadth
# ==========================================================================

def breadth_series(series, names):
    """Fraction of `names` trading above their own 200-SMA, per bar; None until
    the 200-SMA exists / no denominator. Missing names are dropped. Port of
    RC.breadthSeries."""
    above = []
    for t in names:
        c = series.get(t)
        if not c:  # missing / empty -> filter(Boolean) drops it
            continue
        d = sma(c, 200)
        a = [None] * len(c)
        for i in range(len(c)):
            a[i] = None if (c[i] is None or d[i] is None) else (1 if c[i] > d[i] else 0)
        above.append(a)
    n = len(above[0]) if above else 0
    frac = [None] * n
    for i in range(n):
        up = 0
        tot = 0
        for k in range(len(above)):
            v = above[k][i]
            if v is not None:
                tot += 1
                up += v
        frac[i] = (up / tot) if tot else None
    return frac


def breadth_leg_series(frac):
    """Vote on the breadth fraction: +1 at >=0.55, -1 at <=0.35, else 0; null->0.
    Port of RC.breadthLegSeries."""
    return [0 if f is None else (1 if f >= 0.55 else (-1 if f <= 0.35 else 0)) for f in frac]


# ==========================================================================
# composite / flips / base rates
# ==========================================================================

def composite_series(legs, first_actives=None):
    """Net the legs to a state with HYSTERESIS (v60): ENTER risk-on at
    net >= +0.34 / defensive at net <= -0.34, then HOLD the state while
    |net| >= 0.17 on the same side; a cross of the OPPOSITE entry threshold
    flips directly. net = raw sum / k(i), where k(i) counts only legs that are
    ACTIVE by bar i (first_actives[j] <= i) -- a leg still in warm-up no longer
    dilutes the denominator toward neutral. first_actives=None means all legs
    active from bar 0 (k constant). `sum` stays the un-normalised total; `k`
    is emitted per bar. Port of RC.compositeSeries."""
    n = len(legs[0])
    enter = 0.34
    exit_ = 0.17
    fas = first_actives if first_actives is not None else [0] * len(legs)
    total = [0] * n
    kser = [0] * n
    state = [None] * n
    prev = None
    for i in range(n):
        s = 0
        k = 0
        for j in range(len(legs)):
            s += legs[j][i]
            if fas[j] <= i:
                k += 1
        total[i] = s
        kser[i] = k
        net = (s / k) if k else 0.0
        if prev == "risk-on":
            st = "defensive" if net <= -enter else ("risk-on" if net >= exit_ else "neutral")
        elif prev == "defensive":
            st = "risk-on" if net >= enter else ("defensive" if net <= -exit_ else "neutral")
        else:
            st = "risk-on" if net >= enter else ("defensive" if net <= -enter else "neutral")
        state[i] = st
        prev = st
    return {"sum": total, "state": state, "k": kser}


def durations(state):
    """Run-length statistics of the composite state series: per-state spell
    count + median spell length, and the CURRENT spell (state, age in
    sessions). The regime label is only worth what its persistence is -- this
    is the number that says how much to trust a fresh flip. Port of
    RC.durations."""
    by = {"risk-on": [], "neutral": [], "defensive": []}
    cur = None
    cnt = 0
    for s in state:
        if s == cur:
            cnt += 1
        else:
            if cur is not None and cur in by:
                by[cur].append(cnt)
            cur = s
            cnt = 1
    if cur is not None and cur in by:
        by[cur].append(cnt)
    out = {}
    for s in ("risk-on", "neutral", "defensive"):
        runs = by[s]
        out[s] = {"n": len(runs), "median": median(runs)}
    current = None if cur is None else {"state": cur, "age": cnt}
    return {"by_state": out, "current": current}


def flips(dates, state):
    """State-change records (no flip emitted on the first bar). Port of RC.flips."""
    out = []
    prev = None
    for i in range(len(state)):
        if state[i] != prev and prev is not None:
            out.append({"date": dates[i], "from": prev, "to": state[i]})
        prev = state[i]
    return out


def base_rates(series, names, state, H=21):
    """Forward-H-bar return base rates bucketed by the composite state at t
    (lookahead-free: t+H<n). Per bar, the cross-sectional MEDIAN return across
    `names`, then the median (and hit rate) of those per bar in each bucket. Port
    of RC.baseRates."""
    arrs = [series[t] for t in names if series.get(t)]
    n = len(arrs[0]) if arrs else 0
    bk = {"risk-on": [], "neutral": [], "defensive": []}
    t = 0
    while t + H < n:
        st = state[t]
        if st in bk:
            rets = []
            for k in range(len(arrs)):
                a = arrs[k][t]
                b = arrs[k][t + H]
                if a is not None and b is not None and a > 0:
                    rets.append(b / a - 1)
            m = median(rets)
            if m is not None:
                bk[st].append(m)
        t += 1
    res = {}
    for s in ("risk-on", "neutral", "defensive"):
        arr = bk[s]
        res[s] = {
            "n": len(arr),
            "median": median(arr),
            "hit": (sum(1 for x in arr if x > 0) / len(arr)) if arr else None,
        }
    return res


def base_rates_multi(series, names, state, hs=(5, 21, 63)):
    """base_rates at several horizons plus an unconditional 'all' bucket and an
    honesty field: n_eff = ceil(n / H), the approximate number of INDEPENDENT
    observations once the H-bar overlap of consecutive windows is accounted
    for (windows sampled every bar overlap ~H times; hit rates look far more
    stable than they are without this). Output {'h5': {...}, 'h21': {...},
    'h63': {...}}, each state -> {n, n_eff, median, hit}. Port of
    RC.baseRatesMulti."""
    arrs = [series[t] for t in names if series.get(t)]
    n = len(arrs[0]) if arrs else 0
    res = {}
    for H in hs:
        bk = {"risk-on": [], "neutral": [], "defensive": [], "all": []}
        t = 0
        while t + H < n:
            st = state[t]
            rets = []
            for k in range(len(arrs)):
                a = arrs[k][t]
                b = arrs[k][t + H]
                if a is not None and b is not None and a > 0:
                    rets.append(b / a - 1)
            m = median(rets)
            if m is not None:
                if st in bk:
                    bk[st].append(m)
                bk["all"].append(m)
            t += 1
        out = {}
        for s in ("risk-on", "neutral", "defensive", "all"):
            arr = bk[s]
            out[s] = {
                "n": len(arr),
                "n_eff": int(math.ceil(len(arr) / H)) if arr else 0,
                "median": median(arr),
                "hit": (sum(1 for x in arr if x > 0) / len(arr)) if arr else None,
            }
        res["h" + str(H)] = out
    return res


# ==========================================================================
# v60 legs: realized vol, correlation, dispersion + activation helpers
# (each function is a verbatim-parity pair with the same-named RC function)
# ==========================================================================

def ret_series(close):
    """Daily simple-return series; null where either close is null or the
    base is non-positive. Port of RC.retSeries."""
    out = [None] * len(close)
    for i in range(1, len(close)):
        a = close[i - 1]
        b = close[i]
        out[i] = (b / a - 1) if (a is not None and b is not None and a > 0) else None
    return out


def rv_series(close, n=21):
    """Annualised realised volatility: sample stdev (n-1) of the last `n`
    daily simple returns x sqrt(252). STRICT window -- null unless all `n`
    returns in the window exist. Port of RC.rvSeries."""
    r = ret_series(close)
    out = [None] * len(close)
    for i in range(len(close)):
        if i < n:
            continue
        s = 0.0
        s2 = 0.0
        ok = True
        for t in range(i - n + 1, i + 1):
            v = r[t]
            if v is None:
                ok = False
                break
            s += v
            s2 += v * v
        if not ok:
            continue
        var = (s2 - s * s / n) / (n - 1)
        if var < 0:
            var = 0.0
        out[i] = math.sqrt(var) * math.sqrt(252)
    return out


def pct_rank_series(vals, win=252):
    """Percentile of each value within its own trailing `win` values
    (inclusive of itself): count(window <= v)/win. STRICT window -- null
    unless all `win` values exist. Port of RC.pctRankSeries."""
    out = [None] * len(vals)
    for i in range(len(vals)):
        if i < win - 1:
            continue
        v = vals[i]
        if v is None:
            continue
        c = 0
        ok = True
        for t in range(i - win + 1, i + 1):
            w = vals[t]
            if w is None:
                ok = False
                break
            if w <= v:
                c += 1
        if ok:
            out[i] = c / win
    return out


def pct_vote_series(pct, lo=0.30, hi=0.70):
    """Vote on a percentile series: +1 at or below `lo` (calm / dispersed),
    -1 at or above `hi` (stressed / crowded), else 0; null -> 0. The band is
    fixed at 30/70 -- the same own-trailing-year framing the setup column's
    ATR percentile uses. Port of RC.pctVoteSeries."""
    return [0 if p is None else (1 if p <= lo else (-1 if p >= hi else 0)) for p in pct]


def corr_pair_series(a, b, win=63):
    """Rolling Pearson correlation of two price series' daily returns over a
    STRICT `win` window (null unless all pairs exist). Port of
    RC.corrPairSeries."""
    ra = ret_series(a)
    rb = ret_series(b)
    n = min(len(a), len(b))
    out = [None] * n
    for i in range(n):
        if i < win:
            continue
        sa = 0.0
        sb = 0.0
        saa = 0.0
        sbb = 0.0
        sab = 0.0
        ok = True
        for t in range(i - win + 1, i + 1):
            x = ra[t]
            y = rb[t]
            if x is None or y is None:
                ok = False
                break
            sa += x
            sb += y
            saa += x * x
            sbb += y * y
            sab += x * y
        if not ok:
            continue
        cov = sab - sa * sb / win
        va = saa - sa * sa / win
        vb = sbb - sb * sb / win
        if va <= 0 or vb <= 0:
            continue
        out[i] = cov / math.sqrt(va * vb)
    return out


def corr_vote_series(corr, thr=0.20):
    """Vote on a correlation series by SIGN with a dead zone: +1 at or below
    -thr (bonds hedge stocks -> growth-driven tape, risk-supportive), -1 at or
    above +thr (no hedge -> inflation/liquidity-driven, risk-hostile), else 0.
    Port of RC.corrVoteSeries."""
    return [0 if c is None else (1 if c <= -thr else (-1 if c >= thr else 0)) for c in corr]


def avg_corr_series(series, names, win=63, min_n=8):
    """Average pairwise correlation across `names` over a rolling `win`
    window, via the sigma-weighted portfolio-variance identity:
    avgcorr = (N^2*Var_p - sum(var_i)) / ((sum(sd_i))^2 - sum(var_i)), with
    equal-weight portfolio p and sample (n-1) variances. Membership at bar i =
    names whose returns are all non-null in the window; null when fewer than
    `min_n` members or a degenerate denominator. Port of RC.avgCorrSeries."""
    rets = []
    for t in names:
        c = series.get(t)
        if c:
            rets.append(ret_series(c))
    n = len(rets[0]) if rets else 0
    out = [None] * n
    for i in range(n):
        if i < win:
            continue
        members = []
        for k in range(len(rets)):
            ok = True
            for t in range(i - win + 1, i + 1):
                if rets[k][t] is None:
                    ok = False
                    break
            if ok:
                members.append(k)
        N = len(members)
        if N < min_n:
            continue
        s1 = 0.0
        s2 = 0.0
        psum = 0.0
        psum2 = 0.0
        for t in range(i - win + 1, i + 1):
            p = 0.0
            for k in members:
                p += rets[k][t]
            p /= N
            psum += p
            psum2 += p * p
        for k in members:
            s = 0.0
            ss = 0.0
            for t in range(i - win + 1, i + 1):
                v = rets[k][t]
                s += v
                ss += v * v
            var = (ss - s * s / win) / (win - 1)
            if var < 0:
                var = 0.0
            s1 += math.sqrt(var)
            s2 += var
        var_p = (psum2 - psum * psum / win) / (win - 1)
        denom = s1 * s1 - s2
        if denom <= 0:
            continue
        out[i] = (N * N * var_p - s2) / denom
    return out


def leg_first_active(cont):
    """First bar where an EMA-slope leg (ratio/trend rule) can vote non-zero:
    i >= 21 with cont, its 50-EMA and the EMA 21 bars back all non-null.
    Returns len(cont) if never. Port of RC.legFirstActive."""
    e = ema(cont, 50)
    for i in range(len(cont)):
        if i >= 21 and cont[i] is not None and e[i] is not None and e[i - 21] is not None:
            return i
    return len(cont)


def first_non_null(arr):
    """Index of the first non-null value, or len(arr). Port of RC.firstNonNull."""
    for i in range(len(arr)):
        if arr[i] is not None:
            return i
    return len(arr)


# ==========================================================================
# rank receipts (cross-sectional 12-1 momentum percentile + streak)
# ==========================================================================

def mom121_series(close):
    """12-1 momentum series: close[i-21]/close[i-273]-1 (needs i>=273). Port of
    RC.mom121Series."""
    LONG, SHORT = 273, 21
    out = [None] * len(close)
    for i in range(len(close)):
        a = close[i - LONG] if i >= LONG else None
        b = close[i - SHORT] if i >= SHORT else None
        out[i] = (b / a - 1) if (a is not None and b is not None and a > 0) else None
    return out


def rank_receipts(series, names):
    """Cross-sectional percentile rank of each name's 12-1 momentum, per bar,
    then a streak/entry/since-return receipt on the last bar for names currently
    in the top (>=0.8) or bottom (<=0.2) quintile. LOOKAHEAD-SAFE: each bar's
    rank uses only prices at or before it. Port of RC.rankReceipts."""
    present = [t for t in names if series.get(t)]
    mom = [mom121_series(series[t]) for t in present]
    n = len(mom[0]) if mom else 0
    rank = [[None] * n for _ in present]
    for i in range(n):
        vals = []
        for k in range(len(present)):
            v = mom[k][i]
            if v is not None:
                vals.append((k, v))
        vals.sort(key=lambda p: p[1])  # ascending, stable -> ties keep k order
        L = len(vals)
        for j in range(L):
            rank[vals[j][0]][i] = (j / (L - 1)) if L > 1 else 0.5

    def tier_at(k, i):
        r = rank[k][i]
        if r is None:
            return None
        return "top" if r >= 0.8 else ("bottom" if r <= 0.2 else "mid")

    last = n - 1
    out = {}
    for k2 in range(len(present)):
        t = present[k2]
        cur_tier = tier_at(k2, last) if last >= 0 else None
        rec = {
            "ticker": t,
            "rank": None if (last < 0 or rank[k2][last] is None) else rank[k2][last],
            "tier": cur_tier,
            "streak": 0,
            "sinceRet": None,
            "entryIdx": None,
        }
        if cur_tier == "top" or cur_tier == "bottom":
            i2 = last
            s = 0
            while i2 >= 0 and tier_at(k2, i2) == cur_tier:
                s += 1
                i2 -= 1
            rec["streak"] = s
            rec["entryIdx"] = i2 + 1
            c = series[t]
            pe = c[rec["entryIdx"]]
            pn = c[last]
            rec["sinceRet"] = (pn / pe - 1) if (pe is not None and pn is not None and pe > 0) else None
        out[t] = rec
    return out


# ==========================================================================
# sector ladder
# ==========================================================================

def ret(close, lag):
    """Trailing return over `lag` bars ending on the last bar. Port of RC.ret."""
    i = len(close) - 1
    a = close[i - lag] if i >= lag else None
    b = close[i]
    return (b / a - 1) if (a is not None and b is not None and a > 0) else None


def basket_series(series, members):
    """Equal-weight, daily-rebalanced index (base 100) of the member closes that
    are present: each session's basket return is the plain mean of the members'
    simple returns that day (a member with no bar on either side of the session
    sits out that day; a day with no member data carries the prior level).
    None until the first computable return. Stream D (9/2/26). Port of
    RC.basketSeries -- the summation order (members order) is part of parity."""
    cs = [series[m] for m in members if series.get(m)]
    if not cs:
        return None
    n = len(cs[0])
    out = [None] * n
    v = None
    for i in range(n):
        acc = 0.0
        cnt = 0
        if i >= 1:
            for c in cs:
                if i < len(c):
                    a = c[i - 1]
                    b = c[i]
                    if a is not None and b is not None and a > 0:
                        acc += b / a - 1
                        cnt += 1
        if cnt:
            v = (100.0 if v is None else v) * (1.0 + acc / cnt)
        out[i] = v
    return out


def _series_stale(c):
    """True when a series carried real values and has since gone None -- i.e.
    build_panel stopped forward-filling it because the name stopped printing
    bars. A series that has simply not started yet (all None) is not stale."""
    if not c or c[-1] is not None:
        return False
    return any(v is not None for v in c)


def sector_ladder(series, etfs):
    """Rank sector ETFs by a 63d/126d blended return, tag each into thirds of
    the field by 63d return with a streak, sort by blend desc, and flag an
    offense/defense divergence (an offense AND a defense sleeve both in the top
    third). Each row also carries the SHORT-TERM read: r21 (21 trading sessions
    ~= 1 calendar month) and third21, the row's third of the field by 21d
    return on the last bar -- the renderer surfaces 21d-vs-63d disagreement as
    an early-turn tell.

    v60 DEDUP: an etf entry may carry "grp" (sleeve group). The thirds /
    streak / divergence FIELD keeps only the first-listed present member of
    each group (the primary); later members are TWINS -- they still show
    levels (r21/r63/r126/blend) but get third=third21=None, streak=0 and
    twin_of=<primary ticker>, so near-duplicate proxies (SMH+SOXX, the three
    gold-miner ETFs) no longer double-count the same sleeve in the field or
    fire the divergence/turn tells twice. `etfs` is a list of
    {"t","name","side"[,"grp"][,"sector"][,"basket"]}. Port of RC.sectorLadder.

    Stream D (9/2/26): an entry may carry `basket` (a list of member tickers);
    its series is basket_series() over the members present and its row is
    flagged basket=True with `members`. `sector` is carried through untouched
    (display grouping only -- the field, thirds, streaks and tells never see
    it).

    9/15/26: an entry may also carry `field` -- the name of the ladder it
    belongs to ("style", "bonds"; REG_ETFS entries carry none). It is copied
    onto the row ONLY when set, exactly like `stale`, so the sector ladder's
    emitted row shape (and with it the JS-oracle parity fixtures) is
    byte-identical to what it was before the two extra ladders existed."""
    ser = {}
    for e in etfs:
        ser[e["t"]] = basket_series(series, e["basket"]) if e.get("basket") else series.get(e["t"])
    series = ser
    present = [e for e in etfs if series.get(e["t"])]
    # A sled whose series has real history but has gone None at the end is DEAD,
    # not flat (build_panel's staleness cutoff). Out of the field entirely, and
    # not eligible to be a group primary -- so a dead primary hands the sleeve
    # to the next live member instead of silencing the whole group.
    stale_flag = [_series_stale(series[e["t"]]) for e in present]
    seen_grp = {}
    field_set = {}
    twin_of = [None] * len(present)
    for k in range(len(present)):
        if stale_flag[k]:
            continue
        g = present[k].get("grp")
        if not g:
            field_set[k] = 1
        elif g not in seen_grp:
            seen_grp[g] = k
            field_set[k] = 1
        else:
            twin_of[k] = present[seen_grp[g]]["t"]
    field_idx = sorted(field_set.keys())
    rows = []
    for k in range(len(present)):
        e = present[k]
        c = series[e["t"]]
        r5 = ret(c, 5)     # #46(c): 1 week = 5 sessions, display only (not in the sort key)
        r21 = ret(c, 21)
        r63 = ret(c, 63)
        r126 = ret(c, 126)
        if r63 is not None and r126 is not None:
            blend = (r63 + r126) / 2.0
        else:
            blend = r63 if r63 is not None else r126
        row = {"t": e["t"], "name": e["name"], "side": e.get("side"),
               "r5": r5, "r21": r21, "r63": r63, "r126": r126, "blend": blend,
               "twin_of": twin_of[k], "sector": e.get("sector"), "gics": e.get("gics"),
               "basket": bool(e.get("basket")), "members": e.get("basket") or None}
        # Emitted ONLY when true: every non-stale row keeps the oracle's exact
        # key set, so parity/expected.json still matches byte for byte.
        if stale_flag[k]:
            row["stale"] = True
        # Same rule as `stale` above, for the same reason: carried only when the
        # etf entry sets it (REG_STYLE / REG_BONDS), so a REG_ETFS row's key set
        # is untouched.
        if e.get("field"):
            row["field"] = e["field"]
        rows.append(row)
    n = len(series[present[0]["t"]]) if present else 0

    def ret_ser(k, lag):
        c = series[present[k]["t"]]
        s = [None] * len(c)
        for i in range(len(c)):
            a = c[i - lag] if i >= lag else None
            b = c[i]
            s[i] = (b / a - 1) if (a is not None and b is not None and a > 0) else None
        return s

    # return series for FIELD members only, keyed by position in `present`
    r63ser = {k: ret_ser(k, 63) for k in field_idx}
    r21ser = {k: ret_ser(k, 21) for k in field_idx}

    def third_at(ser, k, i):
        mine = ser[k][i]
        if mine is None:
            return None
        vals = []
        for q in field_idx:
            v = ser[q][i]
            if v is not None:
                vals.append(v)
        vals.sort(reverse=True)  # descending, stable -> ties keep original order
        L = len(vals)
        pos = vals.index(mine)   # first occurrence, mirrors JS Array.indexOf
        if pos < math.ceil(L / 3):
            return "top"
        if pos >= L - math.ceil(L / 3):
            return "bottom"
        return "mid"

    last = n - 1
    for k in range(len(rows)):
        # STALE (2026-09-13): build_panel stops forward-filling a name five
        # sessions past its last real bar, so a dead sled's series now ends in
        # None. Mark the row and keep it OUT of the field -- a flat line has no
        # honest rank, third or streak. The key is emitted only when true, so
        # the JS-oracle parity fixtures (none of which end in None) are
        # byte-identical; a divergence in tools/regime_e2e_parity.py on a stale
        # sled is the signal, not a regression.
        if rows[k].get("stale"):
            rows[k]["third"] = None
            rows[k]["third21"] = None
            rows[k]["streak"] = 0
            continue
        if k not in field_set:   # twin: levels only, out of the field
            rows[k]["third"] = None
            rows[k]["third21"] = None
            rows[k]["streak"] = 0
            continue
        t_third = third_at(r63ser, k, last) if last >= 0 else None
        rows[k]["third"] = t_third
        rows[k]["third21"] = third_at(r21ser, k, last) if last >= 0 else None
        rows[k]["streak"] = 0
        if t_third == "top" or t_third == "bottom":
            i = last
            s = 0
            while i >= 0 and third_at(r63ser, k, i) == t_third:
                s += 1
                i -= 1
            rows[k]["streak"] = s

    rows.sort(key=lambda r: (r["blend"] if r["blend"] is not None else -9e9), reverse=True)
    top_off = any(r["third"] == "top" and r["side"] == "offense" for r in rows)
    top_def = any(r["third"] == "top" and r["side"] == "defense" for r in rows)
    return {"rows": rows, "divergence": top_off and top_def}


def ladder_payload(lad, rnd, with_field=False):
    """Round + key-filter a sector_ladder() result into its technicals.json
    shape. The key set AND ITS ORDER are the sector ladder's frozen emitted
    shape; `field` is appended only for the style/bond ladders (9/15/26), so
    `regime.ladder` stays byte-identical to what the inline emitter produced
    before they existed. `rnd` is build_regime's rounding shim."""
    rows = [{
        "t": r["t"], "name": r["name"], "side": r["side"],
        "r5": rnd(r.get("r5")), "r21": rnd(r["r21"]), "r63": rnd(r["r63"]), "r126": rnd(r["r126"]),
        "blend": rnd(r["blend"]), "third": r["third"], "third21": r["third21"],
        "streak": r["streak"], "twin_of": r["twin_of"],
        "sector": r.get("sector"), "gics": r.get("gics"), "basket": r.get("basket", False), "members": r.get("members"),
    } for r in lad["rows"]]
    if with_field:
        for out_row, r in zip(rows, lad["rows"]):
            out_row["field"] = r.get("field")
    return {"rows": rows, "divergence": lad["divergence"]}


# ==========================================================================
# EMITTER config  (moved out of index.html renderRegimePanel / REG_ETFS)
# ==========================================================================
# The sector-rotation ladder + offense/defense divergence field. Verbatim from
# index.html `REG_ETFS` (v52). `side` drives the divergence tell.
REG_ETFS = [
    # Stream E (9/2/26, Jake: "select down into the entire GICS hierarchy ... regime
    # watching is about foresight not backward looking analysis"): the ladder maps the
    # WHOLE MARKET on MSCI's published GICS structure (data/gics_structure.json,
    # vendored from msci.com; refresh annually). Every sled carries `gics`, the code
    # of the node it proxies (2 = sector, 4 = industry group, 6 = industry, 8 = sub-
    # industry) or a pseudo-node ("TH" themes, "BR" broad, "RG" regions); `sector` is
    # the display group (the GICS sector name or the pseudo-sector). DISPLAY ONLY --
    # the field/thirds/streaks/tells never read gics/sector.
    # Proxy rule (Jake: "the hybrid of equal weight baskets or representative
    # etfs"): a representative ETF where a clean one exists, an equal-weight basket
    # (`basket: [...]`, regime.basket_series == RC.basketSeries) otherwise. `grp`
    # twins stay near-duplicates only. `side` drives the divergence tell and is set
    # ONLY on clearly polar sleds (growth-cyclical = offense, defensive = defense);
    # everything else is None so the tell keeps its meaning in an ~80-row field.
    # Adding rows re-thirds the field: streaks reset again at 9/2/26 (Stream E).
    # ---- Broad / regions ----
    {"t": "SPY", "name": "S&P 500", "side": None, "gics": "BR", "sector": "Broad"},
    {"t": "QQQ", "name": "Nasdaq 100", "side": "offense", "gics": "BR", "sector": "Broad"},
    {"t": "IWM", "name": "Small caps", "side": "offense", "gics": "BR", "sector": "Broad"},
    {"t": "EWY", "name": "Korea", "side": None, "gics": "RG", "sector": "Regions"},
    # ---- 10 Energy ----
    {"t": "XLE", "name": "Energy (sector)", "side": None, "gics": "10", "sector": "Energy"},
    {"t": "OIH", "name": "Oil & gas equipment & services", "side": None, "gics": "101010", "sector": "Energy", "grp": "oilsvc"},
    {"t": "XES", "name": "Oil & gas equipment (XES)", "side": None, "gics": "101010", "sector": "Energy", "grp": "oilsvc"},
    {"t": "XOP", "name": "Oil & gas E&P", "side": None, "gics": "10102020", "sector": "Energy"},
    {"t": "AMLP", "name": "Midstream (storage & transport)", "side": None, "gics": "10102040", "sector": "Energy"},
    {"t": "URA", "name": "Uranium", "side": None, "gics": "10102050", "sector": "Energy"},
    # ---- 15 Materials ----
    {"t": "XLB", "name": "Materials (sector)", "side": None, "gics": "15", "sector": "Materials"},
    {"t": "XME", "name": "Metals & mining", "side": "offense", "gics": "151040", "sector": "Materials"},
    {"t": "SLX", "name": "Steel", "side": "offense", "gics": "15104050", "sector": "Materials"},
    {"t": "COPX", "name": "Copper miners", "side": "offense", "gics": "15104020", "sector": "Materials", "grp": "copper"},
    {"t": "ICOP", "name": "Copper miners (ICOP)", "side": "offense", "gics": "15104020", "sector": "Materials", "grp": "copper"},
    {"t": "LIT", "name": "Lithium & battery chain", "side": "offense", "gics": "151040", "sector": "Materials"},
    {"t": "REMX", "name": "Rare earths / strategic metals", "side": None, "gics": "15104020", "sector": "Materials"},
    {"t": "GDX", "name": "Gold miners", "side": "defense", "gics": "15104030", "sector": "Materials", "grp": "gold"},
    {"t": "GDXJ", "name": "Jr gold miners", "side": "defense", "gics": "15104030", "sector": "Materials", "grp": "gold"},
    {"t": "RING", "name": "Gold miners (RING)", "side": "defense", "gics": "15104030", "sector": "Materials", "grp": "gold"},
    {"t": "SILJ", "name": "Jr silver miners", "side": "defense", "gics": "15104045", "sector": "Materials"},
    {"t": "SLV", "name": "Silver (metal)", "side": "defense", "gics": "15104045", "sector": "Materials"},
    # ---- 20 Industrials ----
    {"t": "XLI", "name": "Industrials (sector)", "side": "offense", "gics": "20", "sector": "Industrials"},
    {"t": "ITA", "name": "Aerospace & defense", "side": None, "gics": "201010", "sector": "Industrials", "grp": "defense"},
    {"t": "XAR", "name": "Aerospace & defense (XAR)", "side": None, "gics": "201010", "sector": "Industrials", "grp": "defense"},
    {"t": "PPA", "name": "Aerospace & defense (PPA)", "side": None, "gics": "201010", "sector": "Industrials", "grp": "defense"},
    {"t": "GRID", "name": "Grid / electrical equipment", "side": "offense", "gics": "201040", "sector": "Industrials"},
    {"t": "PAVE", "name": "Infrastructure", "side": "offense", "gics": "2010", "sector": "Industrials"},
    {"t": "IYT", "name": "Transportation", "side": "offense", "gics": "2030", "sector": "Industrials", "grp": "transport"},
    {"t": "XTN", "name": "Transportation (XTN)", "side": "offense", "gics": "2030", "sector": "Industrials", "grp": "transport"},
    {"t": "JETS", "name": "Airlines", "side": "offense", "gics": "203020", "sector": "Industrials"},
    # ---- 25 Consumer Discretionary ----
    {"t": "XLY", "name": "Consumer discretionary (sector)", "side": "offense", "gics": "25", "sector": "Consumer Discretionary"},
    {"t": "DRIV", "name": "Autos / EV & autonomous", "side": "offense", "gics": "251020", "sector": "Consumer Discretionary"},
    {"t": "ITB", "name": "Homebuilders", "side": "offense", "gics": "25201030", "sector": "Consumer Discretionary", "grp": "homebuild"},
    {"t": "XHB", "name": "Homebuilders (XHB)", "side": "offense", "gics": "25201030", "sector": "Consumer Discretionary", "grp": "homebuild"},
    {"t": "PEJ", "name": "Leisure & entertainment", "side": "offense", "gics": "253010", "sector": "Consumer Discretionary"},
    {"t": "XRT", "name": "Retail", "side": "offense", "gics": "2550", "sector": "Consumer Discretionary"},
    # ---- 30 Consumer Staples ----
    {"t": "XLP", "name": "Consumer staples (sector)", "side": "defense", "gics": "30", "sector": "Consumer Staples"},
    {"t": "PBJ", "name": "Food & beverage", "side": "defense", "gics": "3020", "sector": "Consumer Staples"},
    # ---- 35 Health Care ----
    {"t": "XLV", "name": "Health care (sector)", "side": "defense", "gics": "35", "sector": "Health Care"},
    {"t": "IHI", "name": "Medical devices", "side": None, "gics": "351010", "sector": "Health Care"},
    {"t": "IHF", "name": "Health care providers", "side": "defense", "gics": "351020", "sector": "Health Care"},
    {"t": "XBI", "name": "Biotech (equal-weight)", "side": None, "gics": "352010", "sector": "Health Care", "grp": "biotech"},
    {"t": "IBB", "name": "Biotech (IBB)", "side": None, "gics": "352010", "sector": "Health Care", "grp": "biotech"},
    {"t": "XPH", "name": "Pharmaceuticals", "side": "defense", "gics": "352020", "sector": "Health Care", "grp": "pharma"},
    {"t": "PJP", "name": "Pharmaceuticals (PJP)", "side": "defense", "gics": "352020", "sector": "Health Care", "grp": "pharma"},
    # ---- 40 Financials ----
    {"t": "XLF", "name": "Financials (sector)", "side": None, "gics": "40", "sector": "Financials"},
    {"t": "KBE", "name": "Banks", "side": None, "gics": "401010", "sector": "Financials"},
    {"t": "KRE", "name": "Regional banks", "side": None, "gics": "40101015", "sector": "Financials"},
    {"t": "IAI", "name": "Capital markets / broker-dealers", "side": "offense", "gics": "402030", "sector": "Financials", "grp": "capmkts"},
    {"t": "KCE", "name": "Capital markets (KCE)", "side": "offense", "gics": "402030", "sector": "Financials", "grp": "capmkts"},
    {"t": "FINX", "name": "Fintech / payments", "side": "offense", "gics": "40201060", "sector": "Financials"},
    {"t": "KIE", "name": "Insurance", "side": None, "gics": "403010", "sector": "Financials"},
    # ---- 45 Information Technology ----
    {"t": "XLK", "name": "Information technology (sector)", "side": "offense", "gics": "45", "sector": "Information Technology"},
    {"t": "IGV", "name": "Software", "side": "offense", "gics": "451030", "sector": "Information Technology", "grp": "software"},
    {"t": "WCLD", "name": "High-multiple SaaS (WCLD)", "side": "offense", "gics": "45103010", "sector": "Information Technology", "grp": "software"},
    {"t": "CIBR", "name": "Cybersecurity", "side": "offense", "gics": "45103020", "sector": "Information Technology", "grp": "cyber"},
    {"t": "HACK", "name": "Cybersecurity (HACK)", "side": "offense", "gics": "45103020", "sector": "Information Technology", "grp": "cyber"},
    {"t": "OPTICS", "name": "Optics / photonics (basket)", "side": "offense", "gics": "4520", "sector": "Information Technology",
     "basket": ["LITE", "COHR", "AAOI", "CIEN", "FN", "GLW"]},
    {"t": "SMH", "name": "Semis", "side": "offense", "gics": "453010", "sector": "Information Technology", "grp": "semis"},
    {"t": "SOXX", "name": "Semis (SOXX)", "side": "offense", "gics": "453010", "sector": "Information Technology", "grp": "semis"},
    {"t": "XSD", "name": "Semis equal-weight (XSD)", "side": "offense", "gics": "453010", "sector": "Information Technology", "grp": "semis"},
    {"t": "DRAM", "name": "Memory", "side": "offense", "gics": "45301020", "sector": "Information Technology"},
    {"t": "POWERSEMI", "name": "Power semis (basket)", "side": "offense", "gics": "45301020", "sector": "Information Technology",
     "basket": ["ON", "NVTS", "MPWR", "AOSL", "VICR", "STM"]},
    # ---- 50 Communication Services ----
    {"t": "XLC", "name": "Communication services (sector)", "side": None, "gics": "50", "sector": "Communication Services"},
    # 502010 Media: PBS was the sled here until 2026-09-13. It printed no bar
    # after 2026-07-17 (delisted), and the forward-fill then ranked eight weeks
    # of flat line in the top third on 21d. Removed rather than replaced: the
    # sector already has XLC, and a wrong proxy is worse than a missing one.
    {"t": "ESPO", "name": "Video games & esports", "side": "offense", "gics": "502020", "sector": "Communication Services"},
    {"t": "FDN", "name": "Internet / interactive media", "side": "offense", "gics": "502030", "sector": "Communication Services"},
    # ---- 55 Utilities ----
    {"t": "XLU", "name": "Utilities (sector)", "side": "defense", "gics": "55", "sector": "Utilities"},
    {"t": "ICLN", "name": "Clean energy / renewables", "side": None, "gics": "551050", "sector": "Utilities", "grp": "renew"},
    {"t": "TAN", "name": "Solar (TAN)", "side": None, "gics": "551050", "sector": "Utilities", "grp": "renew"},
    {"t": "NLR", "name": "Nuclear energy", "side": None, "gics": "551050", "sector": "Utilities"},
    # ---- 60 Real Estate ----
    {"t": "XLRE", "name": "Real estate (sector)", "side": None, "gics": "60", "sector": "Real Estate", "grp": "reit"},
    {"t": "VNQ", "name": "REITs (VNQ)", "side": None, "gics": "6010", "sector": "Real Estate", "grp": "reit"},
    {"t": "DTCR", "name": "Data center REITs & digital infra", "side": "offense", "gics": "60108050", "sector": "Real Estate"},
    # ---- Themes (cross-sector; not GICS nodes) ----
    {"t": "ARTY", "name": "AI basket (ARTY)", "side": "offense", "gics": "TH", "sector": "Themes"},
    {"t": "HYPERSCALE", "name": "Hyperscalers (basket)", "side": "offense", "gics": "TH", "sector": "Themes",
     "basket": ["MSFT", "GOOGL", "AMZN", "META", "ORCL"]},
    {"t": "NEOCLOUD", "name": "Neoclouds (basket)", "side": "offense", "gics": "TH", "sector": "Themes",
     "basket": ["CRWV", "NBIS", "IREN", "APLD", "CORZ", "WULF"]},
    {"t": "QUANTUM", "name": "Quantum (basket)", "side": "offense", "gics": "TH", "sector": "Themes",
     "basket": ["IONQ", "RGTI", "QBTS", "QUBT", "ARQQ"]},
    {"t": "UFO", "name": "Space", "side": "offense", "gics": "TH", "sector": "Themes"},
    {"t": "BOTZ", "name": "Robotics & AI", "side": "offense", "gics": "TH", "sector": "Themes"},
]


# The STYLE-BOX ladder (9/15/26, Jake: "add style boxes to the sector rotation
# tracking -- large cap (value/growth/core) down to micro caps"). The Russell
# size/style grid -- three caps x core/growth/value, plus micro -- ranked only
# against EACH OTHER, so "growth over value" and "large over small" read
# straight off the thirds. It is a separate `field` rather than ten more
# REG_ETFS rows because these are whole-market slices, not sleeves: dropped
# into the ~80-row sector field they would settle in the middle third (they ARE
# roughly its cap-weighted average), say nothing there, and re-third every
# sector row on the way in. `side` is set on the polar boxes only -- growth =
# offense, value = defense -- so the divergence tell reads "growth AND value
# are both leading", a real broadening signal; core and micro stay None. IWM is
# deliberately also in REG_ETFS: separate fields never dedup against each other.
REG_STYLE = [
    {"t": "IWB", "name": "Large cap core", "side": None, "sector": "Large cap", "field": "style"},
    {"t": "IWF", "name": "Large cap growth", "side": "offense", "sector": "Large cap", "field": "style"},
    {"t": "IWD", "name": "Large cap value", "side": "defense", "sector": "Large cap", "field": "style"},
    {"t": "IWR", "name": "Mid cap core", "side": None, "sector": "Mid cap", "field": "style"},
    {"t": "IWP", "name": "Mid cap growth", "side": "offense", "sector": "Mid cap", "field": "style"},
    {"t": "IWS", "name": "Mid cap value", "side": "defense", "sector": "Mid cap", "field": "style"},
    {"t": "IWM", "name": "Small cap core", "side": None, "sector": "Small cap", "field": "style"},
    {"t": "IWO", "name": "Small cap growth", "side": "offense", "sector": "Small cap", "field": "style"},
    {"t": "IWN", "name": "Small cap value", "side": "defense", "sector": "Small cap", "field": "style"},
    {"t": "IWC", "name": "Micro cap", "side": None, "sector": "Micro cap", "field": "style"},
]

# The BOND-CATEGORY ladder (9/15/26, Jake: "also major bond categories/proxies/
# indices"). Duration buckets, credit quality and the two non-US-Treasury
# sovereign sleeves, ranked against each other on the same 63d/126d blend.
# Again a separate `field`, and here it is not a preference but a correctness
# point: on total return a bond sleeve sits in the BOTTOM THIRD of every bull
# tape, so inside the sector field these fourteen rows would say nothing about
# fixed income while shifting the thirds under all ~80 sector rows. Ranked
# against each other they carry the reads that matter -- HY vs IG (credit
# appetite), TLT vs SHY (the duration call), TIP vs IEF (break-evens).
# 9/15/26 #91: this stays the FOURTEEN-ticker universe (price backfill, tests,
# BOND_DURATION), but it is no longer the ranked field. The field is
# REG_BOND_SPREAD (these minus TREASURY_RUNGS) ranked on EXCESS return; the #91
# block below REG_BONDS says why a total-return rank of all fourteen was really
# just a duration rank.
# `side`: spread product that trades with equities is offense (HYG, BKLN, EMB);
# rate-driven flight-to-quality paper is defense (bills through the long bond,
# TIPS, the aggregate, agency MBS); LQD, MUB and BNDX sit between the two and
# stay None. TLT/IEF/HYG/LQD are also macro LEG inputs -- same series, different
# job, and no dedup across fields.
REG_BONDS = [
    {"t": "BIL", "name": "T-bills 1-3m", "side": "defense", "sector": "Treasuries", "field": "bonds"},
    {"t": "SHY", "name": "Treasuries 1-3y", "side": "defense", "sector": "Treasuries", "field": "bonds"},
    {"t": "IEF", "name": "Treasuries 7-10y", "side": "defense", "sector": "Treasuries", "field": "bonds"},
    {"t": "TLH", "name": "Treasuries 10-20y", "side": "defense", "sector": "Treasuries", "field": "bonds"},
    {"t": "TLT", "name": "Treasuries 20y+", "side": "defense", "sector": "Treasuries", "field": "bonds"},
    {"t": "TIP", "name": "TIPS", "side": "defense", "sector": "Inflation", "field": "bonds"},
    {"t": "AGG", "name": "US aggregate", "side": "defense", "sector": "Aggregate", "field": "bonds"},
    {"t": "MBB", "name": "Agency MBS", "side": "defense", "sector": "Mortgages", "field": "bonds"},
    {"t": "LQD", "name": "IG corporates", "side": None, "sector": "Credit", "field": "bonds"},
    {"t": "HYG", "name": "High yield", "side": "offense", "sector": "Credit", "field": "bonds"},
    {"t": "BKLN", "name": "Leveraged loans", "side": "offense", "sector": "Credit", "field": "bonds"},
    {"t": "MUB", "name": "Munis", "side": None, "sector": "Munis", "field": "bonds"},
    {"t": "BNDX", "name": "Intl IG (hedged)", "side": None, "sector": "International", "field": "bonds"},
    {"t": "EMB", "name": "EM sovereign USD", "side": "offense", "sector": "International", "field": "bonds"},
]

# ==========================================================================
# #91 (9/15/26): the bond field, rebuilt on EXCESS RETURN
# ==========================================================================
# The 9/15 bond ladder ranked all fourteen rows on the same 63d/126d TOTAL
# return as the sector ladder, and that mostly measured DURATION. Five of the
# rows (BIL SHY IEF TLH TLT) are ONE factor at five maturities and rank in
# exact maturity order on any rate move -- the first payload's 63d column was
# BIL +0.9, SHY -0.1, IEF -2.6, TLH -3.7, TLT -5.1, which is not a ranking, it
# is the yield curve written sideways -- and every other row carries its own
# duration on top of whatever else it is. So "credit is leading" read off that
# field was really "short duration is leading": HYG's 63d -1.1% against a
# duration-matched ~3.3y Treasury at about -0.9% is FLAT credit, not a top
# third.
#
# The fix is the one the bond indices themselves use. Two objects instead of
# one ranked field:
#
#   * `bond_curve`  -- the five Treasury rungs pulled OUT of the ranked field
#     and shown as a curve strip, each with a yield-change proxy. Five points
#     on one factor cannot rank against each other; as a curve they say what
#     the rate move actually was.
#   * `bond_ladder` -- the other nine rows ranked on EXCESS RETURN over a
#     duration-matched Treasury. What is left after the rate move is the part
#     that is actually a credit / carry / breakeven call, which is the only
#     part a ranking of these nine can honestly be about.
#
# Every field the ladder already emitted (r5 r21 r63 r126 blend third third21
# streak, and the divergence tell built on them) therefore now means EXCESS
# return. The total return is still carried, as tr21/tr63/tr126/trblend, so the
# renderer can show both -- and the difference between them is itself a read.

# Approximate EFFECTIVE duration in years (approx, 2026-09) -- round numbers off
# the issuer fact sheets, and deliberately NOT refreshed per session. The match
# only has to strip the first-order rate move: a duration that has drifted 0.2y
# moves an excess return by basis points, while the thing it is removing is
# whole percent. TIP is a genuine ~6.6y and is matched against NOMINAL
# Treasuries on purpose -- its excess return IS the breakeven direction.
BOND_DURATION = {
    "BIL": 0.1, "SHY": 1.9, "IEF": 7.3, "TLH": 12.5, "TLT": 16.5,
    "HYG": 3.3, "LQD": 8.4, "BKLN": 0.25, "EMB": 7.0, "MUB": 6.4,
    "MBB": 5.8, "BNDX": 7.0, "AGG": 6.0, "TIP": 6.6,
}

# The curve strip, in MATURITY order. Out of the ranked field entirely.
TREASURY_RUNGS = ["BIL", "SHY", "IEF", "TLH", "TLT"]

# The ranked field: everything else. DERIVED from REG_BONDS rather than written
# out again, so the ticker list, the price backfill and the existing REG_BONDS
# tests stay exactly as they are and the two lists cannot drift apart.
REG_BOND_SPREAD = [e for e in REG_BONDS if e["t"] not in set(TREASURY_RUNGS)]

# Read thresholds. Dead zones, not signs: a 63d total return of 0.1% on IEF is
# noise, and calling that "rallying" would make the quadrant label flicker.
DURATION_BAND = 0.005    # +/-0.5% on IEF's 63d TOTAL return
CURVE_BAND_BP = 10.0     # +/-10bp on the 63d long-minus-short yield proxy
CREDIT_BAND = 0.0025     # +/-0.25% on the median 63d EXCESS return of the credit sleeves
DY_MIN_DUR = 0.5         # below this a total return is carry, not a yield move
CREDIT_SLEEVES = ["HYG", "LQD", "BKLN", "EMB"]


def _bracket_rungs(dur):
    """(lo, hi, w) -- the two TREASURY_RUNGS bracketing `dur`, and the weight on
    the longer one, so that (1-w)*d_lo + w*d_hi == dur exactly. Below BIL or
    above TLT it CLAMPS to that end rung (lo == hi), because extrapolating a
    curve off its own ends is how a match invents a move that never happened."""
    rungs = sorted(TREASURY_RUNGS, key=lambda t: BOND_DURATION[t])
    if dur <= BOND_DURATION[rungs[0]]:
        return rungs[0], rungs[0], 0.0
    if dur >= BOND_DURATION[rungs[-1]]:
        return rungs[-1], rungs[-1], 1.0
    for i in range(len(rungs) - 1):
        lo, hi = rungs[i], rungs[i + 1]
        d_lo, d_hi = BOND_DURATION[lo], BOND_DURATION[hi]
        if d_lo <= dur <= d_hi:
            return lo, hi, (dur - d_lo) / (d_hi - d_lo)
    return rungs[-1], rungs[-1], 1.0


def matched_treasury_series(series, dur):
    """A synthetic daily TOTAL-RETURN index for a Treasury position of `dur`
    years, built on the panel's common index from the two bracketing rungs:
    each session's return is (1-w)*r_lo + w*r_hi and the level is cumulated
    from 1.0 at the first session on which both rungs print.

    None on any session either rung is missing -- a matched return we cannot
    compute must never quietly become zero, because zero is a real answer here
    ("the curve did not move") and it would show up as excess return. Returns
    None outright when a bracketing rung has no series at all, which is the
    signal that the spread row simply cannot be computed yet."""
    lo, hi, w = _bracket_rungs(dur)
    c_lo, c_hi = series.get(lo), series.get(hi)
    if not c_lo or not c_hi:
        return None
    n = min(len(c_lo), len(c_hi))
    out = [None] * n
    v = None
    for i in range(n):
        b_lo, b_hi = c_lo[i], c_hi[i]
        if b_lo is None or b_hi is None:
            continue
        if v is None:
            v = 1.0          # base the index on the first session both rungs print
        else:
            a_lo = c_lo[i - 1]
            a_hi = c_hi[i - 1]
            if a_lo is not None and a_hi is not None and a_lo > 0 and a_hi > 0:
                v *= 1.0 + (1.0 - w) * (b_lo / a_lo - 1.0) + w * (b_hi / a_hi - 1.0)
        out[i] = v
    return out


def excess_series(etf, matched):
    """etf / matched, elementwise -- the EXCESS-RETURN index. Its LEVEL is an
    arbitrary base and means nothing; only its returns do, which is exactly and
    only what sector_ladder reads off a series. None wherever either side is."""
    if not etf or not matched:
        return None
    n = min(len(etf), len(matched))
    out = [None] * n
    for i in range(n):
        a, b = etf[i], matched[i]
        out[i] = None if (a is None or b is None or b == 0) else a / b
    return out


def match_label(dur):
    """The short "measured against what" label for a spread row. Inside the
    bracket it names the interpolated point; within 10% of either end it names
    the rung itself, because "vs ~0.2y UST (BIL/SHY)" is a worse description of
    BKLN (w = 0.083) than the true one, "vs BIL"."""
    lo, hi, w = _bracket_rungs(dur)
    if lo == hi or w <= 0.10:
        return "vs " + lo
    if w >= 0.90:
        return "vs " + hi
    return "vs ~%.1fy UST (%s/%s)" % (dur, lo, hi)


def bond_curve_rows(tr_rows, rnd):
    """The five Treasury rungs in MATURITY order. `tr_rows` is {ticker: row}
    from a plain sector_ladder over the RAW series, so r5..blend here are total
    returns. dyN = -rN / dur, in basis points: the duration-1 inversion of that
    total return into "how far did the yield at this point move". It is a PROXY
    -- it drops carry and convexity -- so it is read for the SHAPE of the move
    across the strip, never as a yield print.

    Below DY_MIN_DUR the inversion is NOT EMITTED AT ALL (None). At BIL's 0.1y
    a bond's total return is essentially all carry and none of it is price, so
    dividing by the duration does not recover a yield move, it multiplies the
    coupon by a hundred: BIL's +0.90% over 63d came out as -901bp, which is not
    a 9pp rally in bill yields, it is three months of T-bill interest. A number
    that wrong is worse than a blank, and the curve read never used it -- it
    reads SHY and TLT for the slope and IEF for the level."""
    rows = []
    for t in TREASURY_RUNGS:
        r = tr_rows.get(t)
        if not r:
            continue
        d = BOND_DURATION[t]

        def _dy(v, _d=d):
            if v is None or _d < DY_MIN_DUR:
                return None
            return round(-v / _d * 10000.0)

        rows.append({
            "t": t, "name": r["name"], "dur": d,
            "r5": rnd(r.get("r5")), "r21": rnd(r["r21"]), "r63": rnd(r["r63"]),
            "r126": rnd(r["r126"]), "blend": rnd(r["blend"]),
            "dy21": _dy(r["r21"]), "dy63": _dy(r["r63"]), "dy126": _dy(r["r126"]),
        })
    return rows


def bond_curve_read(rows):
    """duration / curve / shape / gap_bp off the curve strip.

    `duration` is IEF's own 63d total return (the belly is the cleanest single
    read on "did rates fall"). `curve` is the 63d yield-proxy move at TLT minus
    the one at SHY: the long end selling off relative to the short end is a
    steepening whichever way the level went. `shape` is the two crossed, in the
    market's own vocabulary, and collapses to "flat" the moment either leg is
    inside its dead zone -- half a signal is not a shape."""
    by = {r["t"]: r for r in rows}
    ief = (by.get("IEF") or {}).get("r63")
    if ief is None or abs(ief) <= DURATION_BAND:
        duration = "flat"
    else:
        duration = "rallying" if ief > 0 else "selling off"
    lo = (by.get("SHY") or {}).get("dy63")
    hi = (by.get("TLT") or {}).get("dy63")
    gap = None if (lo is None or hi is None) else hi - lo
    if gap is None or abs(gap) <= CURVE_BAND_BP:
        curve = "flat"
    else:
        curve = "steepening" if gap > 0 else "flattening"
    if duration == "flat" or curve == "flat":
        shape = "flat"
    else:
        shape = ("bull " if duration == "rallying" else "bear ") + \
                ("steepener" if curve == "steepening" else "flattener")
    return {"duration": duration, "curve": curve, "shape": shape, "gap_bp": gap}


def bond_ladder_read(rows, curve_read):
    """credit / duration / quadrant off the nine EXCESS rows plus the curve.

    `credit` is the MEDIAN 63d excess return of the four spread sleeves, not
    HYG alone: one sleeve can be carried by its own index mechanics, four
    agreeing is a credit call. `duration` is COPIED from the curve read so the
    two panels can never disagree with each other. The quadrant is the two
    crossed -- the four corners are the four macro states a bond market can be
    in -- and "mixed" is the honest answer whenever either axis sits in its
    dead zone, rather than the nearest corner."""
    by = {r["t"]: r for r in rows}
    med = median([by[t]["r63"] for t in CREDIT_SLEEVES if t in by])
    if med is None or abs(med) <= CREDIT_BAND:
        credit = "flat"
    else:
        credit = "tightening" if med > 0 else "widening"
    duration = curve_read["duration"]
    if credit == "flat" or duration == "flat":
        quadrant = "mixed"
    elif credit == "tightening":
        quadrant = "goldilocks / disinflation" if duration == "rallying" else "reflation"
    else:
        quadrant = "growth scare" if duration == "rallying" else "inflation shock (2022 shape)"
    return {"credit": credit, "duration": duration, "quadrant": quadrant}


def _field_spread(rows, key, rnd):
    """Best minus worst across the field, in PERCENTAGE POINTS -- the field's
    dispersion. On `blend` (the sort key) that is literally the top row minus
    the bottom row; on r63 it is the max minus the min, the same statistic but
    not necessarily the same two rows. None until two rows can supply it."""
    vals = [r[key] for r in rows if r.get(key) is not None]
    if len(vals) < 2:
        return None
    return rnd((max(vals) - min(vals)) * 100.0, 4)


def bond_field(series, rnd):
    """(bond_ladder, bond_curve) -- the whole #91 object. The ranked field is
    the NINE non-Treasury rows on EXCESS return over their duration-matched
    Treasury; the five rungs become the curve strip beside it. `rnd` is
    build_regime's rounding shim."""
    # Total returns for all fourteen: the curve strip's own levels, and the tr*
    # columns carried alongside each spread row's excess ones.
    tr = {r["t"]: r for r in sector_ladder(series, REG_BONDS)["rows"]}
    curve_rows = bond_curve_rows(tr, rnd)
    curve_read = bond_curve_read(curve_rows)

    spread_series = {}
    for e in REG_BOND_SPREAD:
        raw = series.get(e["t"])
        if not raw:
            continue
        ex = excess_series(raw, matched_treasury_series(series, BOND_DURATION[e["t"]]))
        # A row with no computable excess anywhere is ABSENT, never a row of
        # Nones -- the same rule the ladder already applies to a missing series.
        if ex and any(v is not None for v in ex):
            spread_series[e["t"]] = ex

    lad = sector_ladder(spread_series, REG_BOND_SPREAD)
    out = ladder_payload(lad, rnd, with_field=True)
    for row in out["rows"]:
        d = BOND_DURATION[row["t"]]
        trr = tr.get(row["t"]) or {}
        row["tr21"] = rnd(trr.get("r21"))
        row["tr63"] = rnd(trr.get("r63"))
        row["tr126"] = rnd(trr.get("r126"))
        row["trblend"] = rnd(trr.get("blend"))
        row["dur"] = d
        row["match"] = match_label(d)
    out["read"] = bond_ladder_read(out["rows"], curve_read)
    out["spread63"] = _field_spread(out["rows"], "r63", rnd)
    out["spread_blend"] = _field_spread(out["rows"], "blend", rnd)
    return out, {"rows": curve_rows, "read": curve_read}


# The composite legs. v52 carried the three macro + two equity trend-rule legs
# verbatim from renderRegimePanel; v60 adds the three ORTHOGONAL-MECHANISM legs
# (stocks-bonds correlation, SPY realised vol, book dispersion) -- the
# framework-review finding was that six EMA-slope rules on correlated risk
# proxies are one measurement in six coats. Macro legs activate the moment
# their tickers are present. Order is display/parity order.
LEG_RATIOS = [
    # (label, key, num_candidates, den_candidates, macro)
    ("Credit — HY vs IG", "credit_hy_ig", ["HYG"], ["LQD"], True),
    ("Copper / gold", "copper_gold", ["CPER"], ["GLD"], True),
]
LEG_TRENDS = [
    # (label, key, ticker_candidates, invert, macro)
    ("Dollar (inverse)", "dollar_inv", ["UUP"], True, True),
]
LEG_CORR = [
    # (label, key, a_candidates, b_candidates, macro) — rolling 63d return corr,
    # vote by SIGN with a ±0.20 dead zone (negative = bonds hedge stocks = risk-supportive)
    ("Stocks–bonds corr", "stocks_bonds_corr", ["SPY"], ["TLT"], True),
]
LEG_RATIOS_EQUITY = [
    ("Offense vs defense", "offense_defense", ["SMH", "SOXX"], ["GDX", "GDXJ", "RING"], False),
    ("Beta appetite", "beta_appetite", ["QQQ", "IWM", "SOXX"], ["SPY"], False),
]
LEG_VOL = [
    # (label, key, ticker_candidates, macro) — rv21 percentile vs own trailing
    # year, +1 in the calmest 30%, −1 in the most stressed 30%
    ("Volatility (SPY)", "spy_vol", ["SPY"], False),
]
# Dispersion (avg pairwise 63d corr across the coverage pool, percentile vs own
# trailing year, +1 dispersed / −1 crowded) is built from COVERAGE_POOL below.

# THE COVERAGE POOL (backlog #2): the breadth / base-rate pool is an EXPLICIT,
# hand-maintained list of the single names this project covers -- NOT
# "everything in the price file that isn't a sector ETF", which would let a
# newly added sled or basket constituent silently change every breadth reading.
# The order is historical and load-bearing only for the parity fixtures.
# Sector-ETF proxies, crypto, futures and indices are filtered out below so this
# stays single-name equity breadth.
COVERAGE_POOL = [
    "COHR", "HIMX", "SNDK", "IREN", "AXTI", "INTC", "EWY", "ALAB", "AMSC",
    "NOK", "NVDA", "RING", "GDXJ", "ICOP", "ASML", "TSM", "NVO", "PENG",
    "JPM", "QQQ", "IGV", "MARS", "AOSL",
    "INVH", "LRCX", "EQIX", "BE", "NBIS", "AAOI", "MPWR", "RDDT", "LITE",
    "AEHR", "MRVL", "MU", "HUBS", "NOW", "SHOP", "DDOG", "PANW", "CRWD", "BTC-USD",
]

_ETF_SET = {e["t"] for e in REG_ETFS}
import re as _re
_NONEQUITY = _re.compile(r"(-USD|=F)$")


def _is_book_equity(t, series):
    """A breadth-pool member: present in the panel, not a sector-ETF proxy, not
    crypto/futures/index. Mirrors the renderRegimePanel `book` filter."""
    return (series.get(t) is not None
            and t not in _ETF_SET
            and not _NONEQUITY.search(t)
            and not t.startswith("^"))


def _first_present(series, candidates):
    for t in candidates:
        if series.get(t):
            return t
    return None


# ==========================================================================
# panel construction  (the #3 fix: equity trading-day axis)
# ==========================================================================

def reference_dates(conn, ref_ticker="SPY"):
    """The equity trading-day axis: `ref_ticker`'s bar dates, or the union of
    non-crypto/non-futures trading days if the reference has no bars."""
    ref = conn.execute(
        "SELECT date FROM daily_prices WHERE ticker=? AND close IS NOT NULL ORDER BY date",
        (ref_ticker,),
    ).fetchall()
    dates = [r[0] for r in ref]
    if dates:
        return dates
    rows = conn.execute(
        "SELECT DISTINCT date FROM daily_prices "
        "WHERE ticker NOT LIKE '%-USD' AND ticker NOT LIKE '%=F' ORDER BY date"
    ).fetchall()
    return [r[0] for r in rows]


def sessions_behind(axis, last_date):
    """How many reference sessions have closed since `last_date`. 0 = current.
    `axis` is ascending; counted in SESSIONS, never calendar days, so a normal
    weekend is 0 behind and a delisting is unmistakable."""
    if not axis or not last_date:
        return 0
    lo, hi = 0, len(axis)
    while lo < hi:                      # bisect_right without the import
        mid = (lo + hi) // 2
        if axis[mid] <= last_date:
            lo = mid + 1
        else:
            hi = mid
    return len(axis) - lo


def build_panel(conn, ref_ticker="SPY", max_stale=MAX_STALE_SESSIONS):
    """Build the cross-sectional panel on the EQUITY trading-day axis.

    Axis = `ref_ticker` (SPY) trading days. Every ticker is as-of aligned onto
    it: for each SPY session, the ticker's most recent close on or before that
    session (forward-fill), with leading None until the ticker's first bar.
    Equities align natively; crypto/futures collapse to their latest close per
    SPY session (weekends dropped). Adjusted close drives the series (matching
    RC.parseCsv's adj_close preference and technicals.py's return maths).

    STALENESS CUTOFF (2026-09-13). The forward-fill used to run to the end of
    the axis, so a name that stopped printing bars became a FLAT LINE that the
    ladder then ranked on its merits: PBS (Media) had no bar after 2026-07-17
    and sat in the top third on 21d with r5 = r21 = 0.0, because a dead ticker
    never goes down. fetch_prices logged "[FAIL] PBS no daily bars returned"
    every day and exited 0 the whole time (it only fails on a total wipeout), so
    eight weeks of silence reached the board as a signal.

    Now the fill stops `max_stale` sessions past a ticker's last REAL bar and
    the series goes None. Everything downstream already handles None correctly:
    `ret` returns None, `third_at` returns None, the streak walk stops, and
    `breadth_series` drops the name from its denominator. Set max_stale=None to
    restore the old unbounded fill.
    """
    dates = reference_dates(conn, ref_ticker)

    series = {}
    syms = [r[0] for r in conn.execute(
        "SELECT DISTINCT ticker FROM daily_prices ORDER BY ticker")]
    for sym in syms:
        rows = conn.execute(
            "SELECT date, adj_close, close FROM daily_prices "
            "WHERE ticker=? AND close IS NOT NULL ORDER BY date",
            (sym,),
        ).fetchall()
        # The TRAILING edge only. An interior gap (a foreign market closed for a
        # holiday week) is still forward-filled, which is correct -- the price
        # did not move because the venue was shut. What is never correct is
        # filling past the end of a name's life.
        last_real = rows[-1][0] if rows else None
        last_idx = len(dates) - 1
        if max_stale is not None and last_real is not None:
            last_idx = (len(dates) - sessions_behind(dates, last_real) - 1) + max_stale

        arr = [None] * len(dates)
        ri = 0
        prev = None
        started = False
        for i, d in enumerate(dates):
            while ri < len(rows) and rows[ri][0] <= d:
                v = rows[ri][1] if rows[ri][1] is not None else rows[ri][2]
                if v is not None:
                    prev = v
                    started = True
                ri += 1
            arr[i] = prev if (started and i <= last_idx) else None
        series[sym] = arr
    return dates, series


# ==========================================================================
# emit the `regime` block for technicals.json
# ==========================================================================

def build_regime(conn, emitted_tickers, ref_ticker="SPY", panel=None, round_floats=True):
    """Assemble the top-level `regime` object for technicals.json. Emitted
    ALONGSIDE the per-ticker records (Phase 1) -- the client still computes its
    own copy; this lets prod JSON be diffed against the live client. Float
    fields are rounded to 6dp for JSON compactness (round_floats=True); the port
    functions above are exact and are what the parity harness checks. Pass
    `panel=(dates, series)` to reuse a prebuilt axis+panel (used by the
    end-to-end oracle parity test); `round_floats=False` keeps values exact so
    they compare against the un-rounded oracle within 1e-9."""
    if round_floats:
        def _r(v, d=6):
            return None if v is None else round(v, d)
    else:
        def _r(v, d=6):
            return v

    dates, series = panel if panel is not None else build_panel(conn, ref_ticker)
    if not dates:
        return None

    legs = []
    leg_info = []

    def add_ratio(label, key, num_list, den_list, macro):
        nm = _first_present(series, num_list)
        dn = _first_present(series, den_list)
        if not nm or not dn:
            return
        cont = ratio(series[nm], series[dn])
        s = ratio_leg_series(cont)
        legs.append(s)
        leg_info.append({"key": key, "label": label, "type": "ratio",
                         "val": nm + "÷" + dn, "series": s, "last": s[-1],
                         "cont": cont, "macro": bool(macro), "voting": True,
                         "first_active": leg_first_active(cont)})

    def add_trend(label, key, tick_list, invert, macro):
        t = _first_present(series, tick_list)
        if not t:
            return
        s = trend_leg_series(series[t], invert)
        legs.append(s)
        leg_info.append({"key": key, "label": label, "type": "trend",
                         "val": ("↓" if invert else "↑") + t, "series": s,
                         "last": s[-1], "cont": series[t], "macro": bool(macro),
                         "voting": True,
                         "first_active": leg_first_active(series[t])})

    def add_corr(label, key, a_list, b_list, macro):
        # v62: DISPLAY-ONLY (voting=False). The stocks-bonds correlation
        # classifies the TAPE TYPE (growth- vs inflation-driven), not risk
        # appetite: over 10y it voted +1 on 42% of days SPY sat >=5% below its
        # 63d high (bonds hedging equity stress is not risk-on), and its seat
        # in the denominator alone raised the defensive entry bar from -3/6 to
        # -4/9 -- which is exactly why the composite never printed defensive
        # through the March 2026 -8.6% drawdown. It stays on the panel as a
        # tape-type read; it is NOT in the vote sum or the denominator.
        ta = _first_present(series, a_list)
        tb = _first_present(series, b_list)
        if not ta or not tb:
            return
        cont = corr_pair_series(series[ta], series[tb], 63)
        s = corr_vote_series(cont, 0.20)
        leg_info.append({"key": key, "label": label, "type": "corr",
                         "val": ta + "↔" + tb + " 63d", "series": s,
                         "last": s[-1], "cont": cont, "macro": bool(macro),
                         "voting": False,
                         "first_active": first_non_null(cont)})

    def add_vol(label, key, tick_list, macro):
        t = _first_present(series, tick_list)
        if not t:
            return
        rv = rv_series(series[t], 21)
        pct = pct_rank_series(rv, 252)
        s = pct_vote_series(pct, 0.30, 0.70)
        legs.append(s)
        pl = pct[-1] if pct else None
        leg_info.append({"key": key, "label": label, "type": "vol",
                         "val": t + " rv21" + ("" if pl is None else " p" + str(int(math.floor(pl * 100 + 0.5)))),
                         "series": s, "last": s[-1], "cont": rv, "macro": bool(macro),
                         "voting": True,
                         "first_active": first_non_null(pct)})

    def add_disp(label, key, names):
        ac = avg_corr_series(series, names, 63, 8)
        if first_non_null(ac) >= len(ac):
            return
        pct = pct_rank_series(ac, 252)
        s = pct_vote_series(pct, 0.30, 0.70)
        legs.append(s)
        pl = pct[-1] if pct else None
        leg_info.append({"key": key, "label": label, "type": "disp",
                         "val": "avg corr" + ("" if pl is None else " p" + str(int(math.floor(pl * 100 + 0.5)))),
                         "series": s, "last": s[-1], "cont": ac, "macro": False,
                         "voting": True,
                         "first_active": first_non_null(pct)})

    # ORDER is display/parity order: macro ratios, the dollar trend, the
    # stocks-bonds correlation, the equity ratios, SPY vol, breadth, book
    # dispersion. The emit oracle replicates this exactly.
    for label, key, num, den, macro in LEG_RATIOS:
        add_ratio(label, key, num, den, macro)
    for label, key, tick, invert, macro in LEG_TRENDS:
        add_trend(label, key, tick, invert, macro)
    for label, key, a, b, macro in LEG_CORR:
        add_corr(label, key, a, b, macro)
    for label, key, num, den, macro in LEG_RATIOS_EQUITY:
        add_ratio(label, key, num, den, macro)
    for label, key, tick, macro in LEG_VOL:
        add_vol(label, key, tick, macro)

    book = [t for t in COVERAGE_POOL if _is_book_equity(t, series)]
    frac = breadth_series(series, book) if book else None
    breadth_leg = None
    if frac is not None:
        breadth_leg = breadth_leg_series(frac)
        legs.append(breadth_leg)
        leg_info.append({"key": "breadth_book_200d", "label": "Breadth (book >200d)",
                         "type": "breadth", "series": breadth_leg,
                         "last": breadth_leg[-1], "cont": frac, "macro": False,
                         "voting": True,
                         "first_active": first_non_null(frac)})
    if book:
        add_disp("Book dispersion", "book_dispersion", book)

    if not legs:
        return {"dates": dates, "asof": dates[-1] if dates else None,
                "composite": None, "legs": [], "breadth": None,
                "ladder": None, "receipts": {}, "base_rates": None,
                "note": "no inputs available"}

    fas = [x["first_active"] for x in leg_info if x["voting"]]
    comp = composite_series(legs, fas)
    active = len(legs)  # VOTING legs only; display-only legs are not in `legs`
    macro_count = sum(1 for x in leg_info if x.get("macro") and x.get("voting"))
    last = len(comp["sum"]) - 1
    k_last = comp["k"][last] if last >= 0 else 0
    net_last = comp["sum"][last] / k_last if k_last else None
    dur = durations(comp["state"])

    # receipts pool = the emitted (>=MIN_BARS) technicals tickers minus sector
    # ETFs -- mirrors the client's `TECH.tickers` filter (NOT the book set).
    rec_pool = [t for t in emitted_tickers if series.get(t) and t not in _ETF_SET]
    rec_pool.sort()
    receipts_full = rank_receipts(series, rec_pool)
    receipts = {}
    for t, rec in receipts_full.items():
        receipts[t] = {
            "rank": _r(rec["rank"]),
            "tier": rec["tier"],
            "streak": rec["streak"],
            "sinceRet": _r(rec["sinceRet"]),
            "entryIdx": rec["entryIdx"],
        }

    lad = sector_ladder(series, REG_ETFS)
    ladder_out = ladder_payload(lad, _r)
    # The two 9/15/26 sibling ladders: same function, same output shape, their
    # own fields. A member with no series yet is simply ABSENT from `rows`
    # (sector_ladder's `present` filter), never a row of Nones -- so a row
    # showing up is itself the signal that the backfill landed.
    style_out = ladder_payload(sector_ladder(series, REG_STYLE), _r, with_field=True)
    # The bond field is NOT a plain ladder any more (#91, 9/15/26): the five
    # Treasury rungs come out into `bond_curve` and the other nine are ranked on
    # EXCESS return over a duration-matched Treasury. Same payload shape, plus
    # tr*/dur/match per row and a `read`; bond_field() above says why.
    bond_out, bond_curve_out = bond_field(series, _r)

    brm = base_rates_multi(series, book, comp["state"], (5, 21, 63))
    base_rates_out = {}
    for hk, states in brm.items():
        base_rates_out[hk] = {s: {"n": b["n"], "n_eff": b["n_eff"],
                                  "median": _r(b["median"], 9), "hit": _r(b["hit"], 6)}
                              for s, b in states.items()}

    legs_out = [{
        "key": x["key"], "label": x["label"], "type": x["type"],
        "val": x.get("val"), "macro": x.get("macro", False),
        "voting": x.get("voting", True),
        "series": x["series"], "last": x["last"],
        "first_active": x["first_active"],
        "cont": [_r(v) for v in x["cont"]],  # continuous underlying, for the per-metric sparklines
    } for x in leg_info]

    return {
        "dates": dates,
        "asof": dates[-1],
        "axis": "equity-trading-day (SPY)",
        "mode": "cross-asset" if macro_count >= 2 else "equity-internal",
        "composite": {
            "sum": comp["sum"],
            "state": comp["state"],
            "k": comp["k"],
            "net_last": _r(net_last),
            "state_last": comp["state"][last],
            "score_last": comp["sum"][last],
            "active_legs": active,
            "k_last": k_last,
            "macro_legs": macro_count,
            "hysteresis": {"enter": 0.34, "exit": 0.17},
        },
        "durations": dur,
        "legs": legs_out,
        "breadth": None if frac is None else {
            "frac": [_r(f) for f in frac],
            "leg": breadth_leg,
            "pool_size": len(book),
            "pool": "BOOK",
            "pool_tickers": book,
        },
        "ladder": ladder_out,
        "style_ladder": style_out,
        "bond_ladder": bond_out,
        "bond_curve": bond_curve_out,
        "receipts": receipts,
        "base_rates": base_rates_out,
        "flips": flips(dates, comp["state"]),
    }
