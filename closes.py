#!/usr/bin/env python3
"""closes.py — compact adjusted-close history for the dashboard's Book performance view (#51).

Emits data/closes.json: ONE shared trading-day axis (SPY's settled sessions, last N) and, per
ticker in tickers.txt, the adjusted close aligned to that axis (null where the name has no bar
that day). Adjusted closes so dividends and splits do not read as returns.

    {"generated_at": "...", "asof": "YYYY-MM-DD", "n": 756, "dates": [...],
     "tickers": {"SPY": [..756 floats/nulls..], ...}}

Why this exists: the browser stopped fetching daily_prices.csv.gz in backlog #8 (3.7 MB gunzip per
load); the Book tab needs a few years of closes to compute the current book's constant-weight
history vs a benchmark, rolling vol, drawdown and beta. ~140 names x 756 sessions rounds to ~1 MB
raw and compresses well at the edge. Public prices only (#15): nothing about the book is here —
the weights join in the browser from the deploy-only payload.

    python closes.py            # write data/closes.json (default 756 sessions ~ 3 years)
    python closes.py --n 504
Stdlib only.
"""
import argparse
import datetime as dt
import json
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "prices.db")
DEFAULT_TICKERS = os.path.join(HERE, "tickers.txt")
DEFAULT_OUT = os.path.join(HERE, "data", "closes.json")
AXIS = "SPY"


def load_tickers(path=DEFAULT_TICKERS):
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            t = line.split("#", 1)[0].strip()
            if t:
                out.append(t)
    return out


def build(db=DEFAULT_DB, tickers=DEFAULT_TICKERS, n=756):
    conn = sqlite3.connect(db)
    dates = [r[0] for r in conn.execute(
        "SELECT date FROM daily_prices WHERE ticker=? AND close IS NOT NULL ORDER BY date", (AXIS,))]
    dates = dates[-n:]
    idx = {d: i for i, d in enumerate(dates)}
    out = {}
    for t in load_tickers(tickers):
        row = [None] * len(dates)
        for d, adj, close in conn.execute(
                "SELECT date, adj_close, close FROM daily_prices WHERE ticker=? AND date>=? ORDER BY date",
                (t, dates[0] if dates else "0000")):
            i = idx.get(d)
            if i is None:
                continue
            v = adj if adj is not None else close
            row[i] = round(v, 4) if v is not None else None
        if any(v is not None for v in row):
            out[t] = row
    conn.close()
    return {"generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "asof": dates[-1] if dates else None, "axis": AXIS, "n": len(dates),
            "field": "adj_close (close where adj_close is null)", "dates": dates, "tickers": out}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--tickers", default=DEFAULT_TICKERS)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--n", type=int, default=756)
    a = ap.parse_args()
    payload = build(a.db, a.tickers, a.n)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    print("wrote %s: %d names x %d sessions, asof %s" % (a.out, len(payload["tickers"]), payload["n"], payload["asof"]))


if __name__ == "__main__":
    main()
