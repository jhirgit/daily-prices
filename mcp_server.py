#!/usr/bin/env python3
"""
MCP server exposing the daily-prices SQLite database to an LLM client
(e.g. Claude.ai chat via a custom connector, or Claude Desktop locally).

It serves **read-only** access to the data collected by ``fetch_prices.py``:
the ``daily_prices`` (settled OHLC bars) and ``spot_quotes`` (delayed quotes)
tables in ``prices.db``. One tool (``get_intraday_quotes``) additionally
fetches live delayed quotes straight from Yahoo on demand.

Tools
-----
Curated, predictable queries (from the database):
  list_tickers           - which symbols exist, with their coverage window
  get_price_history      - daily OHLC bars for one ticker over a date range
  get_latest_quote       - newest daily close + newest delayed spot quote
  compare_performance    - % return of several tickers over a window
Live, on demand (fetched from Yahoo, not the database):
  get_intraday_quotes    - current delayed quotes for a batch of tickers
A guarded escape hatch for ad-hoc analysis:
  run_sql                - a single read-only SELECT/WITH statement

Transports
----------
Default is ``streamable-http`` (what Claude.ai custom connectors speak). Use
``--transport stdio`` for a local Claude Desktop connection.

  # Remote connector (host this, then add the URL in Claude.ai -> Connectors)
  python mcp_server.py --host 0.0.0.0 --port 8000

  # Local, for Claude Desktop
  python mcp_server.py --transport stdio

The database is opened read-only; the server never writes to it.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

HERE = os.path.dirname(os.path.abspath(__file__))
# Allow overriding the DB location for hosted deployments (e.g. a mounted volume).
DB_PATH = os.environ.get("PRICES_DB", os.path.join(HERE, "prices.db"))

# Hard cap on rows returned by any single tool call, to keep responses bounded.
MAX_ROWS = 1000

# Optional shared secret. When set, the MCP endpoint is served at an
# unguessable path (/<token>/mcp) instead of the public /mcp, so the URL itself
# acts as the credential. Claude.ai custom connectors don't support pasting a
# static bearer token (OAuth only), so a secret URL is the simplest auth that
# works with them over HTTPS. Generate one with:
#   python -c "import secrets; print(secrets.token_urlsafe(32))"
AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()

# A path segment can't contain '/'; keep it URL-safe so it sits cleanly in a URL.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~-]+$")

mcp = FastMCP("daily-prices")


# --------------------------------------------------------------------------- #
# Database helpers (read-only)
# --------------------------------------------------------------------------- #
def _connect() -> sqlite3.Connection:
    """Open prices.db strictly read-only via a file: URI."""
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(
            f"Database not found at {DB_PATH}. Run fetch_prices.py first, "
            "or set the PRICES_DB environment variable."
        )
    uri = f"file:{DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(cur: sqlite3.Cursor, limit: int = MAX_ROWS) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.fetchmany(limit)]


# A read-only SELECT/WITH, single statement, no obviously dangerous keywords.
_FORBIDDEN = re.compile(
    r"\b(attach|detach|pragma|insert|update|delete|drop|alter|create|replace|"
    r"vacuum|reindex|analyze)\b",
    re.IGNORECASE,
)


def _validate_select(sql: str) -> str:
    """Return a normalized read-only query or raise ValueError."""
    stripped = sql.strip().rstrip(";").strip()
    if not stripped:
        raise ValueError("Empty query.")
    if ";" in stripped:
        raise ValueError("Only a single statement is allowed (no ';').")
    if not re.match(r"(?is)^\s*(select|with)\b", stripped):
        raise ValueError("Only SELECT (or WITH ... SELECT) queries are allowed.")
    if _FORBIDDEN.search(stripped):
        raise ValueError("Query contains a disallowed (non-read-only) keyword.")
    return stripped


# --------------------------------------------------------------------------- #
# Curated tools
# --------------------------------------------------------------------------- #
@mcp.tool()
def list_tickers() -> list[dict[str, Any]]:
    """List every ticker in the database with its row count and date coverage.

    Returns one entry per symbol: the number of stored daily bars and the
    first/last trading dates available. Use this to discover what data exists
    before asking for specific history.
    """
    with _connect() as conn:
        cur = conn.execute(
            """
            SELECT ticker,
                   COUNT(*)  AS bars,
                   MIN(date) AS first_date,
                   MAX(date) AS last_date
            FROM daily_prices
            GROUP BY ticker
            ORDER BY ticker
            """
        )
        return _rows(cur)


@mcp.tool()
def get_price_history(
    ticker: str,
    start: str | None = None,
    end: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Daily OHLC bars for one ticker, newest first.

    Args:
        ticker: Symbol as stored, e.g. "NVDA", "BTC-USD", "^GSPC" (case-insensitive).
        start: Optional inclusive start date, "YYYY-MM-DD".
        end: Optional inclusive end date, "YYYY-MM-DD".
        limit: Max rows to return (1-1000, default 100).

    Returns rows with date, open, high, low, close, adj_close and volume.
    """
    limit = max(1, min(int(limit), MAX_ROWS))
    clauses = ["ticker = ?"]
    params: list[Any] = [ticker.upper()]
    if start:
        clauses.append("date >= ?")
        params.append(start)
    if end:
        clauses.append("date <= ?")
        params.append(end)
    params.append(limit)
    with _connect() as conn:
        cur = conn.execute(
            f"""
            SELECT date, open, high, low, close, adj_close, volume
            FROM daily_prices
            WHERE {' AND '.join(clauses)}
            ORDER BY date DESC
            LIMIT ?
            """,
            params,
        )
        return _rows(cur, limit)


