#!/usr/bin/env python3
"""
Overnight global markets snapshot.

Fetches a compact "what happened while the US slept" board and writes
data/overnight.json for the dashboard to render each morning.

Coverage, in the order a US-based reader scans it:
  - US overnight   : equity index futures (the live US read pre-open)
  - Asia (closed)  : the sessions that finished a few hours ago
  - Europe (open)  : the sessions in progress right now
  - Rates/FX/Cmdty : 10Y yield, dollar, gold, oil
  - Crypto (24h)   : the only thing that traded straight through

Each item's `pct` is last vs previous close: for Asia that's the completed
session, for Europe the session so far, for US futures the overnight move off
the prior settle. Designed to run ~11:30 UTC on weekday mornings (7:30 ET).

Data source: Yahoo Finance via yfinance. Robust per-ticker: a bad symbol
degrades to null, it never sinks the run.
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timezone

import yfinance as yf

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "overnight.json")

# (ticker, display name). Groups render in this order.
BOARD = [
    ("US overnight (futures)", [
        ("ES=F", "S&P 500"),
        ("NQ=F", "Nasdaq 100"),
        ("YM=F", "Dow"),
        ("RTY=F", "Russell 2000"),
    ]),
    ("Asia (closed)", [
        ("^N225", "Nikkei 225 (Tokyo)"),
        ("^HSI", "Hang Seng (Hong Kong)"),
        ("^KS11", "KOSPI (Seoul)"),
        ("^TWII", "Taiwan Weighted"),
        ("000001.SS", "Shanghai Composite"),
    ]),
    ("Europe (open)", [
        ("^FTSE", "FTSE 100 (London)"),
        ("^GDAXI", "DAX (Frankfurt)"),
        ("^STOXX50E", "Euro Stoxx 50"),
        ("^FCHI", "CAC 40 (Paris)"),
    ]),
    ("Rates / FX / Commodities", [
        ("^TNX", "US 10Y yield"),
        ("DX-Y.NYB", "Dollar index"),
        ("GC=F", "Gold"),
        ("CL=F", "WTI crude"),
    ]),
    ("Crypto (24h)", [
        ("BTC-USD", "Bitcoin"),
        ("ETH-USD", "Ethereum"),
    ]),
]

ATTEMPTS = 3


def _f(v):
    try:
        if v is None:
            return None
        v = float(v)
        return None if math.isnan(v) else v
    except (TypeError, ValueError):
        return None


def quote(symbol):
    """Return (last, prev_close, currency) as robustly as possible."""
    tkr = yf.Ticker(symbol)
    last = prev = cur = None
    try:
        fi = tkr.fast_info
        last = _f(getattr(fi, "last_price", None) or fi["lastPrice"] if fi else None)
    except Exception:
        last = None
    try:
        fi = tkr.fast_info
        prev = _f(getattr(fi, "previous_close", None))
    except Exception:
        prev = None
    try:
        cur = tkr.fast_info.currency
    except Exception:
        cur = None
    # Fallback / repair from a short history window.
    if last is None or prev is None:
        try:
            h = tkr.history(period="5d", auto_adjust=False, actions=False)
            closes = [c for c in h["Close"].tolist() if c and not math.isnan(c)]
            if last is None and closes:
                last = _f(closes[-1])
            if prev is None and len(closes) >= 2:
                prev = _f(closes[-2])
        except Exception:
            pass
    return last, prev, cur


def quote_with_retry(symbol):
    for attempt in range(1, ATTEMPTS + 1):
        try:
            last, prev, cur = quote(symbol)
            if last is not None:
                return last, prev, cur
        except Exception:
            pass
        if attempt < ATTEMPTS:
            time.sleep(2 ** attempt)
    return None, None, None


def main():
    groups = []
    ok = 0
    for group_name, members in BOARD:
        items = []
        for sym, name in members:
            last, prev, cur = quote_with_retry(sym)
            pct = None
            if last is not None and prev not in (None, 0):
                pct = round((last / prev - 1.0) * 100, 3)
            if last is not None:
                ok += 1
            items.append({
                "ticker": sym, "name": name,
                "last": round(last, 4) if last is not None else None,
                "prev_close": round(prev, 4) if prev is not None else None,
                "pct": pct, "currency": cur,
            })
            time.sleep(0.4)
        groups.append({"group": group_name, "items": items})

    out = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Yahoo Finance via yfinance",
        "note": ("pct = last vs previous close. Asia = completed session; "
                 "Europe = session in progress; US futures = overnight vs prior "
                 "settle; crypto = trailing 24h."),
        "groups": groups,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(out, f, indent=2)
    total = sum(len(m) for _, m in BOARD)
    print(f"overnight: {ok}/{total} quotes ok -> {OUT}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
