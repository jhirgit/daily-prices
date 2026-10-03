#!/usr/bin/env python3
"""Bollinger %B "buyable bottom" study -- backward replication + forward test.

    python pctb_backtest.py --db research-prices.db --tickers SPY,QQQ,IWM [--start 2000-01-01] [--end ...]

Reads the daily_prices table (fetch_prices.py schema). Indicators on dividend-adjusted
prices (open/high/low scaled by adj_close/close). %B(20,2), RSI(14, Wilder), Stoch %K(14)/%D(3),
MACD(12,26,9) histogram.
Part A (the post's method): pivot low = lowest close within +-5 sessions AND max close over the
next 10 sessions >= +5%; pivot high mirrored (-5%). "Ordinary" = >2 sessions from any pivot.
Part B (forward test): every run of flagged days counted once at its first day; forward returns
vs every-day baseline; p = share of 5000 random same-size day sets whose mean 20d return >= the signal's.
"""
import argparse, sqlite3, numpy as np, pandas as pd

def load(c, t):
    d = pd.read_sql("select date,open,high,low,close,adj_close from daily_prices where ticker=? order by date",
                    c, params=(t,), parse_dates=["date"]).set_index("date")
    f = d.adj_close / d.close
    for k in ["open", "high", "low"]: d[k] = d[k] * f
    d["c"] = d.adj_close
    return d

def ind(d):
    c = d.c; m = c.rolling(20).mean(); s = c.rolling(20).std(ddof=0)
    d["pb"] = (c - (m - 2*s)) / (4*s)
    dl = c.diff(); up = dl.clip(lower=0).ewm(alpha=1/14, adjust=False).mean(); dn = (-dl.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    d["rsi"] = 100 - 100/(1 + up/dn)
    lo = d.low.rolling(14).min(); hi = d.high.rolling(14).max()
    d["k"] = 100*(c - lo)/(hi - lo); d["d"] = d.k.rolling(3).mean()
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    d["mh"] = macd - macd.ewm(span=9, adjust=False).mean()
    d["b50"] = c < c.rolling(50).mean()
    a = c.values; n = len(a); fmax = np.full(n, np.nan); fmin = np.full(n, np.nan)
    for i in range(n - 10): fmax[i] = a[i+1:i+11].max(); fmin[i] = a[i+1:i+11].min()
    d["bounce10"] = fmax/a - 1; d["drop10"] = fmin/a - 1
    for h in (5, 10, 20, 60): d[f"f{h}"] = c.shift(-h)/c - 1
    return d.dropna(subset=["pb", "rsi", "k", "d", "mh"])

def pivots(d, W=5):
    a = d.c.values; lows, highs = [], []
    for i in range(W, len(a) - 10):
        w = a[i-W:i+W+1]
        if a[i] == w.min() and d.bounce10.iloc[i] >= 0.05: lows.append(i)
        if a[i] == w.max() and d.drop10.iloc[i] <= -0.05: highs.append(i)
    return lows, highs

def episodes(mask):
    m = mask.values; return np.where(m & ~np.r_[False, m[:-1]])[0]

def run(c, t, start=None, end=None, rng=None):
    rng = rng or np.random.default_rng(0)
    d = ind(load(c, t))
    if start: d = d[d.index >= start]
    if end: d = d[d.index <= end]
    d = d[d.f20.notna()]
    lows, highs = pivots(d)
    piv = set(lows) | set(highs)
    ordi = np.array([i for i in range(len(d)) if not any(abs(i - p) <= 2 for p in piv)])
    print(f"\n######## {t}  {d.index[0].date()}..{d.index[-1].date()}  {len(d)} days · {len(lows)} bottoms · {len(highs)} tops")
    L, O = d.iloc[lows], d.iloc[ordi]
    print("A. median at bottoms vs ordinary days:")
    for k in ["mh", "pb", "k", "d", "rsi"]: print(f"   {k:4s} {L[k].median():7.2f} vs {O[k].median():7.2f}")
    rules = {"pb<0.15": d.pb < 0.15, "pb<0.15 & <50dma": (d.pb < 0.15) & d.b50,
             "rsi<35 & pb<0.15": (d.rsi < 35) & (d.pb < 0.15), "rsi<30": d.rsi < 30}
    print("A. share of bottoms / tops / ordinary days printing the rule:")
    for r, m in rules.items():
        print(f"   {r:20s} bottoms {m.iloc[lows].mean():5.0%}  tops {m.iloc[highs].mean() if highs else float('nan'):5.0%}  ordinary {m.iloc[ordi].mean():5.1%}")
    a = d.c.values; mins = [i for i in range(5, len(a) - 5) if a[i] == a[i-5:i+6].min()]
    nob = [i for i in mins if i not in set(lows)]
    print(f"A. placebo: all 11-day local lows with pb<0.15 {(d.pb.iloc[mins] < 0.15).mean():.0%}; lows that did NOT bounce 5% {(d.pb.iloc[nob] < 0.15).mean():.0%}")
    print(f"B. baseline any day: f5 {d.f5.mean():+.2%} f10 {d.f10.mean():+.2%} f20 {d.f20.mean():+.2%} f60 {d.f60.mean():+.2%} up20 {(d.f20 > 0).mean():.0%}  P(>=5% bounce in 10d) {(d.bounce10 >= 0.05).mean():.1%}")
    pool = d.f20.dropna().values
    for r, m in rules.items():
        e = episodes(m); E = d.iloc[e]
        if not len(e): print(f"B. {r:20s} no signals"); continue
        near = np.mean([any(abs(i - p) <= 3 for p in lows) for i in e])
        f20 = E.f20.dropna().values
        p = (np.array([rng.choice(pool, len(f20)).mean() for _ in range(5000)]) >= f20.mean()).mean()
        print(f"B. {r:20s} days {int(m.sum()):4d} eps {len(e):3d} | f5 {E.f5.mean():+.2%} f10 {E.f10.mean():+.2%} f20 {E.f20.mean():+.2%} f60 {E.f60.mean():+.2%} up20 {(E.f20 > 0).mean():.0%} | >=5% bounce {(E.bounce10 >= 0.05).mean():.0%} | at a real bottom {near:.0%} | worst f20 {E.f20.min():+.1%} | p(random>=) {p:.2f}")
        by = {y: f"{g.f20.mean():+.1%}/{len(g)}" for y, g in E.groupby(E.index.year)}
        print(f"   by year (mean f20 / n): {by}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="prices.db"); ap.add_argument("--tickers", default="SPY,QQQ,IWM")
    ap.add_argument("--start"); ap.add_argument("--end")
    a = ap.parse_args(); con = sqlite3.connect(a.db)
    for t in a.tickers.split(","): run(con, t.strip(), a.start, a.end)
