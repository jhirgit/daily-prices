#!/usr/bin/env python3
"""Detect that a covered name has actually reported, and shout when one has not.

    python earnings_detect.py              # -> data/earnings_detected.json
    python earnings_detect.py --window 14  # widen the look-back (default 10 days)

WHY THIS EXISTS
---------------
`earnings_dates.py` says when a name is EXPECTED to report. Nothing said whether
it DID. That gap is the one that actually loses work: a print lands, nobody
notices for days, and the re-underwrite is late or skipped. Today the only thing
that notices is a human opening the dashboard, or a weekday routine that
re-derives the calendar by web search each morning.

This closes the loop against the primary source. For every name whose expected
date is inside the window it asks EDGAR whether an 8-K has been filed, and
records one of three states:

    detected  an 8-K landed on or after (expected - 2d). The print happened.
    awaiting  the expected date has not passed yet. Normal.
    overdue   the expected date passed by more than GRACE sessions and EDGAR
              still shows nothing. Either the date is wrong or the print was
              missed - both need a human, so this EXITS NONZERO and the
              workflow's alert job opens an issue.

Overdue is deliberately loud. The failure this whole design is aimed at is
SILENCE (jr-filings' scan died 2026-08-21 on an unset key and nobody was told),
and a nagging issue for a slipped date is correct: the calendar is wrong and
fixing it is the action.

SCOPE / WHAT THIS DOES NOT DO
-----------------------------
It records THAT a filing exists, with its accession number and URL. It does not
download or stage the filing text - that is SPEC-56 P3b, which needs a decision
about which repo the content lands in (this repo is PUBLIC). Detection is useful
on its own and does not need that decision.

PUBLIC REPO. Output carries ticker symbols (already public in tickers.txt),
public EDGAR accession numbers and public filing dates. Never coverage,
anchors, verdicts or position data.

EDGAR ETIQUETTE: a declared User-Agent is required, and the fair-access limit is
10 requests/second. Only names inside the window are queried - typically a
handful, a few dozen in the thick of a season.
"""

import argparse
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
DATES = os.path.join(DATA, "earnings_dates.json")
CIKMAP = os.path.join(DATA, "cik_map.json")
OUT = os.path.join(DATA, "earnings_detected.json")

UA = os.environ.get("SEC_USER_AGENT", "jr-dash-earnings-detect jakeradencom@gmail.com")
GRACE = 2          # sessions after the expected date before a miss is "overdue"
LOOKBACK = 10      # days of expected dates to keep checking


def get(url, tries=3):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            if i == tries - 1:
                raise
            time.sleep(1.5 * (i + 1))
    return None


