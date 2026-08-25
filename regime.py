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

def composite_series(legs):
    """Net the legs to a state: risk-on at net>=+0.34, defensive at <=-0.34,
    else neutral (net = raw sum / #legs). `sum` is the un-normalised total. Port
    of RC.compositeSeries."""
    n = len(legs[0])
    k = len(legs) or 1
    thr = 0.34
    total = [0] * n
    state = [None] * n
    for i in range(n):
        s = 0
        for j in range(len(legs)):
            s += legs[j][i]
        total[i] = s
        net = s / k
        state[i] = "risk-on" if net >= thr else ("defensive" if net <= -thr else "neutral")
    return {"sum": total, "state": state}


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
    third). `etfs` is a list of {"t","name","side"}. Port of RC.sectorLadder."""
    present = [e for e in etfs if series.get(e["t"])]
    rows = []
    for e in present:
        c = series[e["t"]]
        r63 = ret(c, 63)
        r126 = ret(c, 126)
        if r63 is not None and r126 is not None:
            blend = (r63 + r126) / 2.0
        else:
            blend = r63 if r63 is not None else r126
        rows.append({"t": e["t"], "name": e["name"], "side": e.get("side"),
                     "r63": r63, "r126": r126, "blend": blend})
    n = len(series[present[0]["t"]]) if present else 0

    r63ser = []
    for e in present:
        c = series[e["t"]]
        s = [None] * len(c)
        for i in range(len(c)):
            a = c[i - 63] if i >= 63 else None
            b = c[i]
            s[i] = (b / a - 1) if (a is not None and b is not None and a > 0) else None
        r63ser.append(s)

    def third_at(k, i):
        mine = r63ser[k][i]
        if mine is None:
            return None
        vals = []
        for q in range(len(present)):
            v = r63ser[q][i]
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
        t_third = third_at(k, last) if last >= 0 else None
        rows[k]["third"] = t_third
        rows[k]["streak"] = 0
        if t_third == "top" or t_third == "bottom":
            i = last
            s = 0
            while i >= 0 and third_at(k, i) == t_third:
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
    {"t": "SMH", "name": "Semis", "side": "offense"},
    {"t": "SOXX", "name": "Semis (SOXX)", "side": "offense"},
    {"t": "QQQ", "name": "Nasdaq 100", "side": "offense"},
    {"t": "IWM", "name": "Small caps", "side": "offense"},
    {"t": "IGV", "name": "Software", "side": "offense"},
    {"t": "ARTY", "name": "AI basket", "side": "offense"},
    {"t": "SPY", "name": "S&P 500", "side": None},
    {"t": "EWY", "name": "Korea", "side": None},
    {"t": "ICOP", "name": "Copper miners", "side": None},
    {"t": "GDX", "name": "Gold miners", "side": "defense"},
    {"t": "GDXJ", "name": "Jr gold miners", "side": "defense"},
    {"t": "RING", "name": "Gold miners (RING)", "side": "defense"},
    {"t": "SILJ", "name": "Jr silver miners", "side": "defense"},
    {"t": "SLV", "name": "Silver", "side": "defense"},
]

# The composite legs, as (label, key, kind, ...). Verbatim from the addRatio /
# addTrend calls in renderRegimePanel (v52). Macro legs activate the moment
# their tickers are present. Order preserved so the composite matches the client.
LEG_RATIOS = [
    # (label, key, num_candidates, den_candidates, macro)
    ("Credit — HY vs IG", "credit_hy_ig", ["HYG"], ["LQD"], True),
    ("Copper / gold", "copper_gold", ["CPER"], ["GLD"], True),
]
LEG_TRENDS = [
    # (label, key, ticker_candidates, invert, macro)
    ("Dollar (inverse)", "dollar_inv", ["UUP"], True, True),
]
LEG_RATIOS_EQUITY = [
    ("Offense vs defense", "offense_defense", ["SMH", "SOXX"], ["GDX", "GDXJ", "RING"], False),
    ("Beta appetite", "beta_appetite", ["QQQ", "IWM", "SOXX"], ["SPY"], False),
]

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
                         "macro": bool(macro)})

    def add_trend(label, key, tick_list, invert, macro):
        t = _first_present(series, tick_list)
        if not t:
            return
        s = trend_leg_series(series[t], invert)
        legs.append(s)
        leg_info.append({"key": key, "label": label, "type": "trend",
                         "val": ("↓" if invert else "↑") + t, "series": s,
                         "last": s[-1], "macro": bool(macro)})

    # ORDER matters for parity with the client: macro ratios, then the trend,
    # then the equity ratios, then breadth (exactly the addRatio/addTrend order
    # in renderRegimePanel).
    for label, key, num, den, macro in LEG_RATIOS:
        add_ratio(label, key, num, den, macro)
    for label, key, tick, invert, macro in LEG_TRENDS:
        add_trend(label, key, tick, invert, macro)
    for label, key, num, den, macro in LEG_RATIOS_EQUITY:
        add_ratio(label, key, num, den, macro)

    book = [t for t in BOOK_UNION if _is_book_equity(t, series)]
    frac = breadth_series(series, book) if book else None
    breadth_leg = None
    if frac is not None:
        breadth_leg = breadth_leg_series(frac)
        legs.append(breadth_leg)
        leg_info.append({"key": "breadth_book_200d", "label": "Breadth (book >200d)",
                         "type": "breadth", "series": breadth_leg,
                         "last": breadth_leg[-1], "macro": False})

    if not legs:
        return {"dates": dates, "asof": dates[-1] if dates else None,
                "composite": None, "legs": [], "breadth": None,
                "ladder": None, "receipts": {}, "base_rates": None,
                "note": "no inputs available"}

    comp = composite_series(legs)
    active = len(legs)
    macro_count = sum(1 for x in leg_info if x.get("macro"))
    last = len(comp["sum"]) - 1
    net_last = comp["sum"][last] / active if active else None

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
        "r63": _r(r["r63"]), "r126": _r(r["r126"]), "blend": _r(r["blend"]),
        "third": r["third"], "streak": r["streak"],
    } for r in lad["rows"]]

    br = base_rates(series, book, comp["state"], 21)
    base_rates_out = {s: {"n": br[s]["n"], "median": _r(br[s]["median"], 9),
                          "hit": _r(br[s]["hit"], 6)} for s in br}

    legs_out = [{
        "key": x["key"], "label": x["label"], "type": x["type"],
        "val": x.get("val"), "macro": x.get("macro", False),
        "series": x["series"], "last": x["last"],
    } for x in leg_info]

    return {
        "dates": dates,
        "asof": dates[-1],
        "axis": "equity-trading-day (SPY)",
        "mode": "cross-asset" if macro_count >= 2 else "equity-internal",
        "composite": {
            "sum": comp["sum"],
            "state": comp["state"],
            "net_last": _r(net_last),
            "state_last": comp["state"][last],
            "score_last": comp["sum"][last],
            "active_legs": active,
            "macro_legs": macro_count,
        },
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
