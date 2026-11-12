#!/usr/bin/env python3
"""
How the sector-ladder sleeves move TOGETHER, written to data/rotation_corr.json.

    python rotation_corr.py            # prices.db only; no network

Jake, 2026-11-12: "i would like to be able to see the covariance and correlation
of these sleeves over time to each other and benchmarks ... in the expanded line
a list of things with high and low covariance and correlation ... a covariance
matrix somewhere ... also the power of the covariance and correlation over time".

WHAT IS MEASURED. Daily simple returns of every ladder row, on the ladder's own
series (``regime.build_panel`` on the SPY session axis; a basket row is
``regime.basket_series``, exactly what the ladder ranks), plus a short list of
benchmarks that are not ladder rows (BENCH).

  corr[W]   Pearson correlation of daily returns over the last W sessions, for
            every pair, on the days BOTH have a return. A pair with fewer than
            MIN_SHARE of the window in common is null, never a number.
  vol[W]    each series' own annualised volatility over the same window
            (stdev of daily returns x sqrt(252)).
            Annualised covariance is corr x vol_i x vol_j; the board multiplies
            it out, so the matrix is carried once, not twice.
  beta, r2  against each benchmark over W: beta = corr x vol_i / vol_bench,
            r2 = corr squared -- the share of the sleeve's daily variance the
            benchmark accounts for. This is the "power" of the relationship.
  roll      the same 63-session correlation looked at every STEP sessions back
            over HIST sessions: each sleeve against the rolled benchmarks and
            against the rest of the field (the mean of its correlations with
            every other non-twin sleeve); and for the field as a whole the mean
            pairwise correlation and the share of total variance carried by the
            first principal component of the correlation matrix (how much of
            the field moves as one).

DISPLAY ONLY. Nothing here is a ranking, a signal or a forecast, and nothing on
the board reads it into one. Correlation of daily returns describes the last W
sessions and says nothing about the next.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import regime as RG

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
DB_PATH = os.path.join(HERE, "prices.db")
OUT_PATH = os.path.join(DATA_DIR, "rotation_corr.json")

WINDOWS = (63, 252)        # sessions: a quarter and a year
ROLL_W = 63                # the window behind every point of the history
HIST = 756                 # sessions of history looked back over (about 3 years)
STEP = 5                   # one point a week
MIN_SHARE = 0.8            # a pair needs this share of the window in common
ANN = 252.0
FIELD_SHARE = 0.9         # a history point needs this share of today's field present
TOP_N = 6                  # most / least correlated sleeves listed per row
# Benchmarks that are NOT ladder rows (SPY, QQQ and IWM are rows already and sit
# in the matrix). Each is used only if prices.db carries it.
BENCH = [("TLT", "Long Treasuries"), ("HYG", "High-yield credit"), ("GLD", "Gold"),
         ("UUP", "US dollar"), ("BTC-USD", "Bitcoin")]
# The benchmarks every sleeve's history is drawn against, and listed first in
# the expanded row. The first three are ladder rows.
ROLL_BENCH = ["SPY", "QQQ", "IWM", "TLT"]


def daily_returns(close):
    """Simple daily returns of one series; None where either bar is missing."""
    out = [None] * len(close)
    for i in range(1, len(close)):
        a, b = close[i - 1], close[i]
        if a is not None and b is not None and a > 0:
            out[i] = b / a - 1.0
    return out


def _num(x, d):
    if x is None:
        return None
    x = float(x)
    if not math.isfinite(x):
        return None
    return round(x, d)


def corr_block(R, w, min_share=MIN_SHARE):
    """(corr DataFrame, annualised vol Series) over the last `w` rows of the
    returns frame `R`. Pairwise-complete, with a floor on the overlap."""
    tail = R.tail(w)
    mp = max(3, int(math.ceil(w * min_share)))
    c = tail.corr(min_periods=mp)
    n = tail.notna().sum()
    vol = tail.std(ddof=1) * math.sqrt(ANN)
    vol[n < mp] = np.nan
    return c, vol


def pc1_share(c):
    """Share of total variance on the first principal component of a correlation
    matrix. Rows/columns with any missing pair are left out; None under 3."""
    a = c.to_numpy(dtype=float).copy()
    np.fill_diagonal(a, np.nan)
    live = [k for k, row in zip(c.index, a) if np.isfinite(row).any()]   # has at least one pair
    m = c.loc[live, live]
    if m.isna().any().any():
        keep = [k for k in m.index if m.loc[k].notna().all()]
        m = m.loc[keep, keep]
    if len(m) < 3:
        return None
    ev = np.linalg.eigvalsh(m.to_numpy(dtype=float))
    tot = float(ev.sum())
    return float(ev[-1] / tot) if tot > 0 else None


def build(dates, series, etfs=None, bench=None, windows=WINDOWS, roll_w=ROLL_W,
          hist=HIST, step=STEP, top_n=TOP_N, roll_bench=None):
    """The whole payload body (everything but generated_at) from a panel."""
    etfs = RG.REG_ETFS if etfs is None else etfs
    bench = BENCH if bench is None else bench
    roll_bench = ROLL_BENCH if roll_bench is None else roll_bench
    errors = {}

    # ---- the series, as the ladder sees them --------------------------------
    lad = RG.sector_ladder(series, etfs)
    twin = {r["t"]: r["twin_of"] for r in lad["rows"]}
    stale = {r["t"] for r in lad["rows"] if r.get("stale")}
    cols, meta = {}, {}
    for e in etfs:
        c = RG.basket_series(series, e["basket"]) if e.get("basket") else series.get(e["t"])
        if not c:
            errors[e["t"]] = "no series in prices.db"
            continue
        if e["t"] in stale:
            errors[e["t"]] = "series is stale"
            continue
        cols[e["t"]] = daily_returns(c)
        meta[e["t"]] = {"name": e["name"], "sector": e.get("sector"), "side": e.get("side"),
                        "basket": bool(e.get("basket")), "twin_of": twin.get(e["t"])}
    sleeves = list(cols.keys())
    bcols = []
    for t, name in bench:
        if t in cols:
            continue
        c = series.get(t)
        if not c:
            errors[t] = "benchmark not in prices.db"
            continue
        cols[t] = daily_returns(c)
        meta[t] = {"name": name, "sector": "Benchmark", "side": None, "basket": False,
                   "twin_of": None, "bench": True}
        bcols.append(t)
    order = sleeves + bcols
    R = pd.DataFrame({t: pd.Series(cols[t], dtype="float64") for t in order}, columns=order)
    field = [t for t in sleeves if not meta[t]["twin_of"]]
    roll_bench = [b for b in roll_bench if b in cols]

    # ---- the matrices -------------------------------------------------------
    corr, vol, blocks = {}, {}, {}
    for w in windows:
        c, v = corr_block(R, w)
        blocks[w] = (c, v)
        # lower triangle, row by row: row i holds i values (pairs with 0..i-1)
        tri = []
        a = c.to_numpy(dtype=float)
        for i in range(len(order)):
            tri.append([_num(a[i, j], 3) for j in range(i)])
        corr[str(w)] = tri
        vol[str(w)] = [_num(v[t], 4) for t in order]

    # ---- per sleeve: who it moves with, and against each benchmark ---------
    rows = {}
    w0 = windows[0]
    c0, v0 = blocks[w0]
    cL, vL = blocks[windows[-1]]
    bench_all = [b for b in ["SPY", "QQQ", "IWM"] if b in cols] + bcols
    for t in sleeves:
        others = [u for u in field if u != t and u != meta[t]["twin_of"] and meta[u]["twin_of"] != t]
        pairs = [(u, c0.at[t, u]) for u in others if pd.notna(c0.at[t, u])]
        pairs.sort(key=lambda p: (-p[1], p[0]))
        hi = [[u, _num(x, 2), _num(cL.at[t, u], 2)] for u, x in pairs[:top_n]]
        lo = [[u, _num(x, 2), _num(cL.at[t, u], 2)] for u, x in sorted(pairs, key=lambda p: (p[1], p[0]))[:top_n]]
        b = {}
        for k in bench_all:
            if k == t:
                continue
            ent = {}
            for w in windows:
                c, v = blocks[w]
                x = c.at[t, k]
                if pd.isna(x) or pd.isna(v[t]) or pd.isna(v[k]) or not v[k] > 0:
                    ent[str(w)] = None
                else:
                    ent[str(w)] = {"corr": _num(x, 2), "beta": _num(x * v[t] / v[k], 2), "r2": _num(x * x, 2)}
            b[k] = ent
        fm = [x for _, x in pairs]
        fl = [float(cL.at[t, u]) for u in others if pd.notna(cL.at[t, u])]
        rows[t] = {"hi": hi, "lo": lo, "bench": b,
                   "field": {str(w0): _num(sum(fm) / len(fm), 2) if fm else None,
                             str(windows[-1]): _num(sum(fl) / len(fl), 2) if fl else None},
                   "n_pairs": len(pairs)}

    # ---- the history --------------------------------------------------------
    n = len(dates)
    last = n - 1
    idx = [i for i in range(last, max(roll_w, last - hist) - 1, -step)][::-1]
    mp = max(3, int(math.ceil(roll_w * MIN_SHARE)))
    rdates = [dates[i] for i in idx]
    per = {t: {k: [] for k in roll_bench + ["field"]} for t in sleeves}
    f_avg, f_pc1, f_n = [], [], []
    for i in idx:
        win = R.iloc[i - roll_w + 1:i + 1]
        c = win.corr(min_periods=mp)
        cf = c.loc[field, field]
        a = cf.to_numpy(dtype=float).copy()
        np.fill_diagonal(a, np.nan)
        live = ~np.all(np.isnan(a), axis=1)
        with np.errstate(invalid="ignore"):
            rowmean = np.where(live, np.nanmean(np.where(live[:, None], a, 0.0), axis=1), np.nan) if live.any() else np.full(len(field), np.nan)
        fmean = dict(zip(field, rowmean))
        vals = a[np.isfinite(a)]
        f_avg.append(_num(vals.mean(), 3) if vals.size else None)
        f_pc1.append(_num(pc1_share(cf), 3))
        f_n.append(int(live.sum()))
        for t in sleeves:
            for k in roll_bench:
                per[t][k].append(None if k == t else _num(c.at[t, k], 2))
            if meta[t]["twin_of"]:
                # a twin's own read against the field it is held out of
                oth = [u for u in field if u != meta[t]["twin_of"]]
                x = c.loc[t, oth].to_numpy(dtype=float)
                per[t]["field"].append(_num(np.nanmean(x), 2) if np.isfinite(x).any() else None)
            else:
                per[t]["field"].append(_num(fmean.get(t), 2))
    # A point speaks for "the field" only when nearly all of today's field was
    # there: a mean over 20 sleeves is not the same quantity as one over 60.
    need = int(math.ceil(FIELD_SHARE * (f_n[-1] if f_n else 0)))
    for j, k in enumerate(f_n):
        if k < need:
            f_avg[j] = None
            f_pc1[j] = None
            for t in sleeves:
                per[t]["field"][j] = None
    for t in sleeves:
        rows[t]["roll"] = per[t]

    return {
        "asof": dates[-1] if dates else None,
        "axis": "equity-trading-day (SPY)",
        "windows": list(windows),
        "tickers": order,
        "meta": meta,
        "n_sleeves": len(sleeves),
        "field": field,
        "bench": bcols,
        "corr": corr,
        "vol": vol,
        "rows": rows,
        "roll": {"window": roll_w, "step": step, "dates": rdates, "bench": roll_bench,
                 "avg": f_avg, "pc1": f_pc1, "n": f_n, "need": need},
        "errors": errors,
        "method": ("Pearson correlation of daily simple returns on the ladder's own series (basket rows are the "
                   "equal-weight basket index), pairwise on the sessions both have, null under %d%% of the window. "
                   "vol is annualised (x sqrt 252); annualised covariance = corr x vol_i x vol_j. beta = corr x vol_i / "
                   "vol_bench; r2 = corr squared. roll = the %d-session correlation every %d sessions; field avg = mean "
                   "pairwise correlation of the non-twin sleeves; pc1 = share of variance on the first principal "
                   "component of their correlation matrix. Display only." % (int(MIN_SHARE * 100), roll_w, step)),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--out", default=OUT_PATH)
    a = ap.parse_args(argv)
    conn = sqlite3.connect(a.db)
    dates, series = RG.build_panel(conn)
    conn.close()
    if not dates:
        print("rotation_corr: empty panel", file=sys.stderr)
        return 1
    out = {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    out.update(build(dates, series))
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"), allow_nan=False)
    os.replace(tmp, a.out)
    print("rotation_corr: %d sleeves + %d benchmarks, %d history points, asof %s, %d bytes, %d error(s)"
          % (out["n_sleeves"], len(out["bench"]), len(out["roll"]["dates"]), out["asof"],
             os.path.getsize(a.out), len(out["errors"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
