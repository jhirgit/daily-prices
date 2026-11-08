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

The window is the feed's dates PLUS every name the previous artifact still held
unresolved (awaiting, overdue or error), kept on its old expected date until it
is detected or the look-back passes - a feed that rolls a name to next quarter
before its 8-K lands must not silence it (B-05). A name that passes the
look-back unresolved is listed once under `aged_out` and printed.

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


_SUBMISSIONS = {}


def submissions(cik):
    """One EDGAR submissions fetch per CIK per run; recent_8k and files_8k_for share it."""
    if cik not in _SUBMISSIONS:
        _SUBMISSIONS[cik] = get("https://data.sec.gov/submissions/CIK%s.json" % cik)
    return _SUBMISSIONS[cik]


def recent_8k(cik, since):
    """The most recent EARNINGS 8-K (Item 2.02) filed on/after `since`, or None."""
    return _earnings_8k(submissions(cik), since, cik)


def files_8k_for(cik):
    """True when the issuer files 8-Ks. A fetch error also returns True: an unreadable answer
    must never reclassify a name as a foreign filer and silence it."""
    try:
        return files_8k(submissions(cik))
    except Exception:
        return True


def files_8k(sub):
    """Does this issuer file 8-Ks at all? A foreign private issuer (AEM, KGC, AGI: 6-K and
    40-F) has a CIK but never files an 8-K, so 'no Item 2.02 8-K' says nothing about its
    print and must never read as overdue (2026-11-04: four false OVERDUEs failed the job)."""
    forms = sub.get("filings", {}).get("recent", {}).get("form", [])
    return any(f in ("8-K", "8-K/A") for f in forms)


def scan_submissions(sub, since, cik):
    """(earnings 8-K hit or None, files_8k) from one EDGAR submissions payload."""
    return _earnings_8k(sub, since, cik), files_8k(sub)


def _earnings_8k(sub, since, cik):
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


# B-05 (2026-11-07 review): the feed can roll a name to its NEXT quarter before the
# 8-K for the one just reported reaches EDGAR (AMSC moved to 2027-02-03 one day
# after its 11/04 report). Keyed only on the feed's date, the name then left the
# window with no detection and no OVERDUE - silence, the failure this file exists
# to prevent. So a name the previous artifact still held as unresolved stays in
# the window on its OLD expected date until it is detected or the look-back
# passes, and a name that ages out unresolved is reported, never dropped quietly.
CARRY_STATUSES = ("awaiting", "overdue", "error")


