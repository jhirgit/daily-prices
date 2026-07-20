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

Two ways to make this data available to Claude.ai chat:

**1. Public URLs (simplest).** If this repo is **public**, the pipeline also
writes text exports under [`data/`](data/) that Claude can fetch directly — paste
a raw link into a chat and ask away. No server, no auth. See
[`data/README.md`](data/README.md).

```
Read https://raw.githubusercontent.com/jhirgit/daily-prices/main/data/latest.json
and tell me how NVDA and AMD are doing.
```

Generate the exports locally with `python export_data.py` (writes `data/`).

**2. MCP connector.** `mcp_server.py` exposes the database to Claude.ai (or
Claude Desktop) as a read-only [MCP](https://modelcontextprotocol.io) connector
with query tools, so you can ask *"how did NVDA do this week?"* and Claude
queries `prices.db`. It also has a live `get_intraday_quotes` tool that fetches
current delayed prices for a batch of tickers on demand (bypassing the DB), so
Claude can answer *"where is my watchlist trading right now?"*. Needs hosting;
see **[MCP.md](MCP.md)**.

```bash
pip install -r requirements-mcp.txt
export MCP_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python mcp_server.py --port 8000      # streamable HTTP at /<token>/mcp, for Claude.ai
```

**Intraday, no hosting.** In a Claude Code / cowork shell (or by hand) you can
pull a batch of live quotes with the standalone CLI — no server required:

```bash
python intraday.py NVDA AMD SMH ^SOX --compact
python intraday.py --tickers-file tickers.txt        # whole watchlist, JSON
```

## On-demand intraday service (GitHub Actions + Finnhub)

For real-time quotes without hosting anything: the **Intraday Prices** workflow
(`.github/workflows/intraday-prices.yml`) runs on a cron every 20 minutes during
US market hours (fetching the Finnhub-compatible watchlist symbols), and is also
a `workflow_dispatch` that anyone with repo access (including a Claude/Cowork
session) can trigger with a comma-separated ticker list — leave the input empty
to fetch the watchlist. It fetches live quotes from
[Finnhub](https://finnhub.io/) via `scripts/fetch_intraday.py` (stdlib only, no
dependencies), then commits the result back to `main`:

- `data/intraday.json` — the latest snapshot, also served as a plain JSON
  endpoint: `https://raw.githubusercontent.com/jhirgit/daily-prices/main/data/intraday.json`
  (`data/latest.json` stays owned by the nightly daily-close export)
- `data/history/<stamp>.json` — a timestamped copy per run (pruned to the
  newest 20; older snapshots remain in git history)
- a Markdown price table in the workflow run's job summary

Setup (one time): add a Finnhub token as repo secret **`FINNHUB_API_KEY`**
(Settings -> Secrets and variables -> Actions).

Trigger it from the UI (Actions -> Intraday Prices -> Run workflow) or the CLI:

```bash
gh workflow run "Intraday Prices" -f tickers=AAPL,MSFT,NVDA
```

Caveats: outside US market hours Finnhub returns the last close (`price` ==
`prev_close` is expected, not a bug); unknown symbols land in the `errors`
array; the free tier allows 60 req/min, so the script sleeps 1s per ticker.

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
