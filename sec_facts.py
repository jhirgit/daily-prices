"""SEC companyfacts fetch + cache (stdlib only).

One job: get https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json for a CIK,
politely (<= 5 requests/second, the SEC's published fair-access limit is 10), with the
User-Agent SEC asks for, and cache the raw JSON under data/cache/sec/ (gitignored via
data/cache/). fundamentals.py is the only reader.
"""
import gzip
import json
import os
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "data", "cache", "sec")
UA = "jr-dash research jakeradencom@gmail.com"
MIN_GAP_S = 0.21          # <= 5 requests per second
_last = [0.0]


def _get(url, tries=4):
    for i in range(tries):
        gap = time.time() - _last[0]
        if gap < MIN_GAP_S:
            time.sleep(MIN_GAP_S - gap)
        _last[0] = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))
        except (urllib.error.URLError, TimeoutError):
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))
    raise RuntimeError("unreachable")


def companyfacts(cik, max_age_h=20.0, offline=False):
    """Return the parsed companyfacts JSON for a 10-digit CIK string.

    A cached copy younger than max_age_h is reused; offline=True never touches the
    network (returns None when nothing is cached)."""
    cik = str(cik).zfill(10)
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, "CIK%s.json.gz" % cik)
    if os.path.exists(path):
        age_h = (time.time() - os.path.getmtime(path)) / 3600.0
        if offline or age_h < max_age_h:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                return json.load(f)
    if offline:
        return None
    raw = _get("https://data.sec.gov/api/xbrl/companyfacts/CIK%s.json" % cik)
    with gzip.open(path, "wb") as f:
        f.write(raw)
    return json.loads(raw.decode("utf-8"))
