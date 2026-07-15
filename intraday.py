#!/usr/bin/env python3
"""
On-demand intraday (delayed) quotes for a batch of tickers, fetched live from
Yahoo Finance via yfinance — NOT from the stored database.

Two entry points share one implementation:
  * the ``get_intraday_quotes`` MCP tool (see mcp_server.py) — how Claude.ai /
    cowork requests a batch of quotes through the connector.
  * this file as a CLI, for a Claude Code / cowork shell or a quick manual check:
        python intraday.py NVDA AMD SMH ^SOX
        python intraday.py --tickers-file tickers.txt --compact

Quotes are the delayed (~15 min) last trade; when a market is closed the price
reflects that session's last print. This module does no database I/O.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

# Safety cap: one call fetches at most this many symbols. The whole watchlist
# fits comfortably; the cap just bounds a runaway request.
MAX_TICKERS = 120

# Concurrency for the batch. Modest, to stay clear of Yahoo rate limits.
MAX_WORKERS = 8

PRICE_DP = 4


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _f(v):
    try:
        if v is None:
            return None
        v = float(v)
        return None if math.isnan(v) else v
    except (TypeError, ValueError):
        return None


def _round(v, dp=PRICE_DP):
    return round(v, dp) if isinstance(v, float) else v


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


def _quote_one(symbol: str) -> dict:
    import yfinance as yf  # local import: only needed when a quote is requested

    sym = symbol.strip().upper()
    try:
        fi = yf.Ticker(sym).fast_info
        last = _f(_fast_get(fi, "last_price", "lastPrice"))
        prev = _f(_fast_get(fi, "previous_close", "previousClose"))
        change = round(last - prev, PRICE_DP) if last is not None and prev is not None else None
        change_pct = (
            round((last - prev) / prev * 100, 2)
            if last is not None and prev not in (None, 0)
            else None
        )
        return {
            "ticker": sym,
            "price": _round(last),
            "previous_close": _round(prev),
            "change": change,
            "change_pct": change_pct,
            "open": _round(_f(_fast_get(fi, "open"))),
            "day_high": _round(_f(_fast_get(fi, "day_high", "dayHigh"))),
            "day_low": _round(_f(_fast_get(fi, "day_low", "dayLow"))),
            "currency": _fast_get(fi, "currency"),
            "error": None,
        }
    except Exception as e:  # noqa: BLE001 - isolate per-ticker failures
        return {"ticker": sym, "price": None, "error": str(e)[:200]}


def _dedupe(tickers) -> list[str]:
    out, seen = [], set()
    for t in tickers:
        s = str(t).strip().upper()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def fetch_quotes(tickers, max_workers: int = MAX_WORKERS) -> list[dict]:
    """Live delayed quotes for `tickers`, in input order.

    Per-ticker errors are captured in each row's ``error`` field rather than
    raising, so one bad symbol never sinks the batch.
    """
    syms = _dedupe(tickers)[:MAX_TICKERS]
    if not syms:
        return []
    workers = max(1, min(max_workers, len(syms)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(_quote_one, syms))


def snapshot(tickers) -> dict:
    """Timestamped envelope of a batch of quotes, suitable for returning to an LLM."""
    quotes = fetch_quotes(tickers)
    ok = sum(1 for q in quotes if q.get("error") is None)
    return {
        "fetched_at": _utc_now_iso(),
        "source": "Yahoo Finance via yfinance (delayed ~15 min; last session's price when markets are closed)",
        "requested": len(quotes),
        "ok": ok,
        "quotes": quotes,
    }


def _load_file(path: str) -> list[str]:
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            sym = line.split("#", 1)[0].strip()
            if sym:
                out.append(sym)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Live intraday (delayed) quotes for a batch of tickers."
    )
    ap.add_argument("tickers", nargs="*", help="Ticker symbols, e.g. NVDA AMD ^SOX")
    ap.add_argument("--tickers-file", help="Read symbols from a file (one per line, #-comments ok)")
    ap.add_argument("--compact", action="store_true", help="One line per quote instead of JSON")
    args = ap.parse_args()

    syms = list(args.tickers)
    if args.tickers_file:
        syms += _load_file(args.tickers_file)
    if not syms:
        ap.error("give at least one ticker (positional) or --tickers-file")

    snap = snapshot(syms)
    if args.compact:
        print(f"# {snap['fetched_at']}  ({snap['ok']}/{snap['requested']} ok)")
        for q in snap["quotes"]:
            if q.get("error"):
                print(f"  {q['ticker']:10s} ERROR {q['error']}")
                continue
            pct = f"{q['change_pct']:+.2f}%" if q["change_pct"] is not None else ""
            print(f"  {q['ticker']:10s} {str(q['price']):>12}  {pct}")
    else:
        print(json.dumps(snap, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