def build_window(expected, prev_names, today, window=LOOKBACK):
    """Which names to check today, and on which expected date.

    expected    ticker -> feed date (YYYY-MM-DD), today's earnings_dates feed
    prev_names  the previous artifact's `names` map ({} on a first run)
    Returns (in_window, carried, aged_out):
      in_window  ticker -> the expected date to check against
      carried    ticker -> the feed's current date (or None) for each name kept
                 from the previous artifact rather than from the feed
      aged_out   [{ticker, expected, last_status, days, feed_date}] - names the
                 previous artifact held unresolved that are now past the
                 look-back and are no longer checked
    """
    lo = (today - timedelta(days=window)).isoformat()
    hi = (today + timedelta(days=1)).isoformat()
    in_window = {tk: d for tk, d in expected.items() if lo <= d <= hi}
    carried, aged_out = {}, []
    for tk in sorted(prev_names or {}):
        row = prev_names[tk] or {}
        if row.get("status") not in CARRY_STATUSES or tk in in_window:
            continue
        exp = row.get("expected")
        if not exp or exp > hi:           # no date, or not yet due: nothing late to look for
            continue
        if exp >= lo:
            in_window[tk] = exp
            carried[tk] = expected.get(tk)
        else:                             # unresolved past the look-back: say so, once
            aged_out.append({"ticker": tk, "expected": exp, "last_status": row["status"],
                             "days": (today - date.fromisoformat(exp)).days,
                             "feed_date": expected.get(tk)})
    return in_window, carried, aged_out


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
    # SPEC-71: since cebc949 each value is {"date": "YYYY-MM-DD", "estimated": bool|None};
    # older artifacts carried the bare string. Normalise to the date — this script only
    # needs WHEN, and the `expected` field it emits stays a plain date for stage_earnings.py.
    expected = {tk: (v["date"] if isinstance(v, dict) else v) for tk, v in expected.items()
                if (v.get("date") if isinstance(v, dict) else v)}

    prev = {}
    if os.path.exists(args.out):
        try:
            with io.open(args.out, encoding="utf-8") as fh:
                prev = json.load(fh).get("names", {}) or {}
        except (ValueError, OSError):
            prev = {}                     # an unreadable artifact carries nothing; today's feed still runs

    today = date.today()
    in_window, carried, aged_out = build_window(expected, prev, today, args.window)

    ciks = cik_map(refresh=args.refresh_ciks)
    names, overdue, skipped, foreign = {}, [], [], []

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
            eight_k = True if hit else files_8k_for(cik)
        except Exception as exc:
            names[tk] = {"expected": exp, "status": "error", "error": type(exc).__name__}
            continue
        time.sleep(0.15)                  # EDGAR fair access: well under 10/sec

        if not hit and not eight_k:       # 6-K/40-F/20-F filer: an 8-K will never come
            foreign.append(tk)
            continue
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
        if tk in carried and tk in names:
            # B-05: kept from the previous artifact after the feed moved the name on.
            names[tk]["carried"] = True
            names[tk]["feed_date"] = carried[tk]

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
            "carried": len(carried),
            "aged_out": len(aged_out),
        },
        "aged_out": aged_out,
        "not_sec_filers": sorted(skipped),
        "not_8k_filers": sorted(foreign),
        "names": names,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with io.open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")

    c = doc["counts"]
    print("earnings_detect: %d in window - %d detected, %d awaiting, %d OVERDUE, %d error(s); %d not SEC filers, %d not 8-K filers"
          % (c["in_window"], c["detected"], c["awaiting"], c["overdue"], c["errors"], len(skipped), len(foreign)))
    for tk, exp, late in overdue:
        print("  OVERDUE  %-7s expected %s, %d sessions ago, no 8-K on EDGAR" % (tk, exp, late))
    for tk in sorted(carried):
        print("  CARRIED  %-7s expected %s, the feed now says %s; still checked until detected"
              % (tk, in_window[tk], carried[tk] or "no date"))
    for row in aged_out:
        print("  AGED OUT %-7s expected %s, %s for %d days with no earnings 8-K; no longer checked"
              % (row["ticker"], row["expected"], row["last_status"], row["days"]))

    # OVERDUE IS DATA, NOT A JOB FAILURE (review F-01, 2026-11-08; decision 13a). Until then an
    # overdue name exited 1, the earnings-dates job went red, and the alert issue stayed open
    # for weeks on names whose dates were simply wrong (MRAAF, the foreign filers before B-02):
    # 13 of 14 close-job runs ended "failure" and a real EDGAR or price failure would have
    # looked exactly like that noise. The overdue list stays in the artifact (`names[*].status
    # == "overdue"`, `counts.overdue`) and in the lines printed above; the Brief and the
    # watchdog read it from there. What still exits non-zero here is a run that could not do
    # its job at all: a missing dates feed (above), or EDGAR answering nothing for every
    # name it checked — silence dressed as a clean run is the failure this file exists against.
    if c["checked"] and c["errors"] == c["checked"]:
        print("\nFAIL - every EDGAR lookup errored (%d of %d); the detector saw nothing this run." % (c["errors"], c["checked"]))
        return 1
    if overdue:
        print("\nOVERDUE - %d covered name(s) passed the expected date with no filing on EDGAR. Either the"
              " date in the feed is wrong or the report was missed; a person decides. This is DATA"
              " in the artifact, not a job failure (review F-01)." % len(overdue))
    return 0


if __name__ == "__main__":
    sys.exit(main())
