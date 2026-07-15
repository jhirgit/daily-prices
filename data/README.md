# Published price data

These files are regenerated from `prices.db` on every pipeline run (see
`export_data.py`) so they can be read directly over a public URL — including by
**Claude.ai chat**: paste a raw link below into a conversation and Claude fetches
the live data. No server or auth required (the repository just needs to be
**public**).

| File | Raw URL | Contents |
|------|---------|----------|
| `latest.json` | https://raw.githubusercontent.com/jhirgit/daily-prices/main/data/latest.json | Compact snapshot: newest daily bar + newest delayed quote per ticker. Small — best for "what's the latest price of X". |
| `daily_prices.csv` | https://raw.githubusercontent.com/jhirgit/daily-prices/main/data/daily_prices.csv | Full history of settled OHLC bars. |
| `spot_quotes.csv` | https://raw.githubusercontent.com/jhirgit/daily-prices/main/data/spot_quotes.csv | Full history of delayed spot quotes. |

## Using it in Claude.ai

> Read https://raw.githubusercontent.com/jhirgit/daily-prices/main/data/latest.json
> and tell me how NVDA and AMD are doing.

Tips:
- `latest.json` is the cheapest to read (one row per ticker, ~30 KB). Point
  Claude here for quick lookups.
- The CSVs hold the full history for trend/comparison questions.
- `close` is the official settled daily close; `spot_price` is the delayed last
  trade captured when the job ran, so they can differ intraday.

## Freshness

The GitHub Actions job runs ~22:00 UTC each weekday, refreshes these files, and
commits them. GitHub's raw CDN caches for ~5 minutes, so a fetch is at most a
few minutes behind the latest commit. Prices are delayed, not real-time.

Data source: Yahoo Finance via [`yfinance`](https://github.com/ranaroussi/yfinance).