@mcp.tool()
def get_latest_quote(ticker: str) -> dict[str, Any]:
    """Most recent data for a ticker: the latest settled daily bar and the
    latest delayed spot quote.

    Args:
        ticker: Symbol as stored, e.g. "AAPL", "ETH-USD" (case-insensitive).

    The daily ``close`` is the official close; ``spot.price`` is the delayed
    last trade captured when the job last ran, so they can differ intraday.
    """
    sym = ticker.upper()
    with _connect() as conn:
        daily = conn.execute(
            """
            SELECT date, open, high, low, close, adj_close, volume
            FROM daily_prices WHERE ticker = ?
            ORDER BY date DESC LIMIT 1
            """,
            (sym,),
        ).fetchone()
        spot = conn.execute(
            """
            SELECT captured_at, price, previous_close, currency
            FROM spot_quotes WHERE ticker = ?
            ORDER BY captured_at DESC LIMIT 1
            """,
            (sym,),
        ).fetchone()
    if daily is None and spot is None:
        raise ValueError(f"No data for ticker {sym!r}. Try list_tickers().")
    return {
        "ticker": sym,
        "daily": dict(daily) if daily else None,
        "spot": dict(spot) if spot else None,
    }


@mcp.tool()
def compare_performance(
    tickers: list[str],
    start: str | None = None,
    end: str | None = None,
) -> list[dict[str, Any]]:
    """Compare the price return of several tickers over a window.

    Args:
        tickers: Symbols to compare, e.g. ["NVDA", "AMD", "SMH"].
        start: Optional inclusive start date "YYYY-MM-DD" (default: earliest available).
        end: Optional inclusive end date "YYYY-MM-DD" (default: latest available).

    For each ticker returns the first/last close in the window and the percent
    change between them, sorted best to worst. Tickers with no data are skipped.
    """
    out: list[dict[str, Any]] = []
    with _connect() as conn:
        for raw in tickers:
            sym = raw.upper()
            clauses = ["ticker = ?", "close IS NOT NULL"]
            params: list[Any] = [sym]
            if start:
                clauses.append("date >= ?")
                params.append(start)
            if end:
                clauses.append("date <= ?")
                params.append(end)
            where = " AND ".join(clauses)
            first = conn.execute(
                f"SELECT date, close FROM daily_prices WHERE {where} "
                f"ORDER BY date ASC LIMIT 1",
                params,
            ).fetchone()
            last = conn.execute(
                f"SELECT date, close FROM daily_prices WHERE {where} "
                f"ORDER BY date DESC LIMIT 1",
                params,
            ).fetchone()
            if not first or not last:
                continue
            pct = None
            if first["close"]:
                pct = round((last["close"] - first["close"]) / first["close"] * 100, 2)
            out.append(
                {
                    "ticker": sym,
                    "start_date": first["date"],
                    "start_close": first["close"],
                    "end_date": last["date"],
                    "end_close": last["close"],
                    "pct_change": pct,
                }
            )
    out.sort(key=lambda r: (r["pct_change"] is None, -(r["pct_change"] or 0)))
    return out


