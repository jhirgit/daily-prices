# Research: Bollinger %B "buyable bottom" study

Scratch research, contained to this branch. `.github/workflows/research-pctb.yml`
runs on a push to `research/pctb`: it checks whether the `FINNHUB_API_KEY` plan
serves daily candles, then backfills SPY/QQQ/IWM at `--lookback max` from Yahoo
into a throwaway `research-prices.db` on the runner (never committed, never touches
`prices.db`) and runs `pctb_backtest.py` over the full history, the 2000-03 and
2007-09 bear markets, and a pre/post 2016-08-15 split. Results go to the job
summary and a `pctb-results` artifact.

Delete the branch to remove it; nothing here is on `main`.
