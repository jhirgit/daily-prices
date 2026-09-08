# daily-prices

> **Visibility note (2026-09-01, backlog #15):** this repository is being taken **private**. Once it is, the raw
> `raw.githubusercontent.com` / jsDelivr URLs below stop working for anyone not logged in. The dashboard already
> reads through its same-origin proxy (`jr-dash` `/data/*`, behind Cloudflare Access); tooling on Jake's machine uses
> the local clone or `tools/jr_prices.py` (Access service token). Claude.ai chat cannot read this data directly any more.

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

## Technicals (`technicals.py`)

Computes regime-gated indicators from the stored daily bars and writes
`data/technicals.json` (~230KB) for the dashboard's Technicals tab. Pure
stdlib — no extra dependency beyond what `fetch_prices.py` already needs.

```powershell
python technicals.py            # writes data/technicals.json
python technicals.py --stdout   # print instead
python test_technicals.py       # test suite
```

It is built from two papers that disagree, and the disagreement is the design:

* **Chio 2022** (`arXiv:2206.12282`) — naked MACD(12,26,9) is close to a coin
  flip (win rate 0.37–0.41). Adding *price-pressure* information (RSI, MFI, or
  his volume-and-volatility indicator VPVMA) lifts it. But he never compares
  against buy-and-hold, across 2015–2021.
* **Hurwitz & Marwala 2011** (`arXiv:1110.3383`) — an indicator only works when
  its own assumption holds. MACD assumes a trend, RSI assumes a cycle, and those
  are mutually exclusive.

So the **regime is classified first** and the indicator family whose assumption
fails is muted rather than displayed, and **every strategy is scored against
buy-and-hold** over the same window.

### The regime gate is calibrated, not eyeballed

Thresholds come from a Monte Carlo against a random-walk null (20,000 paths,
90-session windows). Two results drove the design and are worth keeping in mind:

* `R² ≥ 0.50` is **worthless** as a trend test — **44.7% of pure random walks
  clear it**. The bar is set at `R² ≥ 0.85`, the p90 of the null.
* The **variance ratio does not detect cycles** at these horizons. A sinusoid is
  locally smooth, so cyclical data returns VR of 3.9–11.3 — far *above* 1, not
  below. VR is reported as a diagnostic only. What separates a cycle from a
  random walk is *smoothness*: a clean swing crosses its SMA20 ~4–6 times per
  100 sessions, a random walk chops across ~11 times.

The gate publishes its own error rates (`regime_error_rates`): ~10% of random
walks still get labelled trending, ~8% mean-reverting. Over 90 sessions a random
walk genuinely can look like a trend — that is a fact about markets, not a bug.

### Two corrections to Chio's printed VPVMA spec

Both look like typesetting errors and are documented in `vpvma()`:

* eq 4.2-6 writes `EMA(SVWMA * DV, s)` for the **long** leg, which would make it
  identical to the short leg. `LVWMA` is used.
* Table 8's sell rule repeats the buy rule's `VPVMA(t-1) <= VPVMAS(t-1)`; taken
  literally the strategy can never sell. Mirrored to `>=`.

### Returns use adjusted closes, fills use raw closes

Momentum and relative strength run on `adj_close` so dividend payers (the gold
sleeve) are not understated against non-payers (the semis). The backtest uses
raw `close`, because Chio's argument that "no one can buy at Adj Close" is right
for simulating fills — and wrong for comparing total return across names.

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

## Options flow (`options_flow.py`)

A once-a-day chain snapshot for every optionable name in `tickers.txt`, over the
**three nearest monthly expiries** (nearest three of any kind if a name lists
fewer than three monthlies). SPEC-62 phase 1. Source is **yfinance** — the same
OPRA end-of-day figures Schwab returns, but Schwab's token grants account read,
expires every 7 days behind an interactive browser login, and carries no
per-contract IV, none of which survives an unattended job in a **public** repo.

**Read this first:** a daily snapshot is *positioning arithmetic, not flow*. It
cannot see the trade tape, the aggressor side, or whether a print opened or
closed a position — so it never says "bullish sweep". Open interest is the prior
session's OCC figure; volume is the session's own. Display-only, confidence LOW
to MODERATE, alert-never-action.

### Artifacts

| File | What it is |
|---|---|
| `data/options_flow.json` | the snapshot: one row per name, ~250 KB |
| `data/options_iv_hist.json` | the IV history: one `iv30` float per name per session, self-capping at 252 |

```
{"generated_at": <utc>, "as_of": <session>, "source": "yfinance", "expiries": 3,
 "thresholds": {...}, "iv_history": "options_iv_hist.json",
 "universe_n": 195, "count": 195, "elapsed_s": 69.8,
 "skipped": {"BTC-USD": "crypto", "IFNNY": "no listed options"},
 "names": {"NVDA": {
    "spot":, "n":,                              # contracts across the 3 expiries
    "cv":, "pv":, "pc_vol":,                    # call/put volume and the skew
    "coi":, "poi":, "pc_oi":,                   # call/put open interest and the skew
    "d_coi":, "d_poi":,                         # day-over-day OI change
    "voloi_max":, "voloi_max_c":,               # largest vol/OI and its contract
    "iv30":, "iv60":, "kink":,                  # vol POINTS (41.2 == 41.2%)
    "iv_rank":, "iv_pct":, "iv_rank_n":, "iv_state":,
    "kink_event": {"date":, "estimated":},      # the print the kink is pricing, if known
    "n_unusual":, "flags": [],
    "top": [{"s","t","e","k","v","oi","iv","px","n","d_oi"}],   # 5 by premium notional
    "oi_top": {"<contract>": <oi>}}}}                           # 12 largest, the carried state
```

### The signals

| Signal | Definition | Flag |
|---|---|---|
| **vol/OI** | `volume / open interest`, **null** when OI is 0 — never `0`, never `inf` | — |
| **premium notional** | `volume x 100 x mid` (mid when the book is two-sided, else last) | — |
| **UNUSUAL** | `vol/OI > 3` **AND** `volume > 500` **AND** `notional > $1M` — all three ANDed | `UNUSUAL` |
| **P/C skew** | `pc_vol = put volume / call volume`, likewise `pc_oi` | `SKEW` when `> 1.5` or `< 0.40` **and** total volume `> 1,000` |
| **OI delta** | `d_coi` / `d_poi` against yesterday's artifact, plus `d_oi` per contract on the 12 largest-OI contracts carried forward | — |
| **IV rank** | `(iv30 - min) / (max - min)` over the trailing history, plus `iv_pct`, the share of days below | `IV-HIGH` `> 0.8`, `IV-LOW` `< 0.2`, **only once `iv_state` is `full`** |
| **Term kink** | `iv30 - iv60` in vol points, cross-referenced against `data/earnings_dates.json` | `EVENT` when `> +5` |

The three legs of **UNUSUAL** are ANDed for a reason. The ratio alone fires
constantly on illiquid far-OTM strikes where open interest is single digits; the
500-contract floor removes single prints, and the $1M premium floor removes penny
lottery tickets that clear a ratio test trivially. `3x` rather than the `2x`
retail screens use keeps the false-positive rate low across a 195-name universe
scanned unattended.

### How to read a flag

- **`UNUSUAL`** — at least one contract traded more today than its entire
  standing open interest, in size, for real money. That is *new positioning
  rather than churn*. It does **not** say which way, or by whom. Open the `top`
  list and look at the strike and the expiry before drawing any conclusion.
- **`SKEW`** — the day's volume ran heavily one-sided. On an index ETF this is
  usually hedging, not a view; on a single name it is worth a glance.
- **`EVENT`** — the front expiry is pricing something the back expiry is not.
  If `kink_event` is populated, that something is a scheduled print and the flag
  is *explained, not interesting*. If it is `null`, nobody here knows what the
  front month is worried about.
- **`IV-HIGH` / `IV-LOW`** — the name's own 30-day vol against its own trailing
  year. Never against another name's.
- **A blank `iv_rank` is not a zero.** `iv_state` says why: `null` below 60
  sessions of history, `provisional` from 60 to 251, `full` at 252. A rank
  computed over three weeks is a number that means nothing, and shipping it
  would be worse than shipping a blank — which is why phase 1 ships alone and
  starts accumulating.

### State, and the size of it

The **only** state the job carries is its own previous output: it reads
`data/options_flow.json` before overwriting it, and the 12-entry `oi_top` map is
all the open-interest delta needs. No database, nothing to prune, no second
artifact to keep in sync by hand.

The IV history is a separate file because it cannot be anything else: 252 floats
per name projects to **~342 KB**, which inside the snapshot would put the payload
near 590 KB and breach the hard cap outright. It stores one float per name per
session against a shared date axis, `null` where a name had no reading, and trims
itself to 252 sessions.

Measured on a full run, 2026-09-08: **195 optionable names of 222 lines**,
27 skipped, **69.8 s** (0.36 s/name), payload **249,585 B** at ~1,270 B/name
(summary 326 + `oi_top` 339 + `top` 647). The guard **warns above 200 KB and
FAILS above 300 KB** — so it warns today. SPEC-62 section 5 budgeted 850 B/name,
but that estimate is not reachable with the ten-field `top` entry the same
section prescribes. The schema ships as specified rather than quietly trimmed;
headroom before the hard cap is ~236 names.

### It fails loud, after committing

`continue-on-error` on the emit step, then the commit, then an explicit `exit 1`
— the `earnings_detect.py` pattern. The board gets to see the partial data *and*
the failure still surfaces. It goes red when coverage drops below **80%** of the
optionable universe, when the optionable universe itself collapses below 80% of
yesterday's, when `as_of` trails the last settled session in `data/latest.json`,
or when the payload breaches 300 KB. `_alert-failure.yml` then opens an issue.

### Running it

```powershell
python options_flow.py                      # both artifacts
python options_flow.py --tickers NVDA,MU    # a small live run
python options_flow.py --dry-run            # compute and print, write nothing
python options_flow.py --verify             # parity against the frozen fixture, no network
python options_flow.py --freeze             # re-freeze the fixture after an intended change
python -m unittest test_options_flow        # 52 offline tests
```

`--verify` recomputes every signal from `data/fixtures/options_flow_fixture.json`
— a **synthetic** chain set, hand-authored so each name sits on a threshold, not
a capture of live data. The workflow runs it before the live emit: if the
arithmetic has drifted, nothing downstream is worth committing.

### The job

A separate `options` job in `daily-prices.yml` on the **same 22:00 UTC trigger** —
no new cron, no new Worker schedule, `cron-dispatch` already dispatches this
workflow. It needs `[fetch, earnings-dates]` because it is the third job pushing
to this branch and `needs:` is what serialises those pushes; the `always()` guard
keeps it running when `earnings-dates` goes red on an overdue print. Roughly
**+65 Actions min/month** (~3 billable min x 21 weekdays).

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
