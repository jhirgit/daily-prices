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

Adjusted closes stay consistent across runs. Yahoo's Adj Close for a date is
that close times the product of every dividend factor AFTER it, so each new
ex-date changes adj_close for the WHOLE history, and a split changes every
older close too. A run fetches only the lookback window, so before it writes,
`rebase_history` compares the oldest overlapping bar with the stored one and
carries the change back to every older row. Without it (review H1, 2026-09-26)
each dividend landed as a fake one-day loss that was credited back a week
later, and adj_close was in effect a price series.
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

# "The adjustment basis moved" threshold on the adj/close ratio at the overlap
# bar. Yahoo prices arrive as float32-grade values, so an unchanged ratio can
# wobble by ~1e-7; the smallest real dividend in coverage moves it by ~5e-5
# (one cent on a $200 stock). 1e-6 sits between the two.
ADJ_TOL = 1e-6

# A close at the overlap bar that moved by more than this is a split (when the
# window carries a split event that explains it) or bad data (when it does not).
# A one-cent revision of a settled close is ~1e-4, far below it.
SPLIT_TOL = 0.02


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


def rebase_history(conn, ticker, bars) -> dict:
    """Carry an adjustment-basis change back to the stored rows older than `bars`.

    Call BEFORE upsert_daily writes the window. Takes the oldest fetched bar
    that is already stored (d0) and compares its two versions:

      k_div   = (adj_new/close_new) / (adj_old/close_old)   a new dividend
      k_close = close_new / close_old                        a new split

    Every stored row older than d0 gets adj_close *= k_div * k_split and, on a
    split, open/high/low/close *= k_split and volume /= k_split. d0 carries the
    product of every factor the stored history has not seen yet, so one
    comparison re-bases the whole tail however many ex-dates fell in the window.

    k_div is a ratio of ratios, so a cent-level revision of a settled close
    (which moves close and adj together) never reads as a dividend. A split is
    applied only when the window's "Stock Splits" events explain the close
    move; a move with no event to explain it is reported and never applied (a
    bad bar, or a split Yahoo has not published yet -- repair_adj.py fixes the
    history once it settles).

    Returns {"status", "d0", "k_adj", "k_split", "rows"}; status is one of
    "same", "rebased", "no-overlap", "empty", "close-mismatch".
    """
    fresh = []
    for ts, row in bars.iterrows():
        fresh.append((ts.strftime("%Y-%m-%d"), _f(row.get("Close")),
                      _f(row.get("Adj Close")), _f(row.get("Stock Splits")) or 0.0))
    if not fresh:
        return {"status": "empty", "rows": 0}
    stored = {d: (c, a) for d, c, a in conn.execute(
        "SELECT date, close, adj_close FROM daily_prices WHERE ticker=? AND date>=? AND date<=?",
        (ticker, fresh[0][0], fresh[-1][0]))}
    d0 = None
    for d, c_new, a_new, _ in fresh:
        c_old, a_old = stored.get(d, (None, None))
        if all(v is not None and v > 0 for v in (c_new, a_new, c_old, a_old)):
            d0 = (d, c_new, a_new, c_old, a_old)
            break
    if d0 is None:
        older = conn.execute("SELECT COUNT(*) FROM daily_prices WHERE ticker=? AND date<?",
                             (ticker, fresh[0][0])).fetchone()[0]
        return {"status": "no-overlap" if older else "empty", "rows": 0}
    d, c_new, a_new, c_old, a_old = d0
    k_div = (a_new / c_new) / (a_old / c_old)
    k_close = c_new / c_old
    out = {"d0": d, "k_adj": 1.0, "k_split": 1.0, "rows": 0}
    # A split event stays inside the window for a week after it happens, so
    # the event alone does not say whether the stored d0 predates it; the close
    # move does. Unmoved: nothing new. Moved by exactly the events: a new split.
    if abs(k_close - 1.0) <= SPLIT_TOL:
        k_split = 1.0
    else:
        ratio = 1.0
        for dd, _, _, s in fresh:
            if dd > d and s > 0:
                ratio *= s
        if ratio == 1.0 or abs(k_close * ratio - 1.0) > SPLIT_TOL:
            return dict(out, status="close-mismatch", k_close=k_close)
        k_split = 1.0 / ratio
    if abs(k_div - 1.0) <= ADJ_TOL and k_split == 1.0:
        return dict(out, status="same")
    k_adj = k_div * k_split
    n = conn.execute("SELECT COUNT(*) FROM daily_prices WHERE ticker=? AND date<?",
                     (ticker, d)).fetchone()[0]
    if k_split != 1.0:
        conn.execute(
            "UPDATE daily_prices SET open=open*?, high=high*?, low=low*?, close=close*?, "
            "adj_close=adj_close*?, volume=CAST(ROUND(volume/?) AS INTEGER) "
            "WHERE ticker=? AND date<?",
            (k_split, k_split, k_split, k_split, k_adj, k_split, ticker, d))
    else:
        conn.execute("UPDATE daily_prices SET adj_close=adj_close*? WHERE ticker=? AND date<?",
                     (k_adj, ticker, d))
    return dict(out, status="rebased", k_adj=k_adj, k_split=k_split, rows=n)


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


