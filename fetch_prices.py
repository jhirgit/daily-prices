#!/usr/bin/env python3
"""
Fetch daily OHLC bars and a delayed spot quote for a list of tickers and
store them in a local SQLite database.

Designed to run once per weekday (e.g. from a GitHub Actions cron) so the
database accumulates a price history over time for later market analysis.

Data source: Yahoo Finance via the `yfinance` library.

Tables:
  daily_prices  - one settled OHLC row per (ticker, trading date)
  spot_quotes   - a delayed "spot" snapshot per (ticker, capture time)

Re-running is safe: daily bars are upserted by (ticker, date), so a run that
follows a weekend, holiday, or outage backfills any gap in the lookback window.
"""

from __future__ import annotations

import argparse
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

import yfinance as yf

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "prices.db")
DEFAULT_TICKERS = os.path.join(HERE, "tickers.txt")

# Calendar-day window of daily bars to pull each run. A window >1 day means a
# run after a weekend/holiday/outage backfills the gap automatically (the
# upsert dedupes overlapping dates).
LOOKBACK = "7d"

# Seconds to pause between tickers to stay clear of Yahoo rate limits.
SLEEP_BETWEEN = 1.0

# Retry attempts per ticker for transient network / rate-limit errors.
ATTEMPTS = 3


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _f(v):
    """Best-effort float; None for missing / NaN / garbage."""
    try:
        if v is None:
            return None
        v = float(v)
        return None if math.isnan(v) else v
    except (TypeError, ValueError):
        return None


def _i(v):
    f = _f(v)
    return None if f is None else int(f)


def _fmt(v):
    return f"{v:.2f}" if isinstance(v, (int, float)) else "n/a"


def load_tickers(path: str) -> list[str]:
    """One ticker per line; '#' comments (inline or full-line) and blanks ignored."""
    out, seen = [], set()
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            sym = line.split("#", 1)[0].strip().upper()
            if sym and sym not in seen:
                seen.add(sym)
                out.append(sym)
    return out


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS daily_prices (
            ticker      TEXT NOT NULL,
            date        TEXT NOT NULL,      -- trading date, YYYY-MM-DD
            open        REAL,
            high        REAL,
            low         REAL,
            close       REAL,
            adj_close   REAL,               -- split/dividend-adjusted close
            volume      INTEGER,
            source      TEXT,
            updated_at  TEXT,               -- UTC time this row was last written
            PRIMARY KEY (ticker, date)
        );

        CREATE TABLE IF NOT EXISTS spot_quotes (
            ticker         TEXT NOT NULL,
            captured_at    TEXT NOT NULL,   -- UTC time of capture
            price          REAL,            -- delayed last trade price
            previous_close REAL,
            currency       TEXT,
            source         TEXT,
            PRIMARY KEY (ticker, captured_at)
        );

        CREATE INDEX IF NOT EXISTS idx_daily_ticker ON daily_prices (ticker);
        CREATE INDEX IF NOT EXISTS idx_spot_ticker  ON spot_quotes  (ticker);
        """
    )
    conn.commit()


def _fast_get(fast_info, *keys):
    """Read a field from yfinance fast_info across versions (attr or mapping)."""
    for k in keys:
        v = getattr(fast_info, k, None)
        if v is not None:
            return v
        try:
            v = fast_info[k]
        except (KeyError, TypeError, AttributeError):
            v = None
        if v is not None:
            return v
    return None


def upsert_daily(conn, ticker, bars) -> int:
    """Insert/update every bar in the lookback window; returns rows touched."""
    now = utc_now_iso()
    n = 0
    for ts, row in bars.iterrows():
        conn.execute(
            """
            INSERT INTO daily_prices
                (ticker, date, open, high, low, close, adj_close, volume, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'yfinance', ?)
            ON CONFLICT(ticker, date) DO UPDATE SET
                open=excluded.open, high=excluded.high, low=excluded.low,
                close=excluded.close, adj_close=excluded.adj_close,
                volume=excluded.volume, source=excluded.source,
                updated_at=excluded.updated_at
            """,
            (
                ticker, ts.strftime("%Y-%m-%d"),
                _f(row.get("Open")), _f(row.get("High")), _f(row.get("Low")),
                _f(row.get("Close")), _f(row.get("Adj Close")),
                _i(row.get("Volume")), now,
            ),
        )
        n += 1
    return n


def record_spot(conn, ticker, tkr):
    """Capture the current delayed quote; returns the last price (or None)."""
    fi = tkr.fast_info
    price = _f(_fast_get(fi, "last_price", "lastPrice"))
    prev = _f(_fast_get(fi, "previous_close", "previousClose"))
    cur = _fast_get(fi, "currency")
    conn.execute(
        "INSERT OR REPLACE INTO spot_quotes "
        "(ticker, captured_at, price, previous_close, currency, source) "
        "VALUES (?, ?, ?, ?, ?, 'yfinance')",
        (ticker, utc_now_iso(), price, prev, cur),
    )
    return price


def process_ticker(conn, symbol):
    tkr = yf.Ticker(symbol)
    bars = tkr.history(period=LOOKBACK, auto_adjust=False, actions=False)
    if bars is None or bars.empty:
        raise RuntimeError("no daily bars returned (delisted or bad symbol?)")
    n = upsert_daily(conn, symbol, bars)
    spot = record_spot(conn, symbol, tkr)
    conn.commit()
    last = bars.iloc[-1]
    return n, _f(last.get("Open")), _f(last.get("Close")), spot


def process_with_retry(conn, symbol):
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return process_ticker(conn, symbol)
        except Exception as e:  # noqa: BLE001 - per-ticker isolation is intentional
            if attempt == ATTEMPTS:
                raise
            wait = 2 ** attempt
            print(f"    [warn] {symbol}: attempt {attempt} failed ({e}); retry in {wait}s")
            time.sleep(wait)


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch daily prices into SQLite.")
    ap.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    ap.add_argument("--tickers", default=DEFAULT_TICKERS, help="ticker list file")
    args = ap.parse_args()

    symbols = load_tickers(args.tickers)
    if not symbols:
        print(f"No tickers found in {args.tickers}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    init_db(conn)

    print(f"Fetching {len(symbols)} ticker(s) into {args.db}")
    ok, failed = 0, []
    for i, sym in enumerate(symbols):
        try:
            n, o, c, spot = process_with_retry(conn, sym)
            print(f"  [ok]   {sym:8s} bars+{n}  open={_fmt(o)} close={_fmt(c)} spot={_fmt(spot)}")
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  [FAIL] {sym:8s} {e}")
            failed.append(sym)
        if i < len(symbols) - 1:
            time.sleep(SLEEP_BETWEEN)

    conn.close()
    summary = f"{ok} ok, {len(failed)} failed"
    if failed:
        summary += f" ({', '.join(failed)})"
    print(f"\nDone: {summary}")

    # Succeed if at least one ticker worked; fail the job only on a total
    # wipeout (network / library outage) so one bad symbol doesn't break CI.
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
