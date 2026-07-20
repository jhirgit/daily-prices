# How to get price data from this repo (instructions for Claude sessions)

This repo (`jhirgit/daily-prices`) provides two kinds of price data. Pick the
right one for the question being asked.

## 1. End-of-day history — just read a URL, nothing to trigger

Updated automatically each weekday ~23:05 UTC. Use for anything about closes,
history, trends, or "how did X do this week/month".

- `https://raw.githubusercontent.com/jhirgit/daily-prices/main/data/latest.json`
  — newest daily bar + newest delayed spot quote per ticker (compact; start here)
- `https://raw.githubusercontent.com/jhirgit/daily-prices/main/data/daily_prices.csv`
  — full daily OHLC + adj_close + volume history, all tickers
- `https://raw.githubusercontent.com/jhirgit/daily-prices/main/data/spot_quotes.csv`
  — history of delayed spot snapshots

## 2. Live intraday quotes

Use for "where is X trading right now".

**Watchlist names, during US market hours: just read the URL.** A cron runs the
intraday fetch every 20 minutes (13:30–20:00 UTC, weekdays) over the watchlist,
so `https://raw.githubusercontent.com/jhirgit/daily-prices/main/data/intraday.json`
is at most ~20–25 minutes stale with **no auth needed**. Check `as_of` to see
how fresh it is.

**Arbitrary tickers, or fresher-than-cron: dispatch the workflow.** Requires
GitHub access to this repo.

1. Dispatch the GitHub Actions workflow **`Intraday Prices`**
   (`.github/workflows/intraday-prices.yml`) on `jhirgit/daily-prices`, branch
   `main`, with input `tickers` = comma-separated list (e.g. `NVDA,MU,SMH`).
2. Wait for the run to complete — typically ~45–60 s (setup plus ~1 s per ticker).
3. Read `https://raw.githubusercontent.com/jhirgit/daily-prices/main/data/intraday.json`
   and **check `as_of` is newer than your dispatch time**. raw.githubusercontent
   caches for up to ~5 minutes, so if `as_of` is stale, either retry after a
   minute or read `data/intraday.json` via the GitHub API contents endpoint
   (uncached) instead.

### Interpreting intraday.json

- `quotes[]`: `price`, `change`, `change_pct`, `open`/`high`/`low`,
  `prev_close`, `quote_time` (UTC) per ticker.
- Outside US market hours `price == prev_close` is expected (last close), not a bug.
- Unknown/unsupported symbols land in `errors[]`, not `quotes[]`.
- Source is Finnhub free tier: US-listed symbols work; indices (`^SOX`) and
  most non-US listings (`.KS`, `.T`) generally do not — use the end-of-day
  data or Yahoo-based tools for those.

## Which tickers exist here?

The end-of-day watchlist is `tickers.txt` (one Yahoo symbol per line). The
intraday workflow accepts any symbols Finnhub supports — it is not limited to
the watchlist.
