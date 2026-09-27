#!/usr/bin/env python3
"""crowd.py — positioning proxies per name, COMPUTED, never authored (SPEC-36 §3.4, phase 3).

Emits data/crowd.json beside technicals.json: for every ticker in tickers.txt,

    relvol_20_60        avg volume last 20 sessions / avg volume last 60 sessions
    ext_50d             close / SMA50  - 1
    ext_200d            close / SMA200 - 1
    insider_net_usd_30d net DOLLARS sold (+) / bought (-) by insiders in open-market
                        Form 4 trades (codes S and P) dated in the 30 calendar days
                        to today, from data/insiders.json; null when the name is not
                        covered by the insiders cron (or has no Form 4 coverage)
    insider_net_30d     the same number under its old name, kept until the board
                        reads insider_net_usd_30d (review M3, 2026-09-26: it was
                        always dollars, documented and shown as shares, and its
                        "30d" was up to ~60 days of calendar-month buckets)
    asof                the last settled session used

Each proxy is null, never 0.0, where history is short (<60 / <50 / <200 bars) —
a missing number must read as missing on the dashboard, not as "no extension".

DISCLOSURE (#15): this repo is PUBLIC. Everything here is a public fact (a ticker
already in tickers.txt, its own bars, its own Form 4s). Nothing book-revealing
enters it. Confidence that these proxies measure INFORMATION state is LOW — they
measure positioning — and the dashboard labels them display-only until scored
(SPEC-36 §6).

    python crowd.py                 # write data/crowd.json

The arithmetic is pinned offline by test_crowd.py (an in-memory price history and
inline transactions). It replaced the `--verify` parity fixture, which read the
live insider file and so drifted every time a late Form 4 landed. Stdlib only.
"""
import argparse
import datetime as dt
import json
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "prices.db")
DEFAULT_TICKERS = os.path.join(HERE, "tickers.txt")
DEFAULT_INS = os.path.join(HERE, "data", "insiders.json")
DEFAULT_OUT = os.path.join(HERE, "data", "crowd.json")
INSIDER_DAYS = 30
BUY_CODES = {"P"}


def load_tickers(path=DEFAULT_TICKERS):
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            t = line.split("#", 1)[0].strip()
            if t:
                out.append(t)
    return out


def load_bars(conn, ticker, asof=None):
    """Settled daily bars, ascending: [(date, close, volume)].

    `asof` (YYYY-MM-DD) truncates the series to bars on or before that session;
    production passes None and reads to the end of the file."""
    if asof:
        rows = conn.execute(
            "SELECT date, close, volume FROM daily_prices "
            "WHERE ticker=? AND date <= ? ORDER BY date",
            (ticker, asof)).fetchall()
    else:
        rows = conn.execute(
            "SELECT date, close, volume FROM daily_prices WHERE ticker=? ORDER BY date",
            (ticker,)).fetchall()
    return [(d, c, v) for d, c, v in rows if c is not None]


def sma(vals, n):
    if len(vals) < n:
        return None
    w = vals[-n:]
    return sum(w) / float(n)


def compute_one(bars, ins_entry, today):
    if not bars:
        return None
    closes = [b[1] for b in bars]
    vols = [b[2] for b in bars if b[2] is not None]
    last_close = closes[-1]
    out = {"asof": bars[-1][0]}
    v20, v60 = sma(vols, 20), sma(vols, 60)
    out["relvol_20_60"] = round(v20 / v60, 4) if (v20 is not None and v60 not in (None, 0)) else None
    s50, s200 = sma(closes, 50), sma(closes, 200)
    out["ext_50d"] = round(last_close / s50 - 1.0, 4) if s50 else None
    out["ext_200d"] = round(last_close / s200 - 1.0, 4) if s200 else None
    net = insider_net_usd_30d(ins_entry, today)
    out["insider_net_usd_30d"] = net
    out["insider_net_30d"] = net
    return out


def insider_net_usd_30d(entry, today, days=INSIDER_DAYS):
    """Dollars sold (+) minus dollars bought (-) in open-market Form 4 trades
    dated in [today - days, today]. None unless the insiders cron covers the
    name with Form 4s (status "ok"); 0 when it does and nobody traded.

    Form 144 notices (status "144") are left out on purpose: they are notices of
    a PROPOSED sale, not executions, and the old bucket proxy left them out too."""
    if not entry or entry.get("status") != "ok":
        return None
    lo, hi = (today - dt.timedelta(days=days)).isoformat(), today.isoformat()
    net = 0
    for x in entry.get("txns") or []:
        d = x.get("d") or ""
        if lo <= d <= hi:
            v = x.get("v") or 0
            net += -v if x.get("c") in BUY_CODES else v
    return int(net)


def build(db=DEFAULT_DB, tickers=DEFAULT_TICKERS, ins_path=DEFAULT_INS, today=None,
          asof=None):
    """`asof` truncates every series to that session (tests); production leaves
    it None and reads to the end of prices.db."""
    today = today or dt.date.today()
    conn = sqlite3.connect(db)
    ins, ins_gen = {}, None
    if os.path.exists(ins_path):
        with open(ins_path, "r", encoding="utf-8") as fh:
            j = json.load(fh)
        ins = j.get("tickers") or {}
        ins_gen = j.get("generated_at")
    out = {}
    for t in load_tickers(tickers):
        row = compute_one(load_bars(conn, t, asof), ins.get(t), today)
        if row:
            out[t] = row
    conn.close()
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "method": ("SPEC-36 s3.4 positioning proxies: relvol_20_60 = mean(vol,20)/mean(vol,60); "
                   "ext_50d/ext_200d = close/SMA-1; insider_net_usd_30d = open-market Form 4 sales "
                   "minus purchases in US DOLLARS, trades dated in the 30 days to insider_window.to "
                   "(insider_net_30d is the same number under its old name); null (never 0.0) where "
                   "history is short. Display-only until scored against d5 (SPEC-36 s6); "
                   "positioning, not information."),
        "insider_window": {"from": (today - dt.timedelta(days=INSIDER_DAYS)).isoformat(),
                           "to": today.isoformat(), "unit": "USD",
                           "insiders_generated_at": ins_gen},
        "count": len(out),
        "tickers": out,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--tickers", default=DEFAULT_TICKERS)
    ap.add_argument("--insiders", default=DEFAULT_INS)
    ap.add_argument("--out", default=DEFAULT_OUT)
    a = ap.parse_args()
    payload = build(a.db, a.tickers, a.insiders)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    print("wrote %s: %d names" % (a.out, payload["count"]))


if __name__ == "__main__":
    main()
