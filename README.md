# daily-prices

A tiny daily job that records **open/high/low/close**, **adjusted close**, **volume**,
and a **delayed spot quote** for a watchlist of tickers into a **SQLite** database —
so you accumulate a price history for market analysis.

Data source: **Yahoo Finance** via [`yfinance`](https://github.com/ranaroussi/yfinance).

> On "Google Finance": Google retired its public Finance API years ago. The only
> sanctioned route to Google's numbers is the `GOOGLEFINANCE()` Sheets function,
> which is awkward to automate. Yahoo via `yfinance` gives equivalent OHLC + a
> ~15-min-delayed quote with no API key, so it's used here. Swapping the source
> later only means editing `process_ticker()` in `fetch_prices.py`.

## What it stores

**`daily_prices`** — one settled row per `(ticker, date)`:

| ticker | date | open | high | low | close | adj_close | volume | source | updated_at |
|--------|------|------|------|-----|-------|-----------|--------|--------|------------|

**`spot_quotes`** — one delayed snapshot per `(ticker, captured_at)`:

| ticker | captured_at | price | previous_close | currency | source |
|--------|-------------|-------|----------------|----------|--------|

`close` is the official daily close; `spot_quotes.price` is the (delayed) last
trade at the moment the job ran — distinct values while the market is open.

## Configure

Edit **`tickers.txt`** — one Yahoo symbol per line, `#` comments allowed:

```
SPY      # benchmark
NVDA
BRK-B    # use dashes, not dots
^GSPC    # an index
BTC-USD  # crypto
```

## Run locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python fetch_prices.py
```

Creates / updates `prices.db` in this folder. Re-running the same day is safe —
daily bars upsert by `(ticker, date)`, and the lookback window backfills any
days the job missed (weekends, holidays, outages).

Custom paths:

```powershell
python fetch_prices.py --tickers mylist.txt --db C:\data\prices.db
```

## Run daily on GitHub Actions

`.github/workflows/daily-prices.yml` runs the script at **22:00 UTC on weekdays**
and commits the updated `prices.db` back to the repo. To enable it:

1. Push **this folder as the repository root** (the workflow expects
   `fetch_prices.py` and `requirements.txt` at the root, and GitHub only runs
   workflows from `.github/workflows/` at the repo root):
   ```powershell
   cd daily-prices
   git init
   git add .
   git commit -m "Initial commit: daily price fetcher"
   git branch -M main
   git remote add origin https://github.com/<you>/daily-prices.git
   git push -u origin main
   ```
2. In the repo: **Settings -> Actions -> General -> Workflow permissions ->**
   enable **Read and write permissions** (lets the job push the updated DB).
3. **Actions** tab -> **Daily Prices** -> **Run workflow** to test immediately
   (don't wait for the cron). The scheduled run then fires each weekday.

To change the time, edit the `cron:` line (it's in **UTC**).

## Ask Claude about your prices

`mcp_server.py` exposes this database to Claude.ai chat (or Claude Desktop) as a
read-only [MCP](https://modelcontextprotocol.io) connector, so you can ask
*"how did NVDA do this week?"* and Claude queries `prices.db` for you. See
**[MCP.md](MCP.md)** for the tools and how to connect.

```bash
pip install -r requirements-mcp.txt
export MCP_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python mcp_server.py --port 8000      # streamable HTTP at /<token>/mcp, for Claude.ai
```

## Querying the data

```powershell
sqlite3 prices.db "SELECT date, close FROM daily_prices WHERE ticker='NVDA' ORDER BY date DESC LIMIT 10;"
```

```python
import sqlite3, pandas as pd
con = sqlite3.connect("prices.db")
df = pd.read_sql("SELECT * FROM daily_prices WHERE ticker = 'NVDA' ORDER BY date", con)
```

## Caveats

- **Delayed, not real-time.** The spot quote lags ~15 min; daily bars settle
  shortly after the close.
- **Yahoo is unofficial.** It's free and reliable enough for a personal history,
  but not an SLA-backed feed. A bad symbol is skipped (logged `[FAIL]`); the job
  only fails if *every* ticker fails.
- **Repo growth.** Each run commits a new copy of the binary `prices.db`, so git
  history grows over time. Fine for years at a modest watchlist; if it ever
  bloats, squash history or migrate the DB to a release asset.
