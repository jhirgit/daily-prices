# Published price data

> **Visibility note (2026-09-01, backlog #15):** this repository is being taken **private**. Once it is, the raw
> `raw.githubusercontent.com` / jsDelivr URLs below stop working for anyone not logged in. The dashboard already
> reads through its same-origin proxy (`jr-dash` `/data/*`, behind Cloudflare Access); tooling on Jake's machine uses
> the local clone or `tools/jr_prices.py` (Access service token). Claude.ai chat cannot read this data directly any more.

These files are regenerated from `prices.db` on every pipeline run (see
`export_data.py`) so they can be read directly over a public URL — including by
**Claude.ai chat**: paste a raw link below into a conversation and Claude fetches
the live data. No server or auth required (the repository just needs to be
**public**).

| File | Raw URL | Contents |
|------|---------|----------|
| `latest.json` | https://raw.githubusercontent.com/jhirgit/daily-prices/main/data/latest.json | Compact snapshot: newest daily bar + newest delayed quote per ticker. Small — best for "what's the latest price of X". |
| `daily_prices.csv.gz` | https://raw.githubusercontent.com/jhirgit/daily-prices/main/data/daily_prices.csv.gz | Full history of settled OHLC bars. **gzip-compressed** (~4.7 MB; ~17.5 MB uncompressed) — gunzip before parsing (browser: `DecompressionStream`). |
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

<!-- B1 basket data (2026-11-20): schema notes for the rotation-ladder basket payloads. -->
## Basket payloads: what is inside each sector-ladder row (schema notes, 2026-11-20)

**`rotation_members.json`** (`rotation_members.py`, nightly). Additions on 2026-11-20:

| Field | Meaning |
|---|---|
| every `r` / `c` dict | gains `r252`, the one-year window; the row's own `R.r252` is `technicals.json` `regime.ladder_r252[T]` |
| `rows[T].holdings[i].a50`, `.a200` | true / false / null: the line's last close above its own 50- / 200-session simple average, in its own currency |
| `rows[T].shape.basis`, `n_lines`, `w_lines` | which lines the block uses (listed lines; plus the N-PORT names priced in a top-10 row's rest line), how many, and their share of the fund |
| `rows[T].shape.conc` | `top5_w`, `top10_w`; `eff_n` = 1/sum(w^2) with unlisted weight counted as perfectly spread (upper bound), `eff_n_min` with it spread over lines as heavy as the lightest listed (lower bound); equal when every line is listed |
| `rows[T].shape.trend.above50` / `.above200` | `{n, n_up, w_up}`: lines whose average can be read, how many sit above it, their share of that weight |
| `rows[T].shape.win[r5..r252]` | `explained` (sum of the lines' contributions), `resid` (= R - explained), `n`, `n_up`, `w_up`, `top1_share`, `top2_share` (largest one / two contributions in the direction of the move over the move; above 1 when the others net the other way), `ex_top2` (the other lines' return), `disp_w`, `disp_eq`, `iqr` |
| `issuer_files` | per issuer (`globalx`, `firsttrust`, `amplify`, `invesco`): `name`, `funds`, `ok`, `failed`, `status` ok / partial / failed. `health.py` degrades on `failed` |
| `store` | the constituents closes store (`constituents_store.py`; Actions cache, never committed): `store` hit / miss_rebuilt / partial, `wanted`, `appended`, `rebased`, `refetched`, `new`, `served_from_store` (Yahoo failed the symbol today), `failed`, `bytes`, `warning` past 25 MB. `health.py` warns on partial or a warning |

Tiers: `issuer_full` now also covers the First Trust (CIBR FDN GRID), Amplify (HACK SILJ) and Invesco (QQQ)
rows, from each issuer's daily file; the raw files are read in memory and never committed. Global X's rows
(COPX LIT URA DRIV FINX PAVE BOTZ DTCR) stay on Yahoo's top 10 + SEC N-PORT: its file forbids reproduction,
redistribution and derivative works, so it is switched off (`rotation_members.ISSUER_OFF`). `rotation_deep_holdings.json` keeps every line of each N-PORT filing.

**`rotation_fundamentals.json`** (`basket_fundamentals.py`, nightly after `rotation_members.py`):

| Field | Meaning |
|---|---|
| `generated_at`, `asof` | when written; the session `rotation_members.json` spoke for |
| `status`, `error` | `ok` / `partial` (no last closes, or many SEC fetches failed) / `failed` (no rows); `error` names every problem, including companies still waiting for an SEC fetch |
| `rules` | `cover_min` 0.70, `stale_days` 200, `tax_roic` 0.21 |
| `store` | the company store: `companies`, `fresh`, `wanted`, `due`, `fetched`, `failed`, `waiting` |
| `calls`, `run_s` | SEC requests this run; wall time |
| `rows[T].lines_source` | which book: the issuer file or basket members (with date), the whole SEC N-PORT book (with its period), or Yahoo's top 10 only |
| `rows[T].coverage_w`, `n_lines`, `n_companies` | share of the row's weight in covered companies (by CIK), lines, companies |
| `rows[T].uncovered` | `{reason: share of weight}`: listed outside the US; not an SEC filer; no usable SEC financials; SEC figures older than 200 days (annual filer); no price or share count; no FX rate |
| `rows[T].shown`, `why` | the composite is shown only at `coverage_w >= 0.70`; otherwise `m` is null and `why` says what share is covered and the largest gap |
| `rows[T].m` | `pe` (weighted harmonic, losses in; null with `pe_nm` = "aggregate loss"), `earn_yield`, `pe_median_profitable`, `loss_w`, `loss_n`, `ev_sales`, `ev_ebit`, `ev_ebitda`, `fcf_yield`, `div_yield`, `buyback_yield`, `rev_growth`, `earn_growth`, `op_margin`, `op_margin_prev`, `nd_ebitda`, `net_cash_w`, `int_cover_median`, `int_cover_n`, `capex_sales`, `roic`, `share_change`, `pb`, `fin_w` (share in banks and insurers, which enter P/E, P/B and dividend yield only). Fractions, not percent |
| `rows[T].vs_spy` | `pe`, `ev_sales`, `ev_ebit`, `fcf_yield`, `pb` each divided by SPY's own composite |
| `rows[T].per_line` | `{symbol: [own P/E or null for a loss, revenue growth y/y, TTM end]}` for the covered companies |

**`basket_companies.json`** (committed, the composites' company store): one compact record per CIK in `fields`
order (latest TTM, the TTM four quarters earlier, share counts, dividends paid, buybacks, interest expense; money
in millions of the filing currency), built by `fundamentals.build_company` from SEC companyfacts. A company is
fetched again only when its latest quarter is more than 130 days old (weekly while waiting; monthly for an annual
filer), heaviest weight first, inside a 360-second budget.
**History (B1 stage 5, one-off back-fill).** `basket_history.py`, dispatched by hand
(`.github/workflows/basket-history.yml`), writes **`rotation_fundamentals_history.json`**: per row `book` (which
books: the fund's N-PORT, IVV's for SPY, or today's basket members), `n` (quarters with a composite), and `q[]` =
`{date, public (the N-PORT's filing date), cov, m (pe, earn_yield, ev_sales, ev_ebit, ev_ebitda, fcf_yield, pb,
rev_growth, op_margin, op_margin_prev, nd_ebitda, loss_w; null below 70% coverage), gap}`; plus `status`
complete / partial and `progress`. Each company's figures are as known on the date (facts filed by then only).
Its inputs, kept between dispatches: `basket_history/nport_books.json.gz`, `basket_history/company_vintages.json.gz`.
The nightly `rotation_fundamentals.json` then carries `rows[T].history` = `{n, from, to, book, min_quarters,
pct: {pe, ev_sales, ev_ebit, fcf_yield, pb}, pct_vs_spy: {...}, why, status}`: today's value as the share of the
row's own quarters at or below it (and of its ratio to SPY's), null under 16 quarters.

**Declared splits.** `fundamentals.DECLARED_SPLITS` (ticker -> [(effective date, ratio)]) rescales share counts
dated before a split no filing reports yet (SNDK 8-for-1, 2026-11-19); `fundamentals.json` records list them in
`splits` (`declared: true`) and `declared_applied`. It stops applying once the filings show the split.
<!-- end B1 basket data -->

<!-- M1 economic data (2026-11-20): schema notes for the macro payloads. -->
## Economic data: `macro.json`, `macro_history.json`, `macro_releases.json` (schema notes, 2026-11-20)

Written nightly by `macro.py` (the "Economic data" step of `daily-prices.yml`) from the declarative
`macro_catalogue.json`; the computed reads and views are in `macro_reads.py`, the hand-verified release
dates in `macro_calendar_fixed.json`. The full field-by-field schema is in the build report; in brief:

| File | Contents |
|---|---|
| `macro.json` | `series{id}`: `name`, `what`, `group`, `unit`, `freq`, `kind`, `asof` (period), `value`, `prior`, `change`/`change_unit`, `yoy`/`yoy_unit`, `ann3m`, `extra` (`pct_3m`, `d3avg`, `avg3`, `chg_3`, `sum12`, `sum12_chg`, `sum12_yoy`, `chg_63`, `chg_13w`), `released`, `next_release`, `status` ok / stale / error, `reason`, `judged_by` calendar / days, `kept_from` (an error showing the last good value), `notes` (why a change is blank, e.g. the October 2025 shutdown gap), `revised_from`. Also `fed_path` (fed funds futures strip, contract months generated from the date), `reads` (eight computed reads with inputs, formula, limits), `views` (seven inflation views: test chips with fixed thresholds and a computed `side`), `counts`, `degraded`, `not_published`, `meta` (requests, bytes, seconds by host) |
| `macro_history.json` | `series{id}: [[period, value or null], ...]` (monthly/quarterly 15 years; daily/weekly a year in full, then weekly to 3 years); `first{id}{period}` = first value this job saw for recent periods of revised series |
| `macro_releases.json` | `events[]`: `date`, `time_et`, `release`, `name`, `ref`, `source`, `verified`; `sources` = status of each calendar source (BEA schedule JSON, FiscalData release calendar, the hand-verified file) |

Stale is judged against the release calendar: a scheduled release passed and the series did not advance.
Only series no calendar covers fall back to `stale_after_days`. Third-party-copyright series in the
catalogue (`store: false`: University of Michigan, ADP, ICE BofA) are never fetched into this public repo.
Sources and terms: US federal series are public domain; regional Fed, Eurostat, ECB, BIS and OECD data are
free to reuse with attribution (each record's `licence`); fund and futures prices come via Yahoo, as for
every other price here.
<!-- end M1 economic data -->