def cik_map(refresh=False):
    """ticker -> zero-padded CIK, cached. EDGAR's own file, ~10k issuers."""
    if not refresh and os.path.exists(CIKMAP):
        with io.open(CIKMAP, encoding="utf-8") as fh:
            return json.load(fh)["map"]
    raw = get("https://www.sec.gov/files/company_tickers.json")
    out = {}
    for row in raw.values():
        out[str(row["ticker"]).upper()] = str(row["cik_str"]).zfill(10)
    doc = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": "https://www.sec.gov/files/company_tickers.json",
        "count": len(out),
        "map": out,
    }
    os.makedirs(DATA, exist_ok=True)
    with io.open(CIKMAP, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    return out


def sessions_between(a, b):
    """Mon-Fri days after `a` up to and including `b`. Holidays are not modelled;
    GRACE is small and a holiday only makes this MORE forgiving, never less."""
    n, cur = 0, a
    while cur < b:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            n += 1
    return n


# SEC Item 2.02 = "Results of Operations and Financial Condition" — the item an
# earnings release is filed under. REQUIRING IT IS LOAD-BEARING, not tidiness: a
# company files 8-Ks constantly for unrelated reasons, and matching any 8-K makes
# the detector claim a print that never happened. Caught by the negative control
# on 2026-09-03 — NVDA's 2026-09-03 filing is items='8.01' (Other Events) while
# its actual print is the 2026-08-26 'items=2.02,9.01'. Without this filter the
# 8.01 would have been read as an earnings report.
EARNINGS_ITEM = "2.02"


def recent_8k(cik, since):
    """The most recent EARNINGS 8-K (Item 2.02) filed on/after `since`, or None."""
    sub = get("https://data.sec.gov/submissions/CIK%s.json" % cik)
    r = sub.get("filings", {}).get("recent", {})
    n = len(r.get("form", []))
    items_col = r.get("items", [""] * n)
    rows = zip(r.get("form", []), r.get("filingDate", []), r.get("accessionNumber", []),
               r.get("primaryDocument", []), r.get("reportDate", []), items_col)
    for form, filed, acc, doc, period, items in rows:
        if form != "8-K" or filed < since:
            continue
        codes = [c.strip() for c in (items or "").split(",") if c.strip()]
        if EARNINGS_ITEM not in codes:
            continue
        nodash = acc.replace("-", "")
        return {
            "form": form,
            "filed": filed,
            "period": period or None,
            "accession": acc,
            "items": codes,
            "url": "https://www.sec.gov/Archives/edgar/data/%d/%s/%s" % (int(cik), nodash, doc),
        }
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--window", type=int, default=LOOKBACK, help="days of expected dates to check")
    ap.add_argument("--refresh-ciks", action="store_true", help="re-pull EDGAR's ticker->CIK file")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args(argv)

    if not os.path.exists(DATES):
        print("FAIL - %s is missing; run earnings_dates.py first." % os.path.relpath(DATES, HERE))
        return 1
    with io.open(DATES, encoding="utf-8") as fh:
        expected = json.load(fh).get("dates", {})

    today = date.today()
    lo, hi = today - timedelta(days=args.window), today + timedelta(days=1)
    in_window = {tk: d for tk, d in expected.items()
                 if lo.isoformat() <= d <= hi.isoformat()}

    ciks = cik_map(refresh=args.refresh_ciks)
    names, overdue, skipped = {}, [], []

    for tk in sorted(in_window):
        exp = in_window[tk]
        cik = ciks.get(tk.upper())
        if not cik:                       # foreign listings, ETFs: not SEC filers
            skipped.append(tk)
            continue
        exp_d = date.fromisoformat(exp)
        since = (exp_d - timedelta(days=2)).isoformat()
        try:
            hit = recent_8k(cik, since)
        except Exception as exc:
            names[tk] = {"expected": exp, "status": "error", "error": type(exc).__name__}
            continue
        time.sleep(0.15)                  # EDGAR fair access: well under 10/sec

        if hit:
            names[tk] = dict(expected=exp, status="detected", **hit)
        elif exp_d >= today:
            names[tk] = {"expected": exp, "status": "awaiting"}
        else:
            late = sessions_between(exp_d, today)
            status = "overdue" if late > GRACE else "awaiting"
            names[tk] = {"expected": exp, "status": status, "sessions_late": late}
            if status == "overdue":
                overdue.append((tk, exp, late))

    asof = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    doc = {
        "generated_at": asof,
        "source": "EDGAR submissions API; 8-K filings carrying Item 2.02 (Results of Operations) for watchlist tickers with an expected date in the window",
        "note": "Records THAT a filing exists (accession + URL), never its content. `overdue` means the "
                "expected date passed by more than %d sessions with nothing on EDGAR: either the date is "
                "wrong or the print was missed. Public repo: tickers, accessions and filing dates only." % GRACE,
        "window_days": args.window,
        "grace_sessions": GRACE,
        "counts": {
            "in_window": len(in_window),
            "checked": len(names),
            "detected": sum(1 for v in names.values() if v["status"] == "detected"),
            "awaiting": sum(1 for v in names.values() if v["status"] == "awaiting"),
            "overdue": len(overdue),
            "errors": sum(1 for v in names.values() if v["status"] == "error"),
        },
        "not_sec_filers": sorted(skipped),
        "names": names,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with io.open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")

    c = doc["counts"]
    print("earnings_detect: %d in window - %d detected, %d awaiting, %d OVERDUE, %d error(s); %d not SEC filers"
          % (c["in_window"], c["detected"], c["awaiting"], c["overdue"], c["errors"], len(skipped)))
    for tk, exp, late in overdue:
        print("  OVERDUE  %-7s expected %s, %d sessions ago, no 8-K on EDGAR" % (tk, exp, late))

    if overdue:
        print("\nFAIL - a covered name's expected date passed with no filing. Either the date in")
        print("       var EARN / the feed is wrong, or the print was missed. Both need a human.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
