#!/usr/bin/env python3
"""
Export the SQLite price database to plain-text files that can be served over a
public URL (e.g. GitHub raw) and read directly by an LLM such as Claude.ai chat.

Writes into an output directory (default: ./data):
  daily_prices.csv  - full history of settled OHLC bars
  spot_quotes.csv   - full history of delayed spot quotes
  latest.json       - compact snapshot: the newest bar + newest quote per ticker

SQLite is binary and can't be fetched-and-read from a URL; these text exports
can. Run it after fetch_prices.py (the GitHub Actions workflow does exactly
this, then commits the refreshed files).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "prices.db")
DEFAULT_OUT = os.path.join(HERE, "data")

# Price columns are rounded on export: raw yfinance values carry float noise
# (e.g. 194.8300018310547) that bloats the files and reads badly. Volume stays int.
PRICE_DP = 4


def _round(v, dp=PRICE_DP):
    return round(v, dp) if isinstance(v, float) else v


def _export_table(conn: sqlite3.Connection, table: str, price_cols: set[str], path: str) -> int:
    cur = conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2")
    cols = [d[0] for d in cur.description]
    n = 0
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for row in cur:
            w.writerow([_round(v) if c in price_cols else v for c, v in zip(cols, row)])
            n += 1
    return n


def _latest_snapshot(conn: sqlite3.Connection) -> dict:
    """Newest daily bar + newest spot quote for every ticker, as a compact dict."""
    daily = {
        r["ticker"]: r
        for r in conn.execute(
            """
            SELECT d.ticker, d.date, d.open, d.high, d.low, d.close, d.adj_close, d.volume
            FROM daily_prices d
            JOIN (SELECT ticker, MAX(date) AS m FROM daily_prices GROUP BY ticker) t
              ON d.ticker = t.ticker AND d.date = t.m
            """
        )
    }
    spot = {
        r["ticker"]: r
        for r in conn.execute(
            """
            SELECT s.ticker, s.captured_at, s.price, s.previous_close, s.currency
            FROM spot_quotes s
            JOIN (SELECT ticker, MAX(captured_at) AS m FROM spot_quotes GROUP BY ticker) t
              ON s.ticker = t.ticker AND s.captured_at = t.m
            """
        )
    }
    tickers = []
    for sym in sorted(set(daily) | set(spot)):
        d, s = daily.get(sym), spot.get(sym)
        tickers.append(
            {
                "ticker": sym,
                "date": d["date"] if d else None,
                "open": _round(d["open"]) if d else None,
                "high": _round(d["high"]) if d else None,
                "low": _round(d["low"]) if d else None,
                "close": _round(d["close"]) if d else None,
                "adj_close": _round(d["adj_close"]) if d else None,
                "volume": d["volume"] if d else None,
                "spot_price": _round(s["price"]) if s else None,
                "spot_captured_at": s["captured_at"] if s else None,
                "currency": s["currency"] if s else None,
            }
        )
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Yahoo Finance via yfinance",
        "note": (
            "close is the latest settled daily close; spot_price is the last "
            "delayed quote captured when the job ran, so they can differ intraday."
        ),
        "count": len(tickers),
        "tickers": tickers,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Export prices.db to CSV/JSON for public URLs.")
    ap.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output directory")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    daily_csv = os.path.join(args.out, "daily_prices.csv")
    spot_csv = os.path.join(args.out, "spot_quotes.csv")
    latest_json = os.path.join(args.out, "latest.json")

    nd = _export_table(conn, "daily_prices", {"open", "high", "low", "close", "adj_close"}, daily_csv)
    ns = _export_table(conn, "spot_quotes", {"price", "previous_close"}, spot_csv)
    snap = _latest_snapshot(conn)
    with open(latest_json, "w", encoding="utf-8") as fh:
        json.dump(snap, fh, indent=2)
        fh.write("\n")
    conn.close()

    print(f"Wrote {nd} rows -> {daily_csv}")
    print(f"Wrote {ns} rows -> {spot_csv}")
    print(f"Wrote {snap['count']} tickers -> {latest_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