def process_ticker(conn, symbol, lookback=LOOKBACK):
    tkr = yf.Ticker(symbol)
    # actions=True only keeps the Dividends / Stock Splits columns (Yahoo sends
    # the events either way); rebase_history reads the split events.
    bars = tkr.history(period=lookback, auto_adjust=False, actions=True)
    if bars is None or bars.empty:
        raise RuntimeError("no daily bars returned (delisted or bad symbol?)")
    rb = rebase_history(conn, symbol, bars)
    n = upsert_daily(conn, symbol, bars)
    spot = record_spot(conn, symbol, tkr)
    conn.commit()
    last = bars.iloc[-1]
    return n, _f(last.get("Open")), _f(last.get("Close")), spot, rb


def rebase_note(rb) -> str:
    """One log suffix for rebase_history's result ('' when nothing happened)."""
    st = rb.get("status")
    if st == "rebased":
        what = "split+adj" if rb["k_split"] != 1.0 else "adj"
        return f"  rebased {what} x{rb['k_adj']:.6f} on {rb['rows']} rows before {rb['d0']}"
    if st == "no-overlap":
        return "  WARN no overlap with stored history: older rows NOT re-based (run repair_adj.py)"
    if st == "close-mismatch":
        return (f"  WARN close at {rb['d0']} moved x{rb['k_close']:.4f} with no split event: "
                "older rows NOT re-based (run repair_adj.py once Yahoo settles)")
    return ""


def process_with_retry(conn, symbol, lookback=LOOKBACK):
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return process_ticker(conn, symbol, lookback)
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
    ap.add_argument("--lookback", default=LOOKBACK,
                    help="history window per run, yfinance period syntax (default %(default)s; "
                         "use e.g. 1y for a one-time backfill — upsert dedupes overlap)")
    args = ap.parse_args()

    symbols = load_tickers(args.tickers)
    if not symbols:
        print(f"No tickers found in {args.tickers}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    init_db(conn)

    print(f"Fetching {len(symbols)} ticker(s) into {args.db}")
    ok, failed, rebased, unsure = 0, [], 0, []
    for i, sym in enumerate(symbols):
        try:
            n, o, c, spot, rb = process_with_retry(conn, sym, args.lookback)
            print(f"  [ok]   {sym:8s} bars+{n}  open={_fmt(o)} close={_fmt(c)} spot={_fmt(spot)}"
                  + rebase_note(rb))
            ok += 1
            rebased += rb.get("status") == "rebased"
            if rb.get("status") in ("no-overlap", "close-mismatch"):
                unsure.append(sym)
        except Exception as e:  # noqa: BLE001
            print(f"  [FAIL] {sym:8s} {e}")
            failed.append(sym)
        if i < len(symbols) - 1:
            time.sleep(SLEEP_BETWEEN)

    conn.close()
    summary = f"{ok} ok, {len(failed)} failed"
    if failed:
        summary += f" ({', '.join(failed)})"
    summary += f"; {rebased} re-based for a dividend or split"
    if unsure:
        summary += f"; {len(unsure)} NOT re-based ({', '.join(unsure)})"
    print(f"\nDone: {summary}")

    # Succeed if at least one ticker worked; fail the job only on a total
    # wipeout (network / library outage) so one bad symbol doesn't break CI.
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
