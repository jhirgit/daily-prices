#!/usr/bin/env python3
"""Emit the forward earnings calendar for every ticker on the watchlist.

    python earnings_dates.py                      # -> data/earnings_dates.json
    python earnings_dates.py --tickers VST,LRCX   # subset, for a quick check

WHY THIS EXISTS
---------------
Nothing scheduled could see when the covered names report. The dashboard's
`var EARN` carried the dates, but it lives inside a 610 KB HTML file in a
different, private repo, so every automated path was blind to it: this repo has
217 tickers and, until now, zero dates, and `earnings_reactions.py` takes
(ticker, date) pairs IN rather than knowing them. A calendar no scheduler can
read is not a calendar. This makes the forward dates a committed artifact on the
same cadence as the prices, so the earnings loop (jr-dash SPEC-56) has something
to poll.

The dates here are ADVISORY. They come from an aggregator and they slip. The
board's own hand-pinned date, when it carries `confirmed`, outranks this file;
the consumer reconciles the two and flags disagreements rather than overwriting.

PUBLIC REPO — WHAT MAY GO IN THIS FILE
--------------------------------------
This repository is public. The output carries ONLY ticker symbols (already
public in tickers.txt) and their next reporting date (a public fact each company
announces). It must never carry coverage flags, anchors, verdicts, position
data, or any of the board's own prose. Keep it that way: the value of this file
is that it is boring.

Failure policy: a ticker the feed has no date for is simply absent from `dates`
and counted in `missing` — that is normal (ETFs, sleeves, names between
cycles). A ticker that RAISES is recorded in `errors` so a systematic feed break
is visible rather than looking like a quiet week.
"""

import argparse
import io
import json
import os
import sys
from datetime import date, datetime, timezone

import yfinance as yf

HERE = os.path.dirname(os.path.abspath(__file__))
TICKERS = os.path.join(HERE, "tickers.txt")
OUT = os.path.join(HERE, "data", "earnings_dates.json")


def watchlist():
    """Ticker symbols from tickers.txt, comments and blanks stripped."""
    out = []
    with io.open(TICKERS, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                out.append(line)
    return out


def parse_calendar_events(result):
    """Pull (soonest forward earnings date, isEarningsDateEstimate) out of a raw
    quoteSummary `calendarEvents` payload -- the same JSON shape
    `yf.Ticker(tk)._quote._fetch(modules=["calendarEvents"])` returns.

    Returns (None, None) if the payload has no earnings block or no date in it.

    `estimated` is Yahoo's OWN `isEarningsDateEstimate` flag on the earnings
    block. It is a HINT, never a confirmation: `estimated is False` means Yahoo
    took this date from its calendar vendor, NOT that the company confirmed it
    -- do not read `estimated: false` as "confirmed" anywhere downstream. When
    the payload doesn't carry the key at all, `estimated` comes back None.
    """
    try:
        events = result["quoteSummary"]["result"][0]["calendarEvents"]
    except (KeyError, IndexError, TypeError):
        return None, None
    earnings = events.get("earnings") or {}
    raw = earnings.get("earningsDate") or []
    if isinstance(raw, (str, int, float, date, datetime)):
        raw = [raw]
    parsed = []
    for d in raw:
        if isinstance(d, (int, float)):
            # Yahoo ships epoch seconds here; yfinance's own Quote._fetch_calendar
            # decodes the same field with datetime.fromtimestamp(d).date(), so
            # mirror that exactly rather than introduce a UTC-vs-local skew.
            d = datetime.fromtimestamp(d).date()
        if isinstance(d, datetime):
            d = d.date()
        if isinstance(d, date):
            parsed.append(d)
        elif isinstance(d, str) and d[:10]:
            parsed.append(d[:10])
    if not parsed:
        return None, None
    soonest = str(sorted(str(x) for x in parsed)[0])
    return soonest, earnings.get("isEarningsDateEstimate")


def next_date(tk):
    """The soonest forward earnings date the feed knows, plus Yahoo's own
    isEarningsDateEstimate hint for it, or (None, None).

    This reads the raw quoteSummary `calendarEvents` module directly off
    `Ticker._quote` instead of going through the `Ticker.calendar` property --
    SAME single request (`Quote._fetch_calendar()` calls this exact
    `_fetch(modules=["calendarEvents"])`, see yfinance/scrapers/quote.py), no
    extra network call. `.calendar`'s own parsing copies out only
    earningsDate/earningsHigh/earningsLow/earningsAverage/revenue* and drops
    `isEarningsDateEstimate`, which is the one field this feed wants.
    """
    result = yf.Ticker(tk)._quote._fetch(modules=["calendarEvents"])
    if not result:
        return None, None
    return parse_calendar_events(result)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tickers", default="", help="comma-separated subset (default: all of tickers.txt)")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args(argv)

    names = [t.strip() for t in args.tickers.split(",") if t.strip()] or watchlist()
    dates, missing, errors = {}, [], {}

    for tk in names:
        try:
            d, estimated = next_date(tk)
        except Exception as exc:                      # feed hiccup on one name
            errors[tk] = type(exc).__name__
            continue
        if d:
            # `estimated` is Yahoo's own isEarningsDateEstimate hint, not a
            # confirmation flag -- `estimated: false` must never be read
            # downstream as "company-confirmed" (see parse_calendar_events()
            # and SPEC-71 in the jr-dash repo). None means the raw payload
            # didn't carry the key at all.
            dates[tk] = {"date": d, "estimated": estimated}
        else:
            missing.append(tk)

    asof = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    doc = {
        "generated_at": asof,
        "source": "yfinance Ticker._quote calendarEvents.earnings (soonest earningsDate, "
                  "plus Yahoo's isEarningsDateEstimate hint) -- same request as Ticker.calendar",
        "note": "ADVISORY. Aggregator dates slip; a board date marked `confirmed` outranks this file. "
                "`dates[ticker].estimated` is Yahoo's own hint that IT projected the date "
                "(true) vs took it from its calendar vendor (false) -- false is NOT "
                "company confirmation, and estimated may be null when Yahoo omits the flag. "
                "Public repo: tickers and public reporting dates only, never coverage or position data.",
        "count": len(dates),
        "asked": len(names),
        "missing": sorted(missing),
        "errors": errors,
        "dates": {k: dates[k] for k in sorted(dates)},
    }

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with io.open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=False)
        fh.write("\n")

    print("earnings_dates: %d/%d dated, %d without a date, %d error(s) -> %s"
          % (len(dates), len(names), len(missing), len(errors), os.path.relpath(args.out, HERE)))
    if errors:
        print("  errors: " + ", ".join("%s(%s)" % (k, v) for k, v in sorted(errors.items())[:10]))

    # A feed that breaks for EVERYTHING should fail the job, not commit an empty
    # calendar over a good one. A handful of gaps is normal and must not.
    if names and not dates:
        print("FAIL — the feed returned no dates at all; refusing to publish an empty calendar.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
