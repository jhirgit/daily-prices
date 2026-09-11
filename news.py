#!/usr/bin/env python3
"""news.py — last-36h headlines on every covered name, as a FILE (SPEC-83 §4, phase 1).

Emits data/news.json beside overnight.json. Why a file and not a browsing agent: the
morning missive is written by a cloud routine, and a routine that browses is a routine
that can misread or invent a headline with nothing to check it against. It may cite only
what is in this payload, so this payload is the whole guard.

Source is Finnhub `/company-news` on the FINNHUB_API_KEY secret this repo already
provisions for intraday `/quote` — free tier, per-symbol, US listings solid, ADRs/FPIs
partial. Names the API cannot serve land in `unavailable` with a reason so the missive
can say "no feed" rather than "no news"; names it served with nothing in the window land
in `empty`. Those are different facts and the missive must not conflate them.

    {"generated_at": "...Z", "window_h": 36, "source": "finnhub company-news",
     "count": 141,
     "names": {"NVDA": [{"t": "...Z", "headline": "...", "source": "Reuters",
                         "url": "https://...", "summary": "..."}]},
     "empty": ["AXTI", ...],
     "unavailable": [{"ticker": "000660.KS", "why": "HTTP 403"}, ...]}

Indices (`^GSPC`), futures (`GC=F`) and Yahoo-style crypto pairs (`BTC-USD`) are not
company symbols: they are skipped without a request — Finnhub would return an empty
array and each one would still burn a call and a second of pacing. `.KS` / `.ST` / `.T`
foreign listings ARE attempted: the point of phase 1 is to find out what this plan
actually covers, and the answer belongs in `unavailable` where it can be read.

Rate discipline: free tier is 60 calls/min, so one call per ~1.05s; a 429 is retried
once after 65s; any other per-symbol failure is recorded and the run continues. A news
outage must never cost the overnight board.

    python news.py                  # write data/news.json (needs FINNHUB_API_KEY)
    python news.py --sleep 0        # local burst against a paid key
Stdlib only.
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TICKERS = os.path.join(HERE, "tickers.txt")
DEFAULT_OUT = os.path.join(HERE, "data", "news.json")

API = "https://finnhub.io/api/v1/company-news"
SOURCE = "finnhub company-news"
WINDOW_H = 36
CAP = 25            # items kept per name, newest first
SLEEP_S = 1.05      # 60 calls/min free tier, with headroom
RETRY_AFTER_S = 65  # a 429 is a per-minute bucket; wait it out once


class NewsError(Exception):
    """A per-symbol failure. Recorded in `unavailable`; never aborts the run."""


def load_tickers(path=DEFAULT_TICKERS):
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            t = line.split("#", 1)[0].strip()
            if t:
                out.append(t)
    return out


def skip_reason(ticker):
    """Why Finnhub cannot serve this symbol, or None if it should be attempted."""
    if "^" in ticker:
        return "index, not a company symbol"
    if ticker.endswith("=F"):
        return "futures contract, not a company symbol"
    if ticker.endswith("-USD"):
        return "crypto pair, not a company symbol"
    return None


def iso(ts):
    return dt.datetime.fromtimestamp(int(ts), tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso(now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def window_dates(now):
    """Finnhub takes calendar days; ask D-2..D and let the 36h filter do the real cut."""
    d = now.date()
    return (d - dt.timedelta(days=2)).isoformat(), d.isoformat()


def select(raw, now_ts, window_h=WINDOW_H, cap=CAP):
    """Window -> newest-first -> dedupe on url -> cap. Pure; the run's only real logic.

    Finnhub `datetime` is unix seconds. An item with no usable timestamp, or with no
    url, is dropped: the missive cites source and link, so an item it cannot link is
    not an item. Sorting before the dedupe means the newest copy of a syndicated story
    is the one that survives.
    """
    cutoff = now_ts - window_h * 3600
    rows = []
    for it in raw or []:
        if not isinstance(it, dict):
            continue
        try:
            ts = int(it.get("datetime") or 0)
        except (TypeError, ValueError):
            continue
        if ts <= 0 or ts < cutoff:
            continue
        url = str(it.get("url") or "").strip()
        if not url:
            continue
        rows.append((ts, {
            "t": iso(ts),
            "headline": str(it.get("headline") or "").strip(),
            "source": str(it.get("source") or "").strip(),
            "url": url,
            "summary": str(it.get("summary") or "").strip(),
        }))
    rows.sort(key=lambda r: -r[0])  # stable: ties keep the API's own order
    out, seen = [], set()
    for _ts, item in rows:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        out.append(item)
        if len(out) >= cap:
            break
    return out


def fetch_company_news(symbol, frm, to, token, timeout=20):
    """One /company-news call. Raises NewsError on anything that is not a JSON list."""
    qs = urllib.parse.urlencode({"symbol": symbol, "from": frm, "to": to, "token": token})
    try:
        with urllib.request.urlopen(f"{API}?{qs}", timeout=timeout) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as exc:
        raise NewsError("HTTP %s" % exc.code)
    except Exception as exc:  # noqa: BLE001 -- urlopen raises a zoo; all are per-symbol
        raise NewsError(type(exc).__name__ + ": " + str(exc)[:80])
    if isinstance(body, dict) and body.get("error"):
        raise NewsError(str(body["error"])[:80])
    if not isinstance(body, list):
        raise NewsError("unexpected payload: " + type(body).__name__)
    return body


def build(tickers, fetch, now=None, window_h=WINDOW_H, cap=CAP, sleep_s=0.0,
          sleep=time.sleep, log=None):
    """Assemble the payload. `fetch(symbol, frm, to)` is injected so tests need no network.

    A symbol Finnhub cannot serve is never requested; a symbol that fails is recorded and
    the run continues. Names with no items are listed in `empty`, not given empty arrays,
    so the file's size tracks the news and not the watchlist.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    now_ts = int(now.timestamp())
    frm, to = window_dates(now)
    names, empty, unavailable = {}, [], []
    first = True
    for tk in tickers:
        why = skip_reason(tk)
        if why:
            unavailable.append({"ticker": tk, "why": why})
            continue
        if not first and sleep_s:
            sleep(sleep_s)
        first = False
        try:
            raw = fetch(tk, frm, to)
        except NewsError as exc:
            if str(exc) == "HTTP 429":
                sleep(RETRY_AFTER_S)
                try:
                    raw = fetch(tk, frm, to)
                except NewsError as exc2:
                    unavailable.append({"ticker": tk, "why": str(exc2)})
                    continue
            else:
                unavailable.append({"ticker": tk, "why": str(exc)})
                continue
        items = select(raw, now_ts, window_h, cap)
        if items:
            names[tk] = items
        else:
            empty.append(tk)
        if log:
            log(tk, len(items))
    return {
        "generated_at": now_iso(now),
        "window_h": window_h,
        "source": SOURCE,
        "count": sum(len(v) for v in names.values()),
        "names": names,
        "empty": empty,
        "unavailable": unavailable,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default=DEFAULT_TICKERS)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--window-h", type=int, default=WINDOW_H)
    ap.add_argument("--cap", type=int, default=CAP)
    ap.add_argument("--sleep", type=float, default=SLEEP_S)
    a = ap.parse_args()

    token = os.environ.get("FINNHUB_API_KEY")
    if not token:
        sys.exit("FINNHUB_API_KEY is not set (repo secret; see README)")

    tickers = load_tickers(a.tickers)
    payload = build(tickers, lambda s, f, t: fetch_company_news(s, f, t, token),
                    window_h=a.window_h, cap=a.cap, sleep_s=a.sleep)

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)

    served = len(payload["names"]) + len(payload["empty"])
    print("news: %d items across %d names; %d served with nothing, %d unavailable -> %s"
          % (payload["count"], len(payload["names"]), len(payload["empty"]),
             len(payload["unavailable"]), a.out))
    if payload["unavailable"]:
        print("unavailable: " + ", ".join(
            "%s (%s)" % (u["ticker"], u["why"]) for u in payload["unavailable"]))
    return 0 if served else 2


if __name__ == "__main__":
    raise SystemExit(main())
