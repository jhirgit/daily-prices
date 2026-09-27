#!/usr/bin/env python3
"""
repair_adj.py -- re-base prices.db's adj_close on Yahoo's current adjustment
factors. DRY RUN unless --apply.

Why. Before fetch_prices.rebase_history (review H1, 2026-09-26) a daily run
rewrote only its 7-day window, so every ex-date after a ticker's backfill left
the older rows on a stale basis: each dividend showed up as a fake one-day loss,
credited back a week later. This walks the whole stored history once and puts
every row back on one basis. Keep it for the two cases rebase_history refuses to
guess: an outage longer than the lookback window ("no-overlap") and a close move
no split event explains ("close-mismatch").

How. Per ticker, one full-history fetch (auto_adjust=False) gives Yahoo's factor
f(d) = Adj Close / Close for every date. For each stored row:

    adj_close := stored close * f(d)

Only adj_close is written: open/high/low/close/volume are never touched. A date
Yahoo does not return takes the factor of the next later date Yahoo does return
(the factor only changes at an ex-date). A row whose stored close differs from
Yahoo's by more than CLOSE_TOL is left alone and counted -- a split the stored
history never absorbed, or a bad bar -- and a ticker with more than
MAX_MISMATCH_SHARE of its rows in that state is skipped whole and reported.

    python repair_adj.py                         # dry run, every stored ticker
    python repair_adj.py --tickers SPY,TLT        # dry run, a subset
    python repair_adj.py --sample SPY,TLT,HYG     # + before/after vs auto_adjust
    python repair_adj.py --apply                  # write prices.db

Network (Yahoo), so it is a manual tool, never a CI step; the tests exercise
plan() offline.
"""

from __future__ import annotations

import argparse
import bisect
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "prices.db")

# Stored close vs Yahoo close: beyond this the row is a mismatch, not a repair.
CLOSE_TOL = 0.01
# More than this share of a ticker's rows mismatched: skip the ticker whole.
MAX_MISMATCH_SHARE = 0.02
# A row "changes" when its adj_close moves by more than this (relative).
CHANGE_TOL = 1e-7
SAMPLE_DATES = 6
SLEEP_BETWEEN = 0.5


