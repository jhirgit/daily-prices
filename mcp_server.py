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

  # Remote connector (host this, then add the URL in Claude.ai -> Connectors).
  # A non-loopback host requires MCP_AUTH_TOKEN (or --allow-public-unauthenticated).
  MCP_AUTH_TOKEN=... python mcp_server.py --host 0.0.0.0 --port 8000

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
import time
from contextlib import closing
from typing import Any

from mcp.server.fastmcp import FastMCP

HERE = os.path.dirname(os.path.abspath(__file__))
# Allow overriding the DB location for hosted deployments (e.g. a mounted volume).
DB_PATH = os.environ.get("PRICES_DB", os.path.join(HERE, "prices.db"))

# Hard cap on rows returned by any single tool call, to keep responses bounded.
MAX_ROWS = 1000

# Max tickers per compare_performance call (each one costs two queries).
MAX_COMPARE_TICKERS = 25


def _env_float(name: str, default: float) -> float:
    try:
        val = float(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return val if val > 0 else default


# Wall-clock budget for one tool call's SQL work. A progress handler aborts the
# statement once it is exceeded, so a pathological query (e.g. a recursive CTE
# or a huge cross join through run_sql) can't pin a CPU indefinitely.
QUERY_TIMEOUT_S = _env_float("MCP_QUERY_TIMEOUT_S", 5.0)

# How many SQLite VM instructions run between deadline checks.
_PROGRESS_STEPS = 10_000

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
def _connect(timeout_s: float | None = None) -> sqlite3.Connection:
    """Open prices.db strictly read-only via a file: URI.

    Every statement on the returned connection is aborted (sqlite3
    OperationalError "interrupted") once ``timeout_s`` seconds (default
    QUERY_TIMEOUT_S) have elapsed since the connection was opened. Callers
    should wrap it in ``contextlib.closing`` so the handle is released.
    """
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(
            f"Database not found at {DB_PATH}. Run fetch_prices.py first, "
            "or set the PRICES_DB environment variable."
        )
    uri = f"file:{DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    deadline = time.monotonic() + (QUERY_TIMEOUT_S if timeout_s is None else timeout_s)
    # A non-zero return from the handler aborts the running statement.
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), _PROGRESS_STEPS)
    return conn


def _query_timeout_error(exc: sqlite3.OperationalError) -> Exception:
    """Translate a progress-handler abort into a clear error for the client."""
    if "interrupted" in str(exc).lower():
        return ValueError(
            f"Query exceeded the {QUERY_TIMEOUT_S:g}s time limit and was aborted. "
            "Narrow it (add WHERE/LIMIT, avoid large joins or recursive CTEs)."
        )
    return exc


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
    with closing(_connect()) as conn:
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
    with closing(_connect()) as conn:
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
    with closing(_connect()) as conn:
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
    At most 25 tickers per call.
    """
    if len(tickers) > MAX_COMPARE_TICKERS:
        raise ValueError(
            f"Too many tickers ({len(tickers)}); at most {MAX_COMPARE_TICKERS} per call."
        )
    out: list[dict[str, Any]] = []
    with closing(_connect()) as conn:
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
    is rejected. At most 1000 rows are returned, and a query that runs longer
    than the server's time limit (default 5s) is aborted.

    Schema:
      daily_prices(ticker, date, open, high, low, close, adj_close, volume,
                   source, updated_at)   -- one row per (ticker, date)
      spot_quotes(ticker, captured_at, price, previous_close, currency, source)

    Example:
      SELECT ticker, MAX(close) AS hi FROM daily_prices GROUP BY ticker
      ORDER BY hi DESC LIMIT 5
    """
    safe = _validate_select(query)
    with closing(_connect()) as conn:
        try:
            cur = conn.execute(safe)
            return _rows(cur)
        except sqlite3.OperationalError as exc:
            raise _query_timeout_error(exc) from exc


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _startup_error(transport: str, host: str, allow_public: bool) -> str | None:
    """Return why the server must refuse to start, or None if it may start.

    Fails closed: an HTTP transport bound beyond loopback needs MCP_AUTH_TOKEN
    unless the operator explicitly opts out with --allow-public-unauthenticated.
    The secret path is only wired for streamable-http, so sse + token is refused
    rather than silently serving sse at its public default paths.
    """
    if transport not in ("streamable-http", "sse"):
        return None
    if AUTH_TOKEN:
        if not _TOKEN_RE.match(AUTH_TOKEN):
            return (
                "MCP_AUTH_TOKEN must be URL-safe (letters, digits, '-._~'). "
                "Generate one with: python -c "
                '"import secrets; print(secrets.token_urlsafe(32))"'
            )
        if transport == "sse":
            return (
                "MCP_AUTH_TOKEN protects only the streamable-http endpoint; the sse "
                "transport would still be served at the public /sse and /messages/ "
                "paths. Use --transport streamable-http (the default)."
            )
        return None
    if host.strip().lower() not in _LOOPBACK_HOSTS and not allow_public:
        return (
            f"Refusing to serve an unauthenticated endpoint on host {host!r}: "
            "MCP_AUTH_TOKEN is not set. Set MCP_AUTH_TOKEN, bind to 127.0.0.1, "
            "or pass --allow-public-unauthenticated if you really mean it."
        )
    return None


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="MCP server for the daily-prices DB.")
    ap.add_argument(
        "--transport",
        choices=["streamable-http", "sse", "stdio"],
        default="streamable-http",
        help="MCP transport (default: streamable-http for Claude.ai connectors).",
    )
    ap.add_argument("--host", default="0.0.0.0", help="Bind host for HTTP transports.")
    ap.add_argument("--port", type=int, default=8000, help="Bind port for HTTP transports.")
    ap.add_argument(
        "--allow-public-unauthenticated",
        action="store_true",
        help="Allow serving without MCP_AUTH_TOKEN on a non-loopback host "
        "(the endpoint is then readable by anyone who can reach it).",
    )
    args = ap.parse_args(argv)

    err = _startup_error(args.transport, args.host, args.allow_public_unauthenticated)
    if err:
        print(f"[auth] ERROR: {err}", file=sys.stderr)
        sys.exit(2)

    if args.transport in ("streamable-http", "sse"):
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        if AUTH_TOKEN:
            # Serve at a secret path; the full URL is the credential.
            # (_startup_error guarantees the transport is streamable-http here.)
            mcp.settings.streamable_http_path = f"/{AUTH_TOKEN}/mcp"
            masked = AUTH_TOKEN[:4] + "…" + AUTH_TOKEN[-2:]
            print(
                f"[auth] endpoint protected by secret path /{masked}/mcp "
                "— give the full URL to Claude.ai, keep it private.",
                file=sys.stderr,
            )
        else:
            path = "/sse" if args.transport == "sse" else "/mcp"
            print(
                f"[auth] WARNING: no MCP_AUTH_TOKEN set — endpoint is "
                f"UNAUTHENTICATED at {path} on {args.host}. Anyone who can reach "
                "the host can read your data. Set MCP_AUTH_TOKEN before exposing "
                "this to the internet.",
                file=sys.stderr,
            )
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
