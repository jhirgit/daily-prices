#!/usr/bin/env python3
"""macro.py -- the economic-data job: fetch the series in macro_catalogue.json, judge each one
against its release calendar, compute the fixed reads and views, write three payloads.

    data/macro.json           the board's read: per series latest value, prior, change, year-over-year
                              and 3-month annualised change where meaningful, asof / released /
                              next_release, status ok|stale|error with a reason; the fed funds futures
                              path; the eight computed reads; the seven inflation views
    data/macro_history.json   compact per-series history for sparklines and 10-year z-scores, plus the
                              first print of recent periods (vintages accumulate from the job's start)
    data/macro_releases.json  the generated forward release calendar, each row with its source

Sources (one small adapter each): FRED (keyless fredgraph.csv now; the keyed API when FRED_API_KEY is
set -- same adapter; with the key the release calendar also takes FRED's fred/release/dates for every
release whose catalogue entry names a fred_release id), the Bank of England database CSV, Treasury FiscalData, Eurostat, SDMX CSV (ECB, BIS, OECD), the Cleveland Fed
nowcast JSON, Atlanta Fed workbooks, the Census C30 workbook, the repo's own price database, and the
CBOT fed funds futures strip through yfinance (contract months generated from the date; never the
generic ZQ=F, which pointed at January 2027 on 2026-11-20).

Rules (recon-2 section 4, build M1):
  * Polite: one generic client name, at least PACE seconds between requests to one host, retries with
    back-off on timeouts / 429 / 5xx only. A 401/403 (a bot wall) or a 404 (a moved file) is reported,
    never worked around.
  * Loud, not silent: a failed fetch, a parse that finds no rows, a renamed column or category, a series
    key that answers nothing -- each is status "error" with its reason, and yesterday's value is kept but
    marked, never shown as current. "stale" is judged against the release calendar (a scheduled release
    passed and the series did not advance), with a days-elapsed backstop only for series no calendar covers.
  * Known gaps (October 2025 CPI and household survey) give a blank change with the reason; never
    interpolated.
  * store:false series (third-party copyright: Michigan, ADP, ICE BofA) are never fetched into this
    PUBLIC repo.
  * Exits 0 on per-series errors so the price commit is never lost; exits 2 only on a malformed catalogue.

    python macro.py                    # fetch everything, write the three payloads
    python macro.py --only fred,sdmx_csv --out-dir /tmp/x
    python macro.py --offline DIR      # read saved responses from DIR (tests); no network

Stdlib only, except yfinance (already the pipeline's one pinned dependency) for the futures strip.
"""
from __future__ import annotations

import argparse
import calendar as _cal
import csv
import datetime as dt
import gzip
import io
import http.client
import json
import math
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
CATALOGUE = os.path.join(HERE, "macro_catalogue.json")
FIXED_CAL = os.path.join(HERE, "macro_calendar_fixed.json")

UA = "macro-data-fetch/1.0 (nightly economic-data job; low volume)"
PACE = 1.0                 # seconds between requests to the same host
ATTEMPTS = 3
TIMEOUT = 90
HIST_YEARS_SLOW = 15       # monthly / quarterly / annual history kept in macro_history.json
HIST_DAYS_DAILY = 366      # daily / weekly: every observation for a year...
HIST_YEARS_DAILY = 3       # ...then one per week back to three years
FIRST_KEEP = 24            # first prints kept per revised series
ZQ_MONTHS = 13
ZQ_CODES = "FGHJKMNQUVXZ"


class SourceError(Exception):
    """A per-series failure that must show on the board as status 'error' with this text."""


class CatalogueError(Exception):
    """The catalogue itself is malformed: the only condition that exits non-zero."""


# =====================================================================================
# HTTP: polite, paced, cached per run, retried only where a retry is legitimate
# =====================================================================================

class Http:
    def __init__(self, pace=PACE, attempts=ATTEMPTS, timeout=TIMEOUT, offline=None, log=print):
        self.pace, self.attempts, self.timeout = pace, attempts, timeout
        self.offline = offline          # dict url -> bytes | Exception, or a directory of saved responses
        self.cache, self.last_hit, self.stats = {}, {}, {}
        self.log = log

    def _stat(self, host, nbytes):
        s = self.stats.setdefault(host, {"requests": 0, "bytes": 0})
        s["requests"] += 1
        s["bytes"] += nbytes

    def get(self, url, headers=None):
        if url in self.cache:
            v = self.cache[url]
            if isinstance(v, Exception):
                raise v
            return v
        try:
            body = self._get(url, headers)
        except SourceError as e:
            self.cache[url] = e
            raise
        except (ValueError, http.client.HTTPException) as e:
            # e.g. http.client.InvalidURL for a control character in the URL: the request was never made
            e2 = SourceError("the request could not be made (%s): check the URL or the key" % type(e).__name__)
            self.cache[url] = e2
            raise e2
        self.cache[url] = body
        return body

    def _get(self, url, headers):
        host = urllib.parse.urlsplit(url).netloc
        if self.offline is not None:
            v = self.offline(url) if callable(self.offline) else self.offline.get(url)
            if v is None:
                raise SourceError("offline: no saved response for %s" % url[:120])
            if isinstance(v, Exception):
                raise v
            self._stat(host, len(v))
            return v
        h = {"User-Agent": UA, "Accept-Encoding": "gzip"}
        h.update(headers or {})
        err = None
        for attempt in range(1, self.attempts + 1):
            wait = self.pace - (time.time() - self.last_hit.get(host, 0))
            if wait > 0:
                time.sleep(wait)
            self.last_hit[host] = time.time()
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=self.timeout) as r:
                    b = r.read()
                if b[:2] == b"\x1f\x8b":
                    b = gzip.decompress(b)
                self._stat(host, len(b))
                return b
            except urllib.error.HTTPError as e:
                self._stat(host, 0)
                if e.code in (401, 403):
                    raise SourceError("HTTP %d from %s: refused or a bot check; not worked around" % (e.code, host))
                if e.code == 404:
                    raise SourceError("HTTP 404 from %s: the file or series is gone (moved URL or changed key?)" % host)
                if e.code == 400:
                    raise SourceError("HTTP 400 from %s: request refused (bad series id or parameter?)" % host)
                err = "HTTP %d from %s" % (e.code, host)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                self._stat(host, 0)
                err = "%s reaching %s" % (type(e).__name__, host)
            if attempt < self.attempts:
                time.sleep(2 ** attempt)
        raise SourceError(err + " after %d attempts" % self.attempts)


# =====================================================================================
# periods
# =====================================================================================