def _f(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return None if v != v else v


def plan(stored, yahoo):
    """Pure core. stored: [(date, close, adj)] ascending; yahoo: [(date, close,
    adj)] ascending. Returns (updates, stats): updates = [(date, new_adj)] for
    rows whose adj_close moves by more than CHANGE_TOL."""
    ys = [(d, c, a) for d, c, a in yahoo if c and a and c > 0 and a > 0]
    stats = {"rows": len(stored), "changed": 0, "mismatch": 0, "max_rel": 0.0,
             "no_factor": 0, "skipped": False}
    if not ys:
        stats["skipped"] = True
        return [], stats
    ydates = [d for d, _, _ in ys]
    yclose = {d: c for d, c, _ in ys}
    yfac = [a / c for _, c, a in ys]
    updates = []
    for d, c, a in stored:
        if c is None or c <= 0:
            stats["no_factor"] += 1
            continue
        if d in yclose and abs(c / yclose[d] - 1.0) > CLOSE_TOL:
            stats["mismatch"] += 1
            continue
        i = bisect.bisect_left(ydates, d)
        if i == len(ydates):          # newer than Yahoo's last bar: leave it
            stats["no_factor"] += 1
            continue
        new = c * yfac[i]
        rel = abs(new / a - 1.0) if a else float("inf")
        if rel > CHANGE_TOL:
            updates.append((d, new))
            stats["changed"] += 1
            stats["max_rel"] = max(stats["max_rel"], rel)
    if stats["rows"] and stats["mismatch"] > MAX_MISMATCH_SHARE * stats["rows"]:
        stats["skipped"] = True
        return [], stats
    return updates, stats


def yahoo_rows(sym, auto_adjust=False):
    import yfinance as yf
    h = yf.Ticker(sym).history(period="max", auto_adjust=auto_adjust, actions=False)
    if h is None or h.empty:
        return []
    out = []
    for ts, r in h.iterrows():
        c = _f(r.get("Close"))
        a = c if auto_adjust else _f(r.get("Adj Close"))
        out.append((ts.strftime("%Y-%m-%d"), c, a))
    return out


def stored_rows(conn, sym):
    return conn.execute("SELECT date, close, adj_close FROM daily_prices WHERE ticker=? "
                        "ORDER BY date", (sym,)).fetchall()


def sample_report(sym, before, updates):
    """Before/after adj_close on a spread of dates vs Yahoo's auto_adjust Close."""
    auto = {d: c for d, c, _ in yahoo_rows(sym, auto_adjust=True)}
    after = {d: a for d, _, a in before}
    after.update(dict(updates))
    dates = [d for d, _, _ in before if d in auto]
    if not dates:
        print(f"  {sym}: no dates in common with the auto_adjust series")
        return None
    step = max(1, len(dates) // SAMPLE_DATES)
    picks = sorted(set(dates[::step][-SAMPLE_DATES:] + dates[-12:-9] + [dates[-1]]))
    old = {d: a for d, _, a in before}
    print(f"  {sym}: date        before      after       yahoo-auto  before%   after%")
    for d in picks:
        y = auto[d]
        print(f"  {sym}: {d}  {old[d]:10.4f}  {after[d]:10.4f}  {y:10.4f}  "
              f"{(old[d] / y - 1) * 100:+7.3f}  {(after[d] / y - 1) * 100:+7.3f}")
    worst_b = max(abs(old[d] / auto[d] - 1) for d in dates)
    worst_a = max(abs(after[d] / auto[d] - 1) for d in dates)
    print(f"  {sym}: worst over {len(dates)} common dates: before {worst_b * 100:.4f}%  "
          f"after {worst_a * 100:.4f}%")
    return worst_a


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--tickers", default="", help="comma-separated subset (default: every stored ticker)")
    ap.add_argument("--sample", default="", help="comma-separated tickers to compare with auto_adjust")
    ap.add_argument("--apply", action="store_true", help="write prices.db (default: dry run)")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    syms = ([t.strip().upper() for t in args.tickers.split(",") if t.strip()] or
            [r[0] for r in conn.execute("SELECT DISTINCT ticker FROM daily_prices ORDER BY 1")])
    sample = [t.strip().upper() for t in args.sample.split(",") if t.strip()]
    for t in sample:
        if t not in syms:
            syms.append(t)
    print(f"{'APPLY' if args.apply else 'DRY RUN'}: {len(syms)} ticker(s) in {args.db}")
    tot_rows = tot_changed = 0
    failed, skipped, worst = [], [], {}
    for i, sym in enumerate(syms):
        try:
            y = yahoo_rows(sym)
        except Exception as e:  # noqa: BLE001 - per-ticker isolation
            y, err = [], e
        else:
            err = None
        before = stored_rows(conn, sym)
        if not y:
            print(f"  [FAIL] {sym:9s} no Yahoo history ({err or 'empty'}); left as is")
            failed.append(sym)
            continue
        updates, st = plan(before, y)
        tot_rows += st["rows"]
        tot_changed += st["changed"]
        tag = "SKIP " if st["skipped"] else "ok   "
        print(f"  [{tag}] {sym:9s} rows {st['rows']:5d}  changed {st['changed']:5d}  "
              f"max {st['max_rel'] * 100:7.4f}%  close-mismatch {st['mismatch']}"
              + (f"  no-factor {st['no_factor']}" if st["no_factor"] else ""))
        if st["skipped"]:
            skipped.append(sym)
        if sym in sample:
            worst[sym] = sample_report(sym, before, updates)
        if args.apply and updates:
            conn.executemany("UPDATE daily_prices SET adj_close=? WHERE ticker=? AND date=?",
                             [(a, sym, d) for d, a in updates])
            conn.commit()
        if i < len(syms) - 1:
            time.sleep(SLEEP_BETWEEN)
    conn.close()
    print(f"\n{'Applied' if args.apply else 'Would change'}: {tot_changed} of {tot_rows} rows; "
          f"{len(failed)} failed ({', '.join(failed)}); {len(skipped)} skipped ({', '.join(skipped)})")
    if worst:
        print("Sample, worst |after - auto_adjust|: " +
              ", ".join(f"{k} {v * 100:.4f}%" for k, v in worst.items() if v is not None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
