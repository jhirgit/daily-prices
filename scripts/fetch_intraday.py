#!/usr/bin/env python3
"""Fetch intraday quotes from Finnhub for a list of tickers and write JSON.

Reads:
  FINNHUB_API_KEY  - Finnhub API token (repo secret)
  TICKERS          - comma/space/newline separated symbols, e.g. "AAPL,MSFT,NVDA"

Writes:
  data/intraday.json          - most recent snapshot
  data/history/<stamp>.json   - timestamped copy
  $GITHUB_STEP_SUMMARY        - Markdown table (when run in Actions)

Uses only the Python standard library (no pip install needed).

Note: data/latest.json belongs to the nightly daily-close export
(export_data.py) and is deliberately not touched here.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

FINNHUB_URL = "https://finnhub.io/api/v1/quote"


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def get_quote(symbol, token):
    params = urllib.parse.urlencode({"symbol": symbol, "token": token})
    with urllib.request.urlopen(f"{FINNHUB_URL}?{params}", timeout=15) as resp:
        return json.load(resp)


def main():
    token = os.environ.get("FINNHUB_API_KEY")
    if not token:
        sys.exit("FINNHUB_API_KEY is not set")

    raw = os.environ.get("TICKERS", "")
    tickers, seen = [], set()
    for t in raw.replace("\n", ",").replace(" ", ",").split(","):
        t = t.strip().upper()
        if t and t not in seen:
            seen.add(t)
            tickers.append(t)
    if not tickers:
        sys.exit("No tickers provided (set TICKERS, comma-separated)")

    quotes, errors = [], []
    for sym in tickers:
        try:
            q = get_quote(sym, token)
            # Finnhub returns t=0 for unknown symbols.
            if not q or q.get("t", 0) == 0:
                errors.append(sym)
                continue
            quotes.append({
                "ticker": sym,
                "price": q.get("c"),
                "change": q.get("d"),
                "change_pct": q.get("dp"),
                "open": q.get("o"),
                "high": q.get("h"),
                "low": q.get("l"),
                "prev_close": q.get("pc"),
                "quote_time": iso(q["t"]),
            })
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{sym} ({exc})")
        time.sleep(1)  # free tier allows 60 req/min; 1s/ticker stays safe

    payload = {
        "as_of": now_iso(),
        "source": "finnhub",
        "count": len(quotes),
        "requested": tickers,
        "errors": errors,
        "quotes": quotes,
    }

    os.makedirs("data/history", exist_ok=True)
    with open("data/intraday.json", "w") as f:
        json.dump(payload, f, indent=2)
    stamp = payload["as_of"].replace(":", "").replace("-", "")
    with open(f"data/history/{stamp}.json", "w") as f:
        json.dump(payload, f, indent=2)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        lines = [f"### Intraday prices - {payload['as_of']}", "",
                 "| Ticker | Price | Chg | Chg % |", "|---|---:|---:|---:|"]
        for q in quotes:
            lines.append(f"| {q['ticker']} | {q['price']} | {q['change']} | {q['change_pct']}% |")
        if errors:
            lines += ["", f"**Errors:** {', '.join(map(str, errors))}"]
        with open(summary_path, "a") as f:
            f.write("\n".join(lines) + "\n")

    print(json.dumps(payload, indent=2))
    if not quotes:
        sys.exit("No quotes fetched")


if __name__ == "__main__":
    main()