def period_of(date_str, freq):
    """A source date -> this job's period label: D/W 'YYYY-MM-DD', M 'YYYY-MM', Q 'YYYY-Qn', A 'YYYY'."""
    d = date_str.strip()
    m = re.match(r"^(\d{4})-Q([1-4])$", d)
    if m:
        return d if freq == "Q" else None
    m = re.match(r"^(\d{4})-(\d{2})(?:-(\d{2}))?$", d)
    if not m:
        m2 = re.match(r"^(\d{4})$", d)
        return d if (m2 and freq == "A") else None
    y, mo, day = int(m.group(1)), int(m.group(2)), m.group(3)
    if freq in ("D", "W"):
        return "%04d-%02d-%s" % (y, mo, day or "01")
    if freq == "M":
        return "%04d-%02d" % (y, mo)
    if freq == "Q":
        return "%04d-Q%d" % (y, (mo - 1) // 3 + 1)
    if freq == "A":
        return "%04d" % y
    return None


def period_end(p):
    if re.match(r"^\d{4}-\d{2}-\d{2}$", p):
        return dt.date.fromisoformat(p)
    if re.match(r"^\d{4}-\d{2}$", p):
        y, m = int(p[:4]), int(p[5:7])
        return dt.date(y, m, _cal.monthrange(y, m)[1])
    if re.match(r"^\d{4}-Q[1-4]$", p):
        y, q = int(p[:4]), int(p[-1])
        return dt.date(y, 3 * q, _cal.monthrange(y, 3 * q)[1])
    if re.match(r"^\d{4}$", p):
        return dt.date(int(p), 12, 31)
    raise ValueError(p)


def period_shift(p, n):
    """Move a M / Q / A period by n periods."""
    if re.match(r"^\d{4}-\d{2}$", p):
        k = int(p[:4]) * 12 + int(p[5:7]) - 1 + n
        return "%04d-%02d" % (k // 12, k % 12 + 1)
    if re.match(r"^\d{4}-Q[1-4]$", p):
        k = int(p[:4]) * 4 + int(p[-1]) - 1 + n
        return "%04d-Q%d" % (k // 4, k % 4 + 1)
    if re.match(r"^\d{4}$", p):
        return "%04d" % (int(p) + n)
    raise ValueError(p)


def per_year(freq):
    return {"M": 12, "Q": 4, "A": 1}.get(freq)


def add_business_days(d, n):
    while n > 0:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def excel_date(x):
    return (dt.date(1899, 12, 30) + dt.timedelta(days=int(float(x)))).isoformat()


# =====================================================================================
# minimal stdlib .xlsx reader (zip + XML), from recon-2 xlsx_min.py
# =====================================================================================

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}


def _col(ref):
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def read_sheet(data, sheet_name):
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SourceError("not an .xlsx workbook (the file format changed, or an error page came back)")
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    names = [s.get("name") for s in wb.iter("{%s}sheet" % _NS["m"])]
    if sheet_name not in names:
        raise SourceError("workbook has no sheet %r (sheets: %s)" % (sheet_name, ", ".join(names[:12])))
    rid = next(s.get("{%s}id" % _NS["r"]) for s in wb.iter("{%s}sheet" % _NS["m"]) if s.get("name") == sheet_name)
    target = next(r.get("Target") for r in rels if r.get("Id") == rid).lstrip("/")
    if not target.startswith("xl/"):
        target = "xl/" + target
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter("{%s}si" % _NS["m"]):
            shared.append("".join(t.text or "" for t in si.iter("{%s}t" % _NS["m"])))
    rows = []
    for row in ET.fromstring(z.read(target)).iter("{%s}row" % _NS["m"]):
        out = {}
        for c in row.iter("{%s}c" % _NS["m"]):
            v = c.find("m:v", _NS)
            if v is None:
                isv = c.find("m:is", _NS)
                val = "".join(t.text or "" for t in isv.iter("{%s}t" % _NS["m"])) if isv is not None else None
            elif c.get("t") == "s":
                val = shared[int(v.text)]
            elif c.get("t") in ("str", "e"):
                val = v.text
            else:
                try:
                    val = float(v.text)
                except (TypeError, ValueError):
                    val = v.text
            out[_col(c.get("r"))] = val
        if out:
            rows.append([out.get(i) for i in range(max(out) + 1)])
    return rows


def _num(x):
    if isinstance(x, (int, float)) and not (isinstance(x, float) and math.isnan(x)):
        return float(x)
    if isinstance(x, str):
        s = x.strip().replace(",", "")
        if s in ("", ".", "NaN", "#N/A", "(NA)", "NA"):
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


# =====================================================================================
# adapters: entry -> [(period, value-or-None), ...] ascending, or raise SourceError
# =====================================================================================

def _fill(url, today):
    def sub(m):
        k = m.group(1)
        if k.startswith("today-") and k.endswith("d"):
            return (today - dt.timedelta(days=int(k[6:-1]))).isoformat()
        if k == "start_month":
            return "%04d-%02d" % (today.year - HIST_YEARS_SLOW - 1, today.month)
        if k == "start_quarter":
            return "%04d-Q1" % (today.year - HIST_YEARS_SLOW - 1)
        if k == "start_day":
            return (today - dt.timedelta(days=365 * HIST_YEARS_DAILY + 30)).isoformat()
        raise CatalogueError("unknown placeholder {%s} in %s" % (k, url))
    return re.sub(r"\{([a-z_0-9-]+)\}", sub, url)


def fetch_start(e, today):
    yrs = e.get("fetch_years") or (HIST_YEARS_DAILY if e["freq"] in ("D", "W") else HIST_YEARS_SLOW + 1)
    return dt.date(today.year - yrs, 1, 1) if e["freq"] not in ("D", "W") else today - dt.timedelta(days=int(365.25 * yrs) + 30)


# ---------------- FRED -----------------------------------------------------------------

def parse_fredgraph(body, ids):
    """fredgraph.csv: one CSV (one frequency) or a zip of CSVs (several frequencies). -> {ID: [(date, v)]}"""
    texts = []
    if body[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(body))
        texts = [z.read(n).decode("utf-8-sig") for n in z.namelist() if n.lower().endswith(".csv")]
    else:
        texts = [body.decode("utf-8-sig")]
    out = {}
    for t in texts:
        rows = list(csv.reader(io.StringIO(t)))
        if not rows:
            continue
        hdr = [h.strip() for h in rows[0]]
        if not hdr or hdr[0].lower() not in ("observation_date", "date"):
            raise SourceError("FRED CSV header starts %r, not observation_date (the format changed)" % (hdr[:1],))
        for j, name in enumerate(hdr[1:], start=1):
            obs = []
            for r in rows[1:]:
                if len(r) > j and r[0]:
                    obs.append((r[0], _num(r[j])))
            out[name.upper()] = obs
    missing = [i for i in ids if i.upper() not in out]
    if missing and len(ids) == 1:
        raise SourceError("FRED returned no column for %s (series id changed or discontinued?)" % missing[0])
    return out


def parse_fred_api(body, sid):
    try:
        j = json.loads(body)
    except ValueError:
        raise SourceError("FRED API answer is not JSON")
    if "observations" not in j:
        raise SourceError("FRED API answer has no 'observations' (%s)" % str(j.get("error_message", ""))[:120])
    return [(o["date"], _num(o.get("value"))) for o in j["observations"]]


class Ctx:
    def __init__(self, http, today, db=None, keys=None, log=print):
        self.http, self.today, self.db, self.log = http, today, db, log
        self.keys = keys if keys is not None else {k: os.environ.get(k) for k in
                                                     ("FRED_API_KEY", "BLS_API_KEY", "BEA_API_KEY", "CENSUS_API_KEY")}
        # A secret pasted or piped into place can carry a trailing space or newline; a URL built from it
        # is then refused before any request is made (2026-11-21). Keep printable non-space characters only.
        self.keys = {k: ("".join(c for c in v if c.isprintable() and not c.isspace()) or None) if isinstance(v, str) else v
                     for k, v in self.keys.items()}
        self.fred = {}          # ID -> obs or SourceError
        self.price_cache = {}

    def fred_prefetch(self, entries, chunk=12):
        """Keyless: group by start date, 12 ids per request (fredgraph returns at most 12 columns: on 2026-11-20
        a 20-id request came back with the first 12 only). Any id a batch did not carry, or every id of a batch
        that failed, is fetched on its own, so one bad id cannot take the rest down. Keyed: one API call per id."""
        if self.keys.get("FRED_API_KEY"):
            for e in entries:
                sid = e["source_id"]
                url = ("https://api.stlouisfed.org/fred/series/observations?series_id=%s&api_key=%s&file_type=json&observation_start=%s"
                       % (sid, self.keys["FRED_API_KEY"], fetch_start(e, self.today).isoformat()))
                try:
                    self.fred[sid.upper()] = parse_fred_api(self.http.get(url), sid)
                except SourceError as ex:
                    self.fred[sid.upper()] = SourceError(str(ex).replace(self.keys["FRED_API_KEY"], "***"))
            return
        groups = {}
        for e in entries:
            groups.setdefault(fetch_start(e, self.today), []).append(e["source_id"])
        for start, ids in sorted(groups.items()):
            for i in range(0, len(ids), chunk):
                part = ids[i:i + chunk]
                url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s&cosd=%s" % (",".join(part), start.isoformat())
                alone = []
                try:
                    got = parse_fredgraph(self.http.get(url), part)
                    for sid in part:
                        if got.get(sid.upper()):
                            self.fred[sid.upper()] = got[sid.upper()]
                        else:
                            alone.append(sid)
                    if alone:
                        self.log("  [fred] batch did not carry %s; fetching on its own" % ", ".join(alone))
                except SourceError as ex:
                    self.log("  [fred] batch of %d failed (%s); retrying one id at a time" % (len(part), ex))
                    alone = list(part)
                for sid in alone:
                    u1 = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s&cosd=%s" % (sid, start.isoformat())
                    try:
                        self.fred[sid.upper()] = parse_fredgraph(self.http.get(u1), [sid])[sid.upper()]
                    except SourceError as ex1:
                        self.fred[sid.upper()] = ex1


def a_fred(e, ctx):
    v = ctx.fred.get(e["source_id"].upper())
    if v is None:
        ctx.fred_prefetch([e])
        v = ctx.fred.get(e["source_id"].upper())
    if isinstance(v, Exception):
        raise v
    sc = e.get("scale", 1.0)          # e.g. WRESBAL comes in millions; the catalogue shows it in billions
    out = []
    for d, x in v:
        p = period_of(d, e["freq"])
        if p:
            out.append((p, None if x is None else x * sc))
    if not any(x is not None for _, x in out):
        raise SourceError("FRED series %s answered with no values" % e["source_id"])
    return out


# ---------------- Treasury FiscalData ------------------------------------------------------

def a_fiscaldata(e, ctx):
    try:
        j = json.loads(ctx.http.get(_fill(e["url"], ctx.today)))
    except ValueError:
        raise SourceError("FiscalData answer is not JSON")
    rows = j.get("data")
    if not isinstance(rows, list):
        raise SourceError("FiscalData answer has no 'data' list (the API shape changed)")
    if not rows:
        raise SourceError("FiscalData returned zero rows")
    m = e.get("match")
    if m:
        rx = re.compile(m["regex"])
        if not any(m["field"] in r for r in rows):
            raise SourceError("FiscalData rows have no field %r (the API shape changed)" % m["field"])
        rows = [r for r in rows if rx.search(str(r.get(m["field"]) or ""))]
        if not rows:
            raise SourceError("no %s matched %s (the category was renamed?)" % (m["field"], m["regex"]))
    f = e["field"]
    if f not in rows[0] or "record_date" not in rows[0]:
        raise SourceError("FiscalData rows have no field %r (the API shape changed)" % f)
    scale = e.get("scale", 1.0)
    agg = {}
    for r in rows:
        p = period_of(r["record_date"], e["freq"])
        x = _num(r.get(f))
        if p is None or x is None:
            continue
        if e.get("sum_by_date") or m:
            agg[p] = agg.get(p, 0.0) + x * scale
        else:
            agg.setdefault(p, x * scale)
    if not agg:
        raise SourceError("FiscalData rows carried no numeric %s" % f)
    return sorted(agg.items())


# ---------------- Eurostat JSON-stat ---------------------------------------------------------

def a_eurostat(e, ctx):
    try:
        j = json.loads(ctx.http.get(_fill(e["url"], ctx.today)))
    except ValueError:
        raise SourceError("Eurostat answer is not JSON")
    if "error" in j:
        raise SourceError("Eurostat error: %s" % str(j["error"])[:160])
    try:
        tidx = j["dimension"]["time"]["category"]["index"]
        sizes = j["size"]
    except (KeyError, TypeError):
        raise SourceError("Eurostat answer has no time dimension (the JSON-stat shape changed)")
    if math.prod(sizes) != len(tidx):
        raise SourceError("Eurostat filter matched %d series, not one (a dimension changed)" % (math.prod(sizes) // max(len(tidx), 1)))
    vals = j.get("value") or {}
    if not vals:
        raise SourceError("Eurostat returned no observations (the series key changed? label: %s)" % str(j.get("label", ""))[:80])
    inv = {v: k for k, v in tidx.items()}
    out = []
    for k, x in vals.items():
        p = period_of(inv[int(k)], e["freq"])
        if p:
            out.append((p, _num(x)))
    return sorted(out)


# ---------------- SDMX CSV (ECB, BIS, OECD) ---------------------------------------------------

def a_sdmx_csv(e, ctx):
    t = ctx.http.get(_fill(e["url"], ctx.today)).decode("utf-8-sig", "replace")
    rd = csv.DictReader(io.StringIO(t))
    if not rd.fieldnames or "TIME_PERIOD" not in rd.fieldnames or "OBS_VALUE" not in rd.fieldnames:
        raise SourceError("SDMX CSV has no TIME_PERIOD / OBS_VALUE columns (the format changed or an error page came back)")
    out = {}
    for r in rd:
        p = period_of(r["TIME_PERIOD"], e["freq"])
        if p:
            out[p] = _num(r["OBS_VALUE"])
    if not any(v is not None for v in out.values()):
        raise SourceError("SDMX source returned no observations (the series key changed?)")
    return sorted(out.items())


# ---------------- Cleveland Fed nowcast ----------------------------------------------------------

def a_cleveland(e, ctx):
    try:
        j = json.loads(ctx.http.get(e["url"]).decode("utf-8-sig"))
        last = j[-1]
        asof = last["chart"]["_comment"][:10]
        month = last["chart"]["subcaption"]
        names = {ds.get("seriesname"): ds for ds in last["dataset"]}
    except (ValueError, KeyError, IndexError, TypeError):
        raise SourceError("Cleveland Fed nowcast file no longer has its chart-block shape")
    ds = names.get(e["seriesname"])
    if ds is None:
        raise SourceError("Cleveland Fed nowcast has no series %r (names now: %s)" % (e["seriesname"], ", ".join(map(str, names))[:120]))
    pts = [_num(p.get("value")) for p in ds.get("data", [])]
    pts = [p for p in pts if p is not None]
    if not pts or not re.match(r"^\d{4}-\d{2}-\d{2}$", asof):
        raise SourceError("Cleveland Fed nowcast series %r carries no value" % e["seriesname"])
    m = re.match(r"^(\d{4})-(\d{1,2})$", str(month).strip())
    e["_extra"] = {"nowcast_month": "%s-%02d" % (m.group(1), int(m.group(2))) if m else str(month)}
    return [(asof, pts[-1])]


# ---------------- Atlanta Fed workbooks -------------------------------------------------------------

def a_atlanta(e, ctx):
    rows = read_sheet(ctx.http.get(e["url"]), e["sheet"])
    if "header_check" in e:
        hc = e["header_check"]
        cell = rows[hc["row"]][hc["col"]] if len(rows) > hc["row"] and len(rows[hc["row"]]) > hc["col"] else None
        if not (isinstance(cell, str) and hc["contains"] in cell):
            raise SourceError("sheet %s cell (%d,%d) no longer names %s (the layout changed)" % (e["sheet"], hc["row"], hc["col"], hc["contains"]))
        dc, vc = e["date_col"], e["value_col"]
    else:
        hdr_i = next((i for i, r in enumerate(rows[:10]) if e["column"] in [str(c).strip() if c is not None else "" for c in r]), None)
        if hdr_i is None:
            raise SourceError("sheet %s has no column headed %r (the layout changed)" % (e["sheet"], e["column"]))
        dc, vc = 0, [str(c).strip() if c is not None else "" for c in rows[hdr_i]].index(e["column"])
    out = {}
    for r in rows:
        if len(r) > max(dc, vc) and isinstance(r[dc], float) and r[dc] > 20000:
            p = period_of(excel_date(r[dc]), e["freq"])
            x = _num(r[vc])
            if p and x is not None:
                out[p] = x
    if not out:
        raise SourceError("sheet %s carried no dated numeric rows" % e["sheet"])
    return sorted(out.items())


# ---------------- Census C30 workbook -----------------------------------------------------------------

_MON = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def a_census(e, ctx):
    rows = read_sheet(ctx.http.get(e["url"]), e["sheet"])

    def norm(c):
        return re.sub(r"\s+", " ", str(c).replace("_x000D_", " ")).strip() if c is not None else ""
    hdr = next((r for r in rows[:20] if e["column"] in [norm(c) for c in r]), None)
    if hdr is None:
        raise SourceError("sheet %r has no column headed %r (the layout changed)" % (e["sheet"], e["column"]))
    col = [norm(c) for c in hdr].index(e["column"])
    out = {}
    for r in rows:
        m = re.match(r"^([A-Z][a-z]{2})-(\d\d)", str(r[0]) if r and r[0] is not None else "")
        if m and m.group(1) in _MON and len(r) > col:
            x = _num(r[col])
            if x is not None:
                out["20%s-%02d" % (m.group(2), _MON[m.group(1)])] = x
    if not out:
        raise SourceError("sheet %r carried no monthly rows" % e["sheet"])
    return sorted(out.items())


# ---------------- the repo's own price database ---------------------------------------------------------

def load_prices(ctx, ticker, days=3 * 366 + 30):
    if ticker in ctx.price_cache:
        return ctx.price_cache[ticker]
    import pricedb
    conn = pricedb.connect(ctx.db or pricedb.DEFAULT_DB, readonly=True)
    try:
        start = (ctx.today - dt.timedelta(days=days)).isoformat()
        rows = conn.execute("SELECT date, adj_close, close FROM daily_prices WHERE ticker=? AND date>=? ORDER BY date",
                            (ticker, start)).fetchall()
    finally:
        conn.close()
    out = [(d, a if a is not None else c) for d, a, c in rows if (a if a is not None else c) is not None]
    ctx.price_cache[ticker] = out
    return out


def a_pricedb(e, ctx):
    rows = load_prices(ctx, e["source_id"])
    if not rows:
        raise SourceError("no rows for %s in the price database (not added to tickers.txt or not backfilled?)" % e["source_id"])
    return rows


# ---------------- Bank of England database CSV ------------------------------------------------------------

def boe_url(code, start):
    return ("https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp?csv.x=yes&Datefrom=%s&Dateto=now"
            "&SeriesCodes=%s&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N" % (start.strftime("%d/%b/%Y"), code))


def a_boe(e, ctx):
    """Keyless Bank of England database CSV: 'DATE,<code>' then rows like '05 Nov 2026,4'."""
    code = e["source_id"]
    t = ctx.http.get(boe_url(code, fetch_start(e, ctx.today))).decode("utf-8-sig", "replace")
    rows = list(csv.reader(io.StringIO(t)))
    hdr = [h.strip().upper() for h in rows[0]] if rows else []
    if len(hdr) < 2 or hdr[0] != "DATE" or hdr[1] != code.upper():
        raise SourceError("Bank of England CSV header is %r, not DATE,%s (the format changed or an error page came back)" % (hdr[:2], code))
    out = {}
    for r in rows[1:]:
        if len(r) < 2:
            continue
        try:
            d = dt.datetime.strptime(r[0].strip(), "%d %b %Y").date().isoformat()
        except ValueError:
            continue
        p = period_of(d, e["freq"])
        if p:
            out[p] = _num(r[1])
    if not any(v is not None for v in out.values()):
        raise SourceError("Bank of England CSV carried no dated values for %s" % code)
    return sorted(out.items())


ADAPTERS = {"fred": a_fred, "fiscaldata": a_fiscaldata, "eurostat_json": a_eurostat, "sdmx_csv": a_sdmx_csv,
            "clevelandfed_json": a_cleveland, "atlantafed_xlsx": a_atlanta, "census_xlsx": a_census, "pricedb": a_pricedb,
            "boe_csv": a_boe}


# ---------------- fed funds futures strip ----------------------------------------------------------------

def zq_symbols(today, n=ZQ_MONTHS):
    """[(contract month 'YYYY-MM', Yahoo symbol)] for this month and the next n-1, e.g. ZQX26.CBT. Never ZQ=F."""
    out = []
    for k in range(n):
        y, m = divmod(today.year * 12 + today.month - 1 + k, 12)
        out.append(("%04d-%02d" % (y, m + 1), "ZQ%s%02d.CBT" % (ZQ_CODES[m], y % 100)))
    return out


def _yf_last(sym):
    import yfinance as yf
    h = yf.Ticker(sym).history(period="5d", auto_adjust=False)
    if h is None or h.empty:
        raise SourceError("no bars for %s" % sym)
    return h.index[-1].date().isoformat(), float(h["Close"].iloc[-1])


def fed_path(today, quote=None, log=print, attempts=ATTEMPTS, pause=1.0):
    quote = quote or _yf_last
    rows, errs = [], []
    for month, sym in zq_symbols(today):
        got = None
        for a in range(1, attempts + 1):
            try:
                got = quote(sym)
                break
            except Exception as ex:  # noqa: BLE001 - per-contract isolation
                if a == attempts:
                    errs.append("%s: %s" % (sym, str(ex)[:80]))
                else:
                    time.sleep(2 ** a)
        if got:
            d, px = got
            if not (80.0 < px <= 100.0):
                errs.append("%s: price %.3f out of range" % (sym, px))
            else:
                rows.append({"month": month, "symbol": sym, "date": d, "price": round(px, 4), "implied": round(100.0 - px, 3)})
        time.sleep(pause)
    status, reason = "ok", None
    if len(rows) < 6:
        status, reason = "error", "only %d of %d contract months answered: %s" % (len(rows), ZQ_MONTHS, "; ".join(errs)[:300])
    elif errs:
        reason = "%d contract(s) missing: %s" % (len(errs), "; ".join(errs)[:200])
    asof = max((r["date"] for r in rows), default=None)
    if status == "ok" and asof and (today - dt.date.fromisoformat(asof)).days > 6:
        status, reason = "stale", "newest settlement %s is more than six days old" % asof
    return {"what": "CBOT 30-day fed funds futures, one contract per month; implied rate = 100 minus price, the market-priced average "
                    "overnight rate for that month. Contract symbols are generated from the date (never the generic ZQ=F).",
            "source": "CME settlement prices via Yahoo (yfinance)", "asof": asof, "status": status, "reason": reason, "contracts": rows}


# =====================================================================================
# release calendar
# =====================================================================================

def prior_business_day(d, holidays):
    d -= dt.timedelta(days=1)
    while d.weekday() >= 5 or d.isoformat() in holidays:
        d -= dt.timedelta(days=1)
    return d


def fred_release_dates(rel, http, today, key):
    """Keyed FRED: fred/release/dates for each catalogue release that names its FRED release id (fred_release).
    include_release_dates_with_no_data=true is what makes FRED list future dates. -> (events, list of failures)."""
    events, bad = [], []
    lo = (today - dt.timedelta(days=400)).isoformat()
    for k, r in rel.items():
        rid = r.get("fred_release")
        if not rid:
            continue
        url = ("https://api.stlouisfed.org/fred/release/dates?release_id=%d&api_key=%s&file_type=json&realtime_start=%s"
               "&realtime_end=9999-12-31&include_release_dates_with_no_data=true&sort_order=asc&limit=1000" % (int(rid), key, lo))
        try:
            try:
                j = json.loads(http.get(url))
            except ValueError:
                raise SourceError("FRED API answer is not JSON")
            rows = j.get("release_dates") if isinstance(j, dict) else None
            if not isinstance(rows, list):
                raise SourceError("FRED API answer has no 'release_dates' (%s)" % str((j or {}).get("error_message", ""))[:120])
        except SourceError as ex:
            bad.append("%s (release %s): %s" % (k, rid, str(ex).replace(key, "***")[:160]))
            continue
        for x in rows:
            d = str(x.get("date") or "")
            if re.match(r"^\d{4}-\d{2}-\d{2}$", d):
                events.append({"date": d, "time_et": r.get("time_et"), "release": k, "name": r["name"],
                               "source": "FRED API fred/release/dates (release_id %s)" % rid, "verified": today.isoformat()})
    return events, bad


def build_calendar(cat, http, today, fixed_path=FIXED_CAL, keys=None):
    """-> (events sorted by date, sources status). Events: date, time_et, release, name, ref, source, verified.
    keys: {"FRED_API_KEY": ...}; with a FRED key, FRED's release dates fill the BLS, Census, claims and Fed releases the
    hand-kept file lacks (never read from the environment here: build() passes the job's keys)."""
    events, sources = [], {}
    rel = cat["releases"]
    keys = keys or {}
    holidays = set()
    # BEA (keyless JSON schedule)
    bea_names = {r["calendar"][4:]: k for k, r in rel.items() if (r.get("calendar") or "").startswith("bea:")}
    try:
        j = json.loads(http.get("https://apps.bea.gov/API/signup/release_dates.json"))
        missing = [n for n in bea_names if n not in j]
        for n, k in bea_names.items():
            for iso in sorted(set((j.get(n) or {}).get("release_dates") or [])):
                t = dt.datetime.fromisoformat(iso)
                et = t - dt.timedelta(hours=4 if 3 <= t.month <= 10 else 5)   # display only; BEA gives UTC
                events.append({"date": t.date().isoformat(), "time_et": et.strftime("%H:%M"), "release": k, "name": rel[k]["name"],
                               "source": "https://apps.bea.gov/API/signup/release_dates.json", "verified": today.isoformat()})
        sources["bea"] = {"status": "error" if missing else "ok",
                          "reason": ("schedule has no entry named %s (renamed?)" % ", ".join(missing)) if missing else None}
    except (SourceError, ValueError) as ex:
        sources["bea"] = {"status": "error", "reason": str(ex)[:200]}
    # Treasury FiscalData release calendar
    fd = {r["calendar"][11:]: k for k, r in rel.items() if (r.get("calendar") or "").startswith("fiscaldata:")}
    try:
        j = json.loads(http.get("https://api.fiscaldata.treasury.gov/services/calendar/release"))
        if not isinstance(j, list):
            raise SourceError("release calendar is not a list (the API shape changed)")
        n = 0
        for r in j:
            k = fd.get(r.get("datasetId"))
            if k:
                n += 1
                events.append({"date": r["date"], "time_et": r.get("time"), "release": k, "name": rel[k]["name"],
                               "source": "https://api.fiscaldata.treasury.gov/services/calendar/release (dataset %s)" % r["datasetId"],
                               "verified": today.isoformat()})
        sources["fiscaldata"] = {"status": "ok" if n else "error",
                                 "reason": None if n else "no rows for dataset(s) %s (id changed?)" % ", ".join(fd)}
    except (SourceError, ValueError) as ex:
        sources["fiscaldata"] = {"status": "error", "reason": str(ex)[:200]}
    # hand-verified fixed rows
    try:
        with open(fixed_path, encoding="utf-8") as fh:
            fx = json.load(fh)
        for r in fx["events"]:
            events.append(dict(r, name=(rel.get(r["release"]) or {}).get("name") or r.get("ref")))
        holidays = set((fx.get("federal_holidays") or {}).get("dates") or [])
        cov = fx.get("coverage") or {}
        soon = sorted(k for k, v in cov.items() if v and (dt.date.fromisoformat(v) - today).days < 60)
        sources["fixed"] = {"status": "ok", "coverage": cov,
                            "reason": ("hand-kept rows run out within 60 days: %s -- re-verify and extend macro_calendar_fixed.json"
                                       % ", ".join("%s %s" % (k, cov[k]) for k in soon)) if soon else None}
        if soon:
            sources["fixed"]["status"] = "warn"
    except (OSError, ValueError, KeyError) as ex:
        sources["fixed"] = {"status": "error", "reason": "macro_calendar_fixed.json unreadable (%s)" % type(ex).__name__}
    # keyed FRED release dates: added only where the rows above do not already carry that release on that date
    from_fred = set()
    if keys.get("FRED_API_KEY"):
        fe, bad = fred_release_dates(rel, http, today, keys["FRED_API_KEY"])
        have = {(x["release"], x["date"]) for x in events}
        times = {}                                   # FRED gives no time of day: borrow the hand-kept rows' time
        for x in events:
            if x.get("time_et"):
                times.setdefault(x["release"], x["time_et"])
        for x in fe:
            if not x.get("time_et"):
                x["time_et"] = times.get(x["release"])
            if (x["release"], x["date"]) not in have:
                events.append(x)
                have.add((x["release"], x["date"]))
            from_fred.add(x["release"])
        sources["fred"] = {"status": "error" if (bad and not fe) else ("warn" if bad else "ok"),
                           "reason": ("; ".join(bad)[:400]) if bad else None}
    # weekly claims: every Thursday; when Thursday is a federal holiday the Department of Labor releases on the prior
    # business day (Thanksgiving 2026-11-26 -> Wednesday 2026-11-25). Holidays: macro_calendar_fixed.json federal_holidays.
    for k, r in rel.items():
        if (r.get("calendar") or "") == "weekly:Thu" and k not in from_fred:
            d = today - dt.timedelta(days=28)
            while d <= today + dt.timedelta(days=56):
                if d.weekday() == 3:
                    ev = {"date": d.isoformat(), "time_et": "08:30", "release": k, "name": r["name"],
                          "source": "rule: every Thursday, moved to the prior business day when Thursday is a federal holiday",
                          "verified": None}
                    if d.isoformat() in holidays:
                        ev["date"] = prior_business_day(d, holidays).isoformat()
                        ev["note"] = ("moved from Thursday %s (a federal holiday on the OPM schedule) by the Department of "
                                      "Labor's practice; the moved date is not confirmed against a DOL notice" % d.isoformat())
                    events.append(ev)
                d += dt.timedelta(days=1)
    events.sort(key=lambda x: (x["date"], x["release"]))
    return events, sources


def release_dates(events):
    out = {}
    for e in events:
        out.setdefault(e["release"], set()).add(e["date"])
    return {k: sorted(dt.date.fromisoformat(d) for d in v) for k, v in out.items()}


# =====================================================================================
# per-series record: changes, status, release dates
# =====================================================================================

def _r(x, n=4):
    return None if x is None else round(x, n)


def _pct(a, b):
    return None if (a is None or b is None or b == 0) else (a / b - 1.0) * 100.0


def _nearest_before(dated, target, tol_days):
    """dated: list of (date, v) ascending, v not None. Value at or just before target, within tol."""
    best = None
    for d, v in dated:
        if d <= target:
            best = (d, v)
        else:
            break
    if best and (target - best[0]).days <= tol_days:
        return best[1]
    return None


def compute_changes(e, obs, gaps):
    """-> (fields dict, notes list). obs ascending [(period, v|None)], last non-null is 'latest'."""
    f, notes = {}, []
    kind, freq = e["kind"], e["freq"]
    vals = [(p, v) for p, v in obs if v is not None]
    p0, v0 = vals[-1]
    by = dict(obs)
    gap = gaps.get(e.get("gap") or "")

    def base(p):
        v = by.get(p)
        if v is None:
            if gap and gap["period"] == p:
                notes.append(gap["reason"])
            elif p in by:
                notes.append("the source left %s blank" % p)
        return v

    def window_note(ps):
        if gap and gap["period"] in ps and by.get(gap["period"]) is None:
            notes.append(gap["reason"])

    prior = vals[-2] if len(vals) > 1 else (None, None)
    f["prior"], f["prior_asof"] = _r(prior[1]), prior[0]
    if len(vals) == 1:                                  # a one-value source (the nowcasts): no changes to compute
        f["change"], f["change_unit"] = None, None
        return f, []
    if kind == "index":
        f["change"], f["change_unit"] = _r(_pct(v0, prior[1]), 3), "%"
    else:
        f["change"], f["change_unit"] = _r(None if prior[1] is None else v0 - prior[1], 4), ("pp" if kind == "rate" else e.get("unit_out") or e["unit"])
    ext = {}
    py = per_year(freq)
    if py:                                              # M / Q / A
        if freq != "A":
            b = base(period_shift(p0, -py))
            if kind == "rate":
                f["yoy"], f["yoy_unit"] = _r(None if b is None else v0 - b, 3), "pp"
            elif kind == "flow" and b is not None and (b <= 0 or v0 <= 0):
                f["yoy"], f["yoy_unit"] = None, "%"
                notes.append("a percent change is not meaningful for a flow that is negative or changes sign")
            else:
                f["yoy"], f["yoy_unit"] = _r(_pct(v0, b), 3), "%"
        if freq == "M" and kind == "index":
            ext["pct_3m"] = _r(_pct(v0, by.get(period_shift(p0, -3))), 3)
        if freq in ("M", "Q") and kind == "index":
            # 3 months back for monthly, 1 quarter back for quarterly; either way the change is raised to the 4th power
            b = base(period_shift(p0, -3 if freq == "M" else -1))
            f["ann3m"] = _r(None if (b is None or b <= 0 or v0 <= 0) else ((v0 / b) ** 4 - 1) * 100, 3)
        if freq == "M" and kind == "count":
            ps = [period_shift(p0, -k) for k in range(4)]
            window_note(ps)
            ds = [None if (by.get(ps[k]) is None or by.get(ps[k + 1]) is None) else by[ps[k]] - by[ps[k + 1]] for k in range(3)]
            ext["d3avg"] = _r(sum(ds) / 3, 3) if None not in ds else None
        if freq == "M" and kind == "rate":
            ps = [period_shift(p0, -k) for k in range(3)]
            window_note(ps)
            a3 = [by.get(p) for p in ps]
            ext["avg3"] = _r(sum(a3) / 3, 3) if None not in a3 else None
            b3 = base(period_shift(p0, -3))
            ext["chg_3"] = _r(None if b3 is None else v0 - b3, 3)
        if freq == "M" and kind == "flow":
            window_note([period_shift(p0, -k) for k in range(24)])
            last = [by.get(period_shift(p0, -k)) for k in range(12)]
            prev = [by.get(period_shift(p0, -12 - k)) for k in range(12)]
            s1 = sum(last) if None not in last else None
            s0 = sum(prev) if None not in prev else None
            ext["sum12"] = _r(s1, 2)
            ext["sum12_chg"] = _r(None if (s1 is None or s0 is None) else s1 - s0, 2)
            ext["sum12_yoy"] = _r(None if (s1 is None or s0 is None or s0 <= 0 or s1 <= 0) else (s1 / s0 - 1) * 100, 3)
    else:                                               # D / W
        dated = [(dt.date.fromisoformat(p), v) for p, v in vals]
        d0 = dated[-1][0]
        b = _nearest_before(dated, d0 - dt.timedelta(days=365), 10)
        if kind == "rate":
            f["yoy"], f["yoy_unit"] = _r(None if b is None else v0 - b, 3), "pp"
        else:
            f["yoy"], f["yoy_unit"] = _r(_pct(v0, b), 3), "%"
        k = 63 if freq == "D" else 13
        if len(dated) > k:
            bk = dated[-1 - k][1]
            ext["chg_63" if freq == "D" else "chg_13w"] = _r((v0 - bk) if kind in ("rate", "level") else _pct(v0, bk), 3)
    if f.get("yoy") is None and "yoy" in f and not notes:
        notes.append("no observation a year earlier in the fetched window")
    if ext:
        f["extra"] = ext
    return f, sorted(set(notes))


def judge(e, asof, today, rel, dates, grace_days=1):
    """-> (status, reason, released, next_release, judged_by)."""
    ds = dates.get(e["release"]) or []
    nxt = next((d for d in ds if d > today), None)
    lag = rel.get("min_lag_days", 0) or 0
    released = None
    done = [d for d in ds if d <= today]
    if e["freq"] in ("M", "Q") and ds:
        end = period_end(asof)
        carried = [d for d in done if d > end + dt.timedelta(days=lag)]
        released = carried[-1].isoformat() if carried else None
        if nxt is not None:                         # the calendar covers this series: judge by it
            nxt_end = period_end(period_shift(asof, 1))
            limit = today - dt.timedelta(days=grace_days)
            while limit.weekday() >= 5:
                limit -= dt.timedelta(days=1)
            missed = [d for d in ds if d > nxt_end + dt.timedelta(days=lag) and d <= limit]
            if missed:
                return ("stale", "the %s release of %s was due to carry %s; the latest here is %s"
                        % (rel.get("name") or e["release"], missed[0].isoformat(), period_shift(asof, 1), asof),
                        released, nxt.isoformat(), "calendar")
            return "ok", None, released, nxt.isoformat(), "calendar"
    age = (today - period_end(asof)).days
    if age > e["stale_after_days"]:
        return ("stale", "no observation after %s: %d days past the end of that period, over the %d-day limit (no release calendar covers this series)"
                % (asof, age, e["stale_after_days"]), released, nxt.isoformat() if nxt else None, "days")
    return "ok", None, released, nxt.isoformat() if nxt else None, "days"


def make_record(e, obs, today, cat, dates, first):
    vals = [(p, v) for p, v in obs if v is not None]
    if not vals:
        raise SourceError("no values")
    p0, v0 = vals[-1]
    f, notes = compute_changes(e, obs, cat.get("gaps") or {})
    rel = cat["releases"][e["release"]]
    status, reason, released, nxt, judged = judge(e, p0, today, rel, dates)
    rec = {"id": e["id"], "name": e["name"], "what": e["what"], "group": e["group"], "unit": e.get("unit_out") or e["unit"],
           "freq": e["freq"], "kind": e["kind"], "source": e["source"], "source_id": e["source_id"], "release": e["release"],
           "asof": p0, "value": _r(v0, 4)}
    rec.update(f)
    rec.update({"released": released, "next_release": nxt, "status": status, "reason": reason, "judged_by": judged})
    if e.get("_extra"):
        rec["extra"] = dict(rec.get("extra") or {}, **e.pop("_extra"))
    if notes:
        rec["notes"] = notes
    fp = (first or {}).get(e["id"]) or {}
    if e.get("revised") and fp:
        if p0 in fp and fp[p0] is not None and abs(fp[p0] - v0) > 1e-9 * max(1.0, abs(v0)):
            rec["revised_from"] = fp[p0]
        pp = f.get("prior_asof")
        if pp in fp and f.get("prior") is not None and abs(fp[pp] - f["prior"]) > 1e-4 * max(1.0, abs(f["prior"])):
            rec["prior_revised_from"] = fp[pp]
    return rec


# =====================================================================================
# history
# =====================================================================================

def compact_history(e, obs, today):
    if e["freq"] in ("D", "W"):
        cut_all = (today - dt.timedelta(days=HIST_DAYS_DAILY)).isoformat()
        cut_far = (today - dt.timedelta(days=int(365.25 * HIST_YEARS_DAILY))).isoformat()
        out, weeks = [], {}
        for p, v in obs:
            if p >= cut_all:
                out.append([p, _r(v)])
            elif p >= cut_far and v is not None:
                y, w, _ = dt.date.fromisoformat(p).isocalendar()
                weeks[(y, w)] = [p, _r(v)]                 # last observation of each ISO week
        return sorted(weeks.values()) + out
    first_p = period_of("%04d-%02d-01" % (today.year - HIST_YEARS_SLOW, today.month), e["freq"]) or "0000"
    return [[p, _r(v)] for p, v in obs if p >= first_p]


def update_first(e, obs, first):
    if not e.get("revised"):
        return None
    fp = dict(first.get(e["id"]) or {})
    for p, v in obs[-FIRST_KEEP:]:
        if v is not None and p not in fp:
            fp[p] = _r(v)
    keep = sorted(fp)[-FIRST_KEEP:]
    return {p: fp[p] for p in keep}


# =====================================================================================
# main build
# =====================================================================================

def load_catalogue(path=CATALOGUE):
    try:
        with open(path, encoding="utf-8") as fh:
            cat = json.load(fh)
    except (OSError, ValueError) as ex:
        raise CatalogueError("macro_catalogue.json unreadable: %s" % ex)
    need = ("id", "source", "source_id", "name", "what", "unit", "freq", "kind", "release", "group", "store", "stale_after_days")
    seen = set()
    for e in cat.get("series") or []:
        miss = [k for k in need if k not in e]
        if miss:
            raise CatalogueError("series %s lacks %s" % (e.get("id"), ", ".join(miss)))
        if e["id"] in seen:
            raise CatalogueError("duplicate id %s" % e["id"])
        seen.add(e["id"])
        if e["source"] not in ADAPTERS:
            raise CatalogueError("series %s: unknown source %s" % (e["id"], e["source"]))
        if e["release"] not in cat.get("releases", {}):
            raise CatalogueError("series %s: unknown release %s" % (e["id"], e["release"]))
        if e["group"] not in cat.get("groups", {}):
            raise CatalogueError("series %s: unknown group %s" % (e["id"], e["group"]))
        if e["freq"] not in ("D", "W", "M", "Q", "A") or e["kind"] not in ("index", "count", "rate", "flow", "level"):
            raise CatalogueError("series %s: bad freq/kind" % e["id"])
    if not seen:
        raise CatalogueError("catalogue has no series")
    return cat


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def build(cat, http, today, db=None, out_dir=DATA, only=None, quote=None, log=print, fixed_path=FIXED_CAL, keys=None):
    t0 = time.time()
    ctx = Ctx(http, today, db=db, keys=keys, log=log)
    prev = _read_json(os.path.join(out_dir, "macro.json")) or {}
    prev_hist = _read_json(os.path.join(out_dir, "macro_history.json")) or {}
    first_prev = prev_hist.get("first") or {}

    events, cal_sources = build_calendar(cat, http, today, fixed_path, keys=ctx.keys)
    dates = release_dates(events)

    entries = [e for e in cat["series"] if e["store"] is True and (not only or e["source"] in only)]
    skipped = [e["id"] for e in cat["series"] if e["store"] is not True]
    ctx.fred_prefetch([e for e in entries if e["source"] == "fred"])

    series, hist, first, raw = {}, {}, {}, {}
    for e in entries:
        e = dict(e)
        try:
            obs = ADAPTERS[e["source"]](e, ctx)
            raw[e["id"]] = obs
            rec = make_record(e, obs, today, cat, dates, first_prev)
            if e.get("history", True):
                hist[e["id"]] = compact_history(e, obs, today)
                fp = update_first(e, obs, first_prev)
                if fp:
                    first[e["id"]] = fp
        except SourceError as ex:
            rec = {"id": e["id"], "name": e["name"], "what": e["what"], "group": e["group"], "unit": e.get("unit_out") or e["unit"],
                   "freq": e["freq"], "kind": e["kind"], "source": e["source"], "source_id": e["source_id"], "release": e["release"]}
            old = (prev.get("series") or {}).get(e["id"])
            if old and old.get("value") is not None:
                for k in ("asof", "value", "prior", "prior_asof", "change", "change_unit", "yoy", "yoy_unit", "ann3m", "extra", "released"):
                    if k in old:
                        rec[k] = old[k]
                rec["kept_from"] = prev.get("generated_at")
            else:
                rec["value"] = None
            rec.update({"status": "error", "reason": str(ex)[:300], "next_release": None})
            old_h = (prev_hist.get("series") or {}).get(e["id"])
            if old_h and e.get("history", True):
                hist[e["id"]] = old_h
            if first_prev.get(e["id"]):
                first[e["id"]] = first_prev[e["id"]]
        rec["priority"] = e.get("priority", "core")
        rec["licence"] = e.get("licence")
        series[e["id"]] = rec
        st = rec["status"]
        log("  [%-5s] %-22s %-10s %-12s %s" % (st, e["id"], rec.get("asof") or "-", rec.get("value"), (rec.get("reason") or "")[:90]))

    path = None
    if not only or "futures" in only:
        log("  fed funds futures strip ...")
        path = fed_path(today, quote=quote, log=log)
        log("  [%-5s] fed_path %s contracts, asof %s %s" % (path["status"], len(path["contracts"]), path["asof"], path["reason"] or ""))

    import macro_reads
    prices = None
    try:
        prices = macro_reads.aligned_prices(lambda t: load_prices(ctx, t), macro_reads.PRICE_TICKERS)
    except Exception as ex:  # noqa: BLE001 - reads degrade, they never stop the payload
        log("  [warn] price alignment for trend legs failed: %s" % type(ex).__name__)
    reads = macro_reads.compute_reads(cat, series, raw, path, prices, today)
    views = macro_reads.compute_views(cat, series, reads)

    counts = {}
    for r in series.values():
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    core_bad = sorted(k for k, r in series.items() if r["status"] != "ok" and r.get("priority") == "core")
    cal_bad = sorted(k for k, v in cal_sources.items() if v["status"] == "error")
    status = "degraded" if (core_bad or cal_bad or (path and path["status"] != "ok")) else "ok"
    gen = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stats = {h: dict(v) for h, v in sorted(http.stats.items())}
    meta = {"requests": sum(v["requests"] for v in stats.values()), "bytes": sum(v["bytes"] for v in stats.values()),
            "seconds": round(time.time() - t0, 1), "by_host": stats,
            "fred_route": "api (FRED_API_KEY)" if ctx.keys.get("FRED_API_KEY") else "keyless fredgraph.csv"}
    macro = {
        "generated_at": gen, "asof": today.isoformat(), "status": status, "counts": counts,
        "degraded": ["core series %s: %s" % (k, series[k]["status"]) for k in core_bad]
                    + ["calendar source %s: %s" % (k, cal_sources[k]["reason"]) for k in cal_bad]
                    + (["fed funds futures: %s" % path["reason"]] if path and path["status"] != "ok" else []),
        "not_published": {"ids": skipped, "why": "third-party copyright (store:false): never fetched into this public repo"},
        "note": ("Economic series for the board's Macro view (build M1). status: ok; stale = a scheduled release passed and the series did "
                 "not advance (judged_by 'calendar'), or, where no calendar covers the series, its last period ended more than "
                 "stale_after_days ago (judged_by 'days'); error = the fetch or parse failed, the reason says which, and any value shown "
                 "is the last good one (kept_from). Changes: change is against the prior observation (% for indexes, pp for rates, "
                 "units otherwise); yoy against a year earlier; ann3m is the 3-month (or one-quarter) change at an annual rate."),
        "group_titles": cat["groups"], "meta": meta, "fed_path": path, "reads": reads, "views": views,
        "side_rule": cat.get("side_rule"), "series": series,
    }
    history = {"generated_at": gen, "note": ("Per series [[period, value], ...]; null marks a period the source left blank (never "
                                             "interpolated). Monthly and quarterly series keep %d years; daily and weekly keep every "
                                             "observation for a year and one per week back to %d years. Prices of funds and futures are "
                                             "in closes.json, not here. `first` keeps the first value this job saw for each of the last %d "
                                             "periods of a revised series (vintages accumulate from the job's start)." % (HIST_YEARS_SLOW, HIST_YEARS_DAILY, FIRST_KEEP)),
               "series": hist, "first": first}
    horizon_lo, horizon_hi = (today - dt.timedelta(days=120)).isoformat(), (today + dt.timedelta(days=400)).isoformat()
    releases = {"generated_at": gen, "asof": today.isoformat(),
                "note": "Release dates, generated each night. Each row carries its source and the date it was verified; rows from macro_calendar_fixed.json are hand-verified.",
                "sources": cal_sources, "events": [x for x in events if horizon_lo <= x["date"] <= horizon_hi]}
    return macro, history, releases


def write_json(path, doc, compact=False):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        if compact:
            json.dump(doc, fh, separators=(",", ":"), ensure_ascii=False)
        else:
            json.dump(doc, fh, indent=1, ensure_ascii=False)
            fh.write("\n")
    os.replace(tmp, path)
    return os.path.getsize(path)


def offline_store(directory):
    """A saved-response directory: index.json maps url -> filename (tests)."""
    with open(os.path.join(directory, "index.json"), encoding="utf-8") as fh:
        idx = json.load(fh)
    out = {}
    for url, name in idx.items():
        if isinstance(name, dict) and "error" in name:
            out[url] = SourceError(name["error"])
        else:
            with open(os.path.join(directory, name), "rb") as fh:
                out[url] = fh.read()
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fetch economic series and write data/macro*.json")
    ap.add_argument("--catalogue", default=CATALOGUE)
    ap.add_argument("--out-dir", default=DATA)
    ap.add_argument("--db", default=None)
    ap.add_argument("--only", default="", help="comma-separated sources (fred,fiscaldata,...,futures)")
    ap.add_argument("--today", default=None, help="YYYY-MM-DD (tests)")
    ap.add_argument("--offline", default=None, help="directory of saved responses (tests)")
    a = ap.parse_args(argv)
    try:
        cat = load_catalogue(a.catalogue)
    except CatalogueError as ex:
        print("macro: CATALOGUE ERROR: %s" % ex, file=sys.stderr)
        return 2
    today = dt.date.fromisoformat(a.today) if a.today else dt.datetime.now(dt.timezone.utc).date()
    http = Http(offline=offline_store(a.offline) if a.offline else None)
    only = set(x for x in a.only.split(",") if x) or None
    try:
        macro, hist, rel = build(cat, http, today, db=a.db, out_dir=a.out_dir, only=only)
    except CatalogueError as ex:
        print("macro: CATALOGUE ERROR: %s" % ex, file=sys.stderr)
        return 2
    os.makedirs(a.out_dir, exist_ok=True)
    n1 = write_json(os.path.join(a.out_dir, "macro.json"), macro)
    n2 = write_json(os.path.join(a.out_dir, "macro_history.json"), hist, compact=True)
    n3 = write_json(os.path.join(a.out_dir, "macro_releases.json"), rel)
    print("macro: %s %s; %d requests, %.1f MB fetched, %.0f s; macro.json %d KB, macro_history.json %d KB, macro_releases.json %d KB"
          % (macro["status"].upper(), json.dumps(macro["counts"]), macro["meta"]["requests"], macro["meta"]["bytes"] / 1e6,
             macro["meta"]["seconds"], n1 // 1024, n2 // 1024, n3 // 1024))
    for line in macro["degraded"]:
        print("  DEGRADED  " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
