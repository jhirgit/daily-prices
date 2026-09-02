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
    {"t","name","side"[,"grp"]}. Port of RC.sectorLadder."""
    present = [e for e in etfs if series.get(e["t"])]
    seen_grp = {}
    field_set = {}
    twin_of = [None] * len(present)
    for k in range(len(present)):
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
        rows.append({"t": e["t"], "name": e["name"], "side": e.get("side"),
                     "r5": r5, "r21": r21, "r63": r63, "r126": r126, "blend": blend,
                     "twin_of": twin_of[k]})
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


# ==========================================================================
# EMITTER config  (moved out of index.html renderRegimePanel / REG_ETFS)
# ==========================================================================
# The sector-rotation ladder + offense/defense divergence field. Verbatim from
# index.html `REG_ETFS` (v52). `side` drives the divergence tell.
REG_ETFS = [
    # grp = sleeve group for the v60 dedup: only the first-listed present
    # member of a grp joins the thirds/streak/divergence field; later members
    # are display-only twins. SLV (the metal) and SILJ (jr miners) are
    # deliberately NOT grouped -- metal vs miner is an economic distinction
    # (today's SLV-vs-SILJ 63d gap is exactly that), unlike the true
    # near-duplicates SMH/SOXX and GDX/GDXJ/RING.
    {"t": "SMH", "name": "Semis", "side": "offense", "grp": "semis"},
    {"t": "SOXX", "name": "Semis (SOXX)", "side": "offense", "grp": "semis"},
    {"t": "QQQ", "name": "Nasdaq 100", "side": "offense"},
    {"t": "IWM", "name": "Small caps", "side": "offense"},
    {"t": "IGV", "name": "Software", "side": "offense"},
    # Cybersecurity as its OWN sled, deliberately NOT a `grp` twin of IGV. The
    # grp mechanism is for near-duplicates (SMH/SOXX, GDX/GDXJ/RING); cybersec
    # vs general software is an economic distinction, the same call already made
    # for SLV vs SILJ. Added 8/27/26, when IGV +7.8% could not be told apart
    # from CIBR +7.7% by an engine carrying only one software row -- and the
    # single-name prints under it (CRWD +20, OKTA +29, PANW +14) were the whole
    # story. NOTE: adding a row re-thirds the WHOLE field, so streaks on every
    # other sled reset relative to the pre-8/27 series. That is intended.
    {"t": "CIBR", "name": "Cybersecurity", "side": "offense"},
    {"t": "ARTY", "name": "AI basket", "side": "offense"},
    {"t": "DTCR", "name": "Data centers", "side": "offense"},
    {"t": "DRAM", "name": "Memory", "side": "offense"},
    {"t": "GRID", "name": "Grid infra", "side": "offense"},
    {"t": "SPY", "name": "S&P 500", "side": None},
    {"t": "EWY", "name": "Korea", "side": None},
    {"t": "ICOP", "name": "Copper miners", "side": None},
    {"t": "GDX", "name": "Gold miners", "side": "defense", "grp": "gold"},
    {"t": "GDXJ", "name": "Jr gold miners", "side": "defense", "grp": "gold"},
    {"t": "RING", "name": "Gold miners (RING)", "side": "defense", "grp": "gold"},
    {"t": "SILJ", "name": "Jr silver miners", "side": "defense"},
    {"t": "SLV", "name": "Silver", "side": "defense"},
]

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
# Book dispersion (avg pairwise 63d corr of the covered book, percentile vs own
# trailing year, +1 dispersed / −1 crowded) is built from BOOK_UNION below.

# The COVERED EQUITY BOOK (backlog #2): the breadth / base-rate pool is the
# union of the Book tab's HELD, REST and FLAT arrays -- an explicit book set,
# NOT "everything in the price file that isn't a sector ETF". Verbatim ticker
# union from index.html (v52). Sector-ETF proxies, crypto, futures and indices
# are filtered out below so this stays single-name equity breadth.
BOOK_UNION = [
    # HELD
    "COHR", "HIMX", "SNDK", "IREN", "AXTI", "INTC", "EWY", "ALAB", "AMSC",
    "NOK", "NVDA", "RING", "GDXJ", "ICOP", "ASML", "TSM", "NVO", "PENG",
    # REST
    "JPM", "QQQ", "IGV", "MARS", "AOSL",
    # FLAT
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

def build_panel(conn, ref_ticker="SPY"):
    """Build the cross-sectional panel on the EQUITY trading-day axis.

    Axis = `ref_ticker` (SPY) trading days. Every ticker is as-of aligned onto
    it: for each SPY session, the ticker's most recent close on or before that
    session (forward-fill), with leading None until the ticker's first bar.
    Equities align natively; crypto/futures collapse to their latest close per
    SPY session (weekends dropped). Adjusted close drives the series (matching
    RC.parseCsv's adj_close preference and technicals.py's return maths).
    """
    ref = conn.execute(
        "SELECT date FROM daily_prices WHERE ticker=? AND close IS NOT NULL ORDER BY date",
        (ref_ticker,),
    ).fetchall()
    dates = [r[0] for r in ref]
    if not dates:
        # Fallback: union of equity (non-crypto/futures) trading days.
        rows = conn.execute(
            "SELECT DISTINCT date FROM daily_prices "
            "WHERE ticker NOT LIKE '%-USD' AND ticker NOT LIKE '%=F' ORDER BY date"
        ).fetchall()
        dates = [r[0] for r in rows]

    series = {}
    syms = [r[0] for r in conn.execute(
        "SELECT DISTINCT ticker FROM daily_prices ORDER BY ticker")]
    for sym in syms:
        rows = conn.execute(
            "SELECT date, adj_close, close FROM daily_prices "
            "WHERE ticker=? AND close IS NOT NULL ORDER BY date",
            (sym,),
        ).fetchall()
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
            arr[i] = prev if started else None
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

    book = [t for t in BOOK_UNION if _is_book_equity(t, series)]
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
    ladder_rows = [{
        "t": r["t"], "name": r["name"], "side": r["side"],
        "r5": _r(r.get("r5")), "r21": _r(r["r21"]), "r63": _r(r["r63"]), "r126": _r(r["r126"]),
        "blend": _r(r["blend"]), "third": r["third"], "third21": r["third21"],
        "streak": r["streak"], "twin_of": r["twin_of"],
    } for r in lad["rows"]]

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
        "ladder": {"rows": ladder_rows, "divergence": lad["divergence"]},
        "receipts": receipts,
        "base_rates": base_rates_out,
        "flips": flips(dates, comp["state"]),
    }
