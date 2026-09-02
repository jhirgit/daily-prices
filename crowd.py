#!/usr/bin/env python3
"""crowd.py — positioning proxies per name, COMPUTED, never authored (SPEC-36 §3.4, phase 3).

Emits data/crowd.json beside technicals.json: for every ticker in tickers.txt,

    relvol_20_60    avg volume last 20 sessions / avg volume last 60 sessions
    ext_50d         close / SMA50  - 1
    ext_200d        close / SMA200 - 1
    insider_net_30d net shares SOLD (+) / BOUGHT (-) by insiders over the last
                    ~30 days, from data/insiders_12m.json (EDGAR Form 4 P/S,
                    monthly buckets [buy_sh, sell_sh, buy_n, sell_n, 10b5-1_sh]);
                    null when the name is not covered by the insiders cron
    asof            the last settled session used

Each proxy is null, never 0.0, where history is short (<60 / <50 / <200 bars) —
a missing number must read as missing on the dashboard, not as "no extension".

DISCLOSURE (#15): this repo is PUBLIC. Everything here is a public fact (a ticker
already in tickers.txt, its own bars, its own Form 4s). Nothing book-revealing
enters it. Confidence that these proxies measure INFORMATION state is LOW — they
measure positioning — and the dashboard labels them display-only until scored
(SPEC-36 §6).

    python crowd.py                 # write data/crowd.json
    python crowd.py --verify        # recompute the parity fixture and compare
    python crowd.py --freeze T1,T2  # write parity/crowd_expected.json from a hand-verified run

Parity discipline is the same as earnings_reactions.py / regime.py: the fixture
is frozen from a first run on five names checked by hand, and --verify refuses
to drift from it. Stdlib only.
"""
import argparse
import datetime as dt
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "prices.db")
DEFAULT_TICKERS = os.path.join(HERE, "tickers.txt")
DEFAULT_INS = os.path.join(HERE, "data", "insiders_12m.json")
DEFAULT_OUT = os.path.join(HERE, "data", "crowd.json")
PARITY = os.path.join(HERE, "parity", "crowd_expected.json")
TOL = 1e-6


def load_tickers(path=DEFAULT_TICKERS):
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            t = line.split("#", 1)[0].strip()
            if t:
                out.append(t)
    return out


def load_bars(conn, ticker):
    """Settled daily bars, ascending: [(date, close, volume)]."""
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
    out["insider_net_30d"] = insider_net_30d(ins_entry, today)
    return out


def insider_net_30d(entry, today):
    """Net shares sold (+) / bought (-) over the buckets that overlap the last 30
    days. Buckets are calendar months, oldest -> newest; a bucket counts when its
    month is the current month or the month containing today-30d."""
    if not entry or entry.get("status") != "ok" or not entry.get("m"):
        return None
    months = entry.get("_months")
    if not months:
        return None
    cutoff = (today - dt.timedelta(days=30)).strftime("%Y-%m")
    net = 0
    used = 0
    for mkey, bucket in zip(months, entry["m"]):
        if mkey >= cutoff:
            b, s = bucket[0] or 0, bucket[1] or 0
            net += s - b
            used += 1
    return int(net) if used else None


def build(db=DEFAULT_DB, tickers=DEFAULT_TICKERS, ins_path=DEFAULT_INS, today=None):
    today = today or dt.date.today()
    conn = sqlite3.connect(db)
    ins = {}
    months = []
    if os.path.exists(ins_path):
        with open(ins_path, "r", encoding="utf-8") as fh:
            j = json.load(fh)
        months = j.get("months") or []
        for t, e in (j.get("tickers") or {}).items():
            e = dict(e)
            e["_months"] = months
            ins[t] = e
    out = {}
    for t in load_tickers(tickers):
        row = compute_one(load_bars(conn, t), ins.get(t), today)
        if row:
            out[t] = row
    conn.close()
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "method": ("SPEC-36 s3.4 positioning proxies: relvol_20_60 = mean(vol,20)/mean(vol,60); "
                   "ext_50d/ext_200d = close/SMA-1; insider_net_30d = Form 4 sells-buys (shares) over the "
                   "monthly buckets overlapping the last 30 days; null (never 0.0) where history is short. "
                   "Display-only until scored against d5 (SPEC-36 s6); positioning, not information."),
        "insider_months": months[-2:] if months else [],
        "count": len(out),
        "tickers": out,
    }


def _close(a, b):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= TOL * max(1.0, abs(a), abs(b))


def verify(db=DEFAULT_DB, tickers=DEFAULT_TICKERS, ins_path=DEFAULT_INS, path=PARITY):
    with open(path, "r", encoding="utf-8") as fh:
        fx = json.load(fh)
    today = dt.date.fromisoformat(fx["today"])
    got = build(db, tickers, ins_path, today)["tickers"]
    bad = 0
    print("parity: %s (today=%s)" % (fx.get("source", path), fx["today"]))
    for t, exp in fx["cases"].items():
        g = got.get(t)
        for k in ("asof", "relvol_20_60", "ext_50d", "ext_200d", "insider_net_30d"):
            want, have = exp.get(k), (g or {}).get(k)
            same = (want == have) if k == "asof" else _close(want, have)
            print("  %-6s %-16s want %-12s got %-12s %s" % (t, k, want, have, "ok" if same else "DRIFT"))
            if not same:
                bad += 1
    if bad:
        print("FAIL: %d value(s) drifted from the frozen fixture" % bad)
        return 1
    print("OK: parity holds")
    return 0


def freeze(names, db=DEFAULT_DB, tickers=DEFAULT_TICKERS, ins_path=DEFAULT_INS, path=PARITY):
    today = dt.date.today()
    got = build(db, tickers, ins_path, today)["tickers"]
    cases = {t: got[t] for t in names if t in got}
    fx = {"source": "crowd.py first run, hand-verified on these names (SPEC-36 phase 3)",
          "today": today.isoformat(), "cases": cases}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(fx, fh, indent=1)
    print("froze %d cases into %s" % (len(cases), path))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--tickers", default=DEFAULT_TICKERS)
    ap.add_argument("--insiders", default=DEFAULT_INS)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--freeze", default=None, help="comma-separated tickers to freeze as the parity fixture")
    a = ap.parse_args()
    if a.verify:
        sys.exit(verify(a.db, a.tickers, a.insiders))
    if a.freeze:
        sys.exit(freeze([t.strip().upper() for t in a.freeze.split(",") if t.strip()], a.db, a.tickers, a.insiders))
    payload = build(a.db, a.tickers, a.insiders)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    print("wrote %s: %d names" % (a.out, payload["count"]))


if __name__ == "__main__":
    main()