# --------------------------------------------------------------------------- #
# Live quotes (fetched on demand from Yahoo, not from the DB)
# --------------------------------------------------------------------------- #
@mcp.tool()
def get_intraday_quotes(tickers: list[str]) -> dict[str, Any]:
    """Current delayed intraday quotes for a batch of tickers, fetched LIVE from
    Yahoo Finance right now — not from the stored database.

    Use this when you need where symbols are trading *today*, rather than the
    last settled daily close the database tools return. Ideal for a quick
    "where is my watchlist trading now?" across many names at once.

    Args:
        tickers: Symbols to quote, e.g. ["NVDA", "AMD", "SMH", "^SOX"]. Up to
            120 per call. Yahoo symbols, case-insensitive (indexes start "^",
            futures end "=F", crypto is PAIR-USD).

    Returns an envelope {fetched_at, source, requested, ok, quotes} where each
    quote carries price, previous_close, change, change_pct, open, day_high,
    day_low and currency. Prices are delayed ~15 min; when a market is closed
    the price reflects that session's last print. A symbol that can't be fetched
    comes back with its ``error`` set instead of prices, so one bad ticker never
    fails the whole batch.
    """
    from intraday import snapshot  # lazy import: only needs yfinance when called

    return snapshot(tickers)


# --------------------------------------------------------------------------- #
# Guarded raw-SQL escape hatch
# --------------------------------------------------------------------------- #
@mcp.tool()
def run_sql(query: str) -> list[dict[str, Any]]:
    """Run one read-only SQL query (SELECT or WITH) against the database.

    Use this for ad-hoc analysis the curated tools don't cover. Only a single
    read-only statement is permitted; anything that writes or modifies schema
    is rejected. At most 1000 rows are returned.

    Schema:
      daily_prices(ticker, date, open, high, low, close, adj_close, volume,
                   source, updated_at)   -- one row per (ticker, date)
      spot_quotes(ticker, captured_at, price, previous_close, currency, source)

    Example:
      SELECT ticker, MAX(close) AS hi FROM daily_prices GROUP BY ticker
      ORDER BY hi DESC LIMIT 5
    """
    safe = _validate_select(query)
    with _connect() as conn:
        cur = conn.execute(safe)
        return _rows(cur)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="MCP server for the daily-prices DB.")
    ap.add_argument(
        "--transport",
        choices=["streamable-http", "sse", "stdio"],
        default="streamable-http",
        help="MCP transport (default: streamable-http for Claude.ai connectors).",
    )
    ap.add_argument("--host", default="0.0.0.0", help="Bind host for HTTP transports.")
    ap.add_argument("--port", type=int, default=8000, help="Bind port for HTTP transports.")
    args = ap.parse_args()

    if args.transport in ("streamable-http", "sse"):
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        if AUTH_TOKEN:
            if not _TOKEN_RE.match(AUTH_TOKEN):
                print(
                    "MCP_AUTH_TOKEN must be URL-safe (letters, digits, '-._~'). "
                    "Generate one with: python -c "
                    '"import secrets; print(secrets.token_urlsafe(32))"',
                    file=sys.stderr,
                )
                sys.exit(2)
            # Serve at a secret path; the full URL is the credential.
            mcp.settings.streamable_http_path = f"/{AUTH_TOKEN}/mcp"
            masked = AUTH_TOKEN[:4] + "…" + AUTH_TOKEN[-2:]
            print(
                f"[auth] endpoint protected by secret path /{masked}/mcp "
                "— give the full URL to Claude.ai, keep it private.",
                file=sys.stderr,
            )
        else:
            print(
                "[auth] WARNING: no MCP_AUTH_TOKEN set — endpoint is PUBLIC at "
                "/mcp. Anyone with the host can read your data. Set "
                "MCP_AUTH_TOKEN before exposing this to the internet.",
                file=sys.stderr,
            )
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
