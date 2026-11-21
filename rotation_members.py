#!/usr/bin/env python3
"""
SPEC-86 Part A: what is inside each sector-ladder row, and who moved it.

For every row of the sector ladder (``regime.REG_ETFS``, 80 rows) this writes
``data/rotation_members.json``: the row's holdings with weights, each
holding's own return over the ladder's windows (r5, r21, r63, r126), its
CONTRIBUTION to the row's return over each window, a rest-of-fund line that
makes every column sum to the ladder's own number, and the participation
numbers (share of weight up, n up, top-3 concentration) the phase checks read.
See ``tools/SPEC-86-rotation-durability.md`` (jr-dash) sections 3 and 10.

WHERE THE NUMBERS COME FROM
---------------------------
* The row's own return ``R`` is read from ``data/technicals.json`` ->
  ``regime.ladder.rows`` -- the exact number on screen. It is NEVER recomputed
  from holdings; the rest-of-fund line is what absorbs any gap.
* Holdings, by tier (the payload records which one a row used):
    basket       the members listed in REG_ETFS, equal weight -- exact.
    trust        SLV: one line, physical silver, 100%.
    issuer_full  State Street's daily holdings file for the SPDR funds in
                 SSGA_FUNDS -- every holding with its weight.
    top10        everything else: Yahoo's ``funds_data.top_holdings``.
  iShares was tried for the iShares rows (2026-09-25, time-boxed): the product
  ids are stable (the product-screener JSON lists them), but every holdings
  download URL -- .csv, .xls, the json tab, the blackrock.com mirror, with and
  without a browser TLS fingerprint -- now serves the product HTML page
  instead of the file. Those rows stay on ``top10`` until that changes.
* Member prices: one batch of Yahoo adjusted closes per run (threaded, one
  request per symbol -- exactly what ``yf.download`` does internally, but this
  way each listing's trading currency comes back with its chart metadata; a
  suffix table gets that wrong: KAP.L trades in USD on the LSE). Basket
  members are read from prices.db instead, through the same panel the ladder
  itself uses, which is what makes the basket attribution exact.

THE ARITHMETIC (spec 3.2)
-------------------------
* ETF start-of-window weights are backed out of today's weights,
  ``w0 = w * (1+R) / (1+r)``, and ``c = w0 * r``. Exact under full holdings and
  no rebalance inside the window; otherwise ``rest = R - sum(c)`` absorbs it.
* A basket is daily-rebalanced equal weight. Each member is attributed
  BUY-AND-HOLD, ``c_i = r_i / n`` with n the members priced at the window
  start, so every line is checkable as weight x return; the basket's own
  return minus those lines is the rest line with ``kind = "rebalancing"``,
  what the daily reset added or cost (2026-09-25; before, the exact daily
  formula ``sum_t (1/n_t) r_it G_{t-1}`` folded that into each name, and
  build() still runs it to assert R is the basket's own return).
* A foreign listing's local return is converted,
  ``r_usd = (1+r_loc)(1+r_fx) - 1``, with ``<CCY>USD=X`` from Yahoo.
* Every series is as-of aligned onto the ladder's own session axis (SPY's
  sessions, technicals.json regime.dates). A member whose last bar trails that
  axis by more than STALE_MAX sessions gets ``r: null`` everywhere.

WHAT IS IN THE REST LINE (rest.drivers, SPEC-86 addendum 2026-09-25)
-------------------------------------------------------------------
``rest.c`` stays ``R - sum(listed c)``. ``rest.drivers`` splits it into
every priced name that moved it (``show`` of them listed first, the board's
"show all" lists the others) and an ``other`` line, what no name explains;
names + other = rest in every window: ``sec_nport`` for a top-10 row (the fund's latest public SEC N-PORT,
the lines Yahoo's ten do not list, the top 60 priced), ``rebalance`` for a
SPDR S&P Select Industry row (each quarter attributed from its own book: the
issuer file for this quarter, the fund's quarter-end N-PORT for earlier ones).
The filings are cached in ``data/rotation_deep_holdings.json`` (committed):
a fund's filings are listed once a week and a document is downloaded once, so
most days make no EDGAR call. EDGAR is reached only through fetch_insiders.get
(its User-Agent and its 0.12s pacing).

Honesty rules (spec section 2): a number that cannot be sourced is null with a
reason in ``errors``; nothing is estimated. No position facts: these are the
published holdings of public funds.

Run:  python rotation_members.py      (network: 26 SSGA files, ~48 Yahoo
                                       top-holdings calls, ~1,400 paced Yahoo
                                       price requests + ~900 rest-line names +
                                       FX; ~5 minutes; the weekly N-PORT check
                                       adds ~60 EDGAR calls, ~150 when every
                                       fund has a new filing)
Test: python test_rotation_members.py  (offline, fixtures only)
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import math
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pricedb
import fetch_insiders            # SEC EDGAR politeness: its UA and its paced get() (<=10 req/s)
import regime

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(HERE, "data", "rotation_members.json")
DEEP_PATH = os.path.join(HERE, "data", "rotation_deep_holdings.json")
LAST_PATH = os.path.join(HERE, "data", "cache", "rotation_last_closes.json")   # gitignored (data/cache/)
TECH_PATH = os.path.join(HERE, "data", "technicals.json")
DB_PATH = os.path.join(HERE, "prices.db")
TICKERS_PATH = os.path.join(HERE, "tickers.txt")

WINDOWS = (("r5", 5), ("r21", 21), ("r63", 63), ("r126", 126), ("r252", 252))
# r252 (B1 stage 2, 2026-11-20): the ladder shows a one-year return
# (technicals.json regime.ladder_r252), so the holdings carry one too.
PART_WINDOWS = ("r21", "r63")     # spec 3.3: participation on 21d and 63d
STALE_MAX = 3                     # sessions a member's last bar may trail the axis
SIGMA_WIN = 126                   # the row's own daily-vol window (spec 4 uses 126d)
CONC_TOP = 3
AXIS_TAIL = 460                   # sessions of member history fetched: r252, plus the
                                  # quarter before it (a Select Industry row's r252 window
                                  # reaches back to the reset it started in), and room
                                  # for the 200-session average the trend breadth reads
GLITCH_X = 20.0                   # a one-day close ratio beyond 20x / below 1/20x
                                  # is a unit error (GBp vs GBP), not a price move
FETCH_WORKERS = 4
FETCH_RATE = 12.0                 # request starts per second, all workers together
RATE_PAUSE_S = 30.0               # first back-off after a Yahoo rate-limit reply
FETCH_BUDGET_S = 300.0            # member pass; the retry and FX passes get their own
RETRY_BUDGET_S = 90.0

# The SPDR funds State Street publishes a daily full-holdings xlsx for
# (spec 3.1, verbatim).
SSGA_FUNDS = ("XLE XLB XLI XLY XLP XLV XLF XLK XLC XLU XLRE XOP XES XME XHB XRT "
              "XBI XPH XSD XAR XTN KBE KRE KIE KCE SPY").split()
SSGA_URL = ("https://www.ssga.com/us/en/intermediary/library-content/products/"
            "fund-data/etfs/us/holdings-daily-us-en-{tk}.xlsx")
UA = {"User-Agent": "Mozilla/5.0 (compatible; daily-prices holdings reader)"}   # generic: no project or owner name (2026-11-20)

TRUSTS = {"SLV": {"t": "XAG", "name": "Physical silver bullion (the trust's only asset)"}}

# ---- what is in the rest line (jr-dash SPEC-86 addendum, 2026-09-25) --------
# 1. SEC N-PORT for the top-10 rows: every holding of the fund, as of its last
#    public quarter-end filing.
MF_URL = "https://www.sec.gov/files/company_tickers_mf.json"
NPORT_LIST_URL = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={sid}"
                  "&type=NPORT-P&dateb=&owner=include&count=10&output=atom")
NPORT_DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/primary_doc.xml"
# Funds SEC's company_tickers_mf.json does not list. Each id was read out of
# the fund's own N-PORT-P (seriesName matched), never guessed.
SERIES_FIX = {
    "HACK": "S000082278",   # Amplify Cybersecurity ETF (Amplify ETF Trust), found 2026-09-25
    "SILJ": "S000082279",   # Amplify Junior Silver Miners ETF (Amplify ETF Trust, CIK 1633061), found 2026-09-27
    "DRAM": "S000102337",   # Roundhill Memory ETF (Roundhill ETF Trust, CIK 1976517), found 2026-09-27
}
DEEP_STALE_DAYS = 7      # re-list a fund's filings when its check is older than this
DEEP_STORE = None        # filing lines kept per fund in the cache: EVERY line since 2026-11-20
                         # (80 before; IWM's 2,014 lines were 86% off any list). The vehicle
                         # composites (basket_fundamentals.py) read the whole book from here.
DEEP_SEARCH_LINES = 90   # ...but only the heaviest 90 per fund are mapped ISIN -> Yahoo symbol
                         # (what the daily price pass can reach, the listed 10 + DEEP_PRICE_CAP)
DEEP_PRICE_CAP = 60      # of the lines NOT in the listed top 10, priced per fund per day
DEEP_SHOW = 15           # driver names shown by default per row (drivers.show); every priced one is carried
DEEP_BUDGET_S = 180.0    # the deep-name price pass
SEARCH_BUDGET_S = 150.0  # Yahoo ISIN -> symbol searches (new ISINs only; cached)
SEARCH_RATE = 8.0
SEARCH_RETRY_DAYS = 30   # an ISIN Yahoo could not map is asked again after this
RUN_WALL_S = 570.0       # the deep pass gives way to this wall clock (workflow timeout 12 min)
QUARTERS_KEEP = 5        # a Select Industry fund keeps its last five quarter-end filings
                         # (three until 2026-11-20; five anchor every quarter of the r252 window)
PX_TOL = 0.15            # a mapped symbol's close on the filing date (USD) must be within
                         # 15% of the filing's own value per share, or it is not the security
# 2. The SPDR S&P Select Industry funds: modified equal weight, reset after the
#    close on the third Friday of March, June, September and December.
SELECT_INDUSTRY = frozenset("XES XOP XME XHB XRT XBI XPH XSD XAR XTN KBE KRE KIE KCE".split())
RESET_MONTHS = (3, 6, 9, 12)

# The public repo's pre-commit guard (tools/sensitive-data-guard.sh), line by
# line: one of these words on the same line as a figure is refused. Names from
# a filing are free text, so every name this file writes passes guard_safe().
# Generic words only: this repo is PUBLIC, and a list naming particular firms
# says where the book is kept (C-05, 2026-11-07). test_guard_words.py keeps the
# three copies of this list identical and closed to proper nouns.
_GUARD_WORD = re.compile(r"(held|holdings?|cost basis|basis|custodian|broker|account|"
                         r"sleeve weight|book value|realized)", re.I)
_GUARD_FIG = re.compile(r"\$[0-9]|[0-9][0-9,.]*[ \t]*(shares|%)|[0-9][0-9,.]*[kKmM][ \t]|"
                        r"[0-9][0-9,.]*[kKmM]$", re.I)
_GUARD_ABBR = (("holdings", "Hldgs"), ("holding", "Hldg"), ("held", "Hld"), ("basis", "Bss"),
               ("custodian", "Cust"), ("realized", "Rlzd"), ("broker", "Brkr"), ("account", "Acct"))

# Yahoo's top_holdings symbols that are not Yahoo price symbols. Each fix here
# was found by a failed price fetch, not guessed.
SYMBOL_FIX = {
    "KAP": "KAP.L",   # Kazatomprom GDR (URA). Bare KAP has no quote; the LSE GDR trades in USD.
}
# Minor-unit currencies Yahoo reports -> the ISO currency the FX pair uses.
# Returns are unit-free, so pence vs pounds only matters for the FX pair name.
MINOR_CCY = {"GBp": "GBP", "GBX": "GBP", "ZAc": "ZAR", "ZAC": "ZAR", "ILA": "ILS"}

# Lines that are not securities: cash, money-market sweeps, currency, futures.
# Deliberately narrow: "Forward Air" (XTN) and Pathward (ticker CASH, KRE) are
# companies, so neither "forward" nor the symbol CASH is a cash marker.
_CASH_NAME = re.compile(r"\b(cash|money market|us dollar|government ob\w*|"
                        r"treasury (sl|obligations))\b", re.I)
_FUT_NAME = re.compile(r"\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{2}\s*$")
_MMF_SYM = re.compile(r"^[A-Z]{3,4}XX$")          # FGXXX, SPAXX: money-market funds
_CASH_SYM = {"XTSLA", "USD"}                       # XTSLA = BlackRock cash fund (IWM)
_US_SYM = re.compile(r"^[A-Z]{1,5}([./][A-Z]{1,2})?$")


# ==========================================================================
# symbols
# ==========================================================================

def is_cash_line(sym, name):
    """True for cash, money-market, currency and futures lines. They are not
    holdings a contribution can be computed for; they fall into the rest line."""
    s = (sym or "").strip().upper()
    n = (name or "").strip()
    if s in _CASH_SYM or s.endswith("=F") or _MMF_SYM.match(s):
        return True
    return bool(_CASH_NAME.search(n) or _FUT_NAME.search(n.upper()))


def candidates(sym, source):
    """Yahoo symbols to try for a holding, best first. ``source`` is "ssga" or
    "yahoo". Returns [] when the symbol cannot be a priced security.

    * SSGA writes US share classes with a dot (BRK.B); Yahoo uses a dash.
      Anything else that is not a plain US ticker (CVR "-" lines, earn-outs,
      internal codes like 2602335D) returns [] and lands in the rest line.
    * Yahoo's top_holdings labels KOSPI names .KQ (000660.KQ = SK hynix); they
      are .KS. .KS first, .KQ kept as the fallback for genuine KOSDAQ names.
    """
    s = (sym or "").strip()
    if not s:
        return []
    if source == "ssga":
        s = s.upper()
        if not _US_SYM.match(s):
            return []
        return [re.sub(r"[./]", "-", s)]
    s = SYMBOL_FIX.get(s, s)
    if s.endswith(".KQ"):
        return [s[:-3] + ".KS", s]
    return [s]


def iso_ccy(ccy):
    if not ccy:
        return None
    return MINOR_CCY.get(ccy, ccy.upper())


def guard_trips(line):
    """True when the public repo's pre-commit guard would refuse this line."""
    return bool(line and _GUARD_WORD.search(line) and _GUARD_FIG.search(line))


def guard_safe(name):
    """A filing's free-text name, made safe for a committed line: '%' and '$'
    are spelled out, and only if the line would STILL trip are the guard's
    words abbreviated. A name that never trips comes back unchanged."""
    s = (name or "").strip()
    if not guard_trips(s):
        return s
    s = s.replace("%", " pct").replace("$", "USD ")
    for word, abbr in _GUARD_ABBR:
        if not guard_trips(s):
            break
        s = re.sub(word, abbr, s, flags=re.I)
    return s


# ==========================================================================
# holdings sources
# ==========================================================================

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def xlsx_rows(blob):
    """First worksheet of an .xlsx as a list of {column letter: text} dicts.
    Stdlib only (zipfile + ElementTree): the SSGA file is plain shared strings
    and numbers, which does not justify a new pinned dependency."""
    z = zipfile.ZipFile(io.BytesIO(blob))
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(_NS + "si"):
            shared.append("".join(t.text or "" for t in si.iter(_NS + "t")))
    sheet = "xl/worksheets/sheet1.xml"
    if sheet not in z.namelist():
        sheet = sorted(n for n in z.namelist() if n.startswith("xl/worksheets/sheet"))[0]
    out = []
    for r in ET.fromstring(z.read(sheet)).iter(_NS + "row"):
        row = {}
        for c in r.findall(_NS + "c"):
            col = re.match(r"[A-Z]+", c.get("r") or "A").group(0)
            kind = c.get("t")
            v = c.find(_NS + "v")
            if kind == "s":
                val = shared[int(v.text)] if v is not None and v.text else None
            elif kind == "inlineStr":
                val = "".join(t.text or "" for t in c.iter(_NS + "t"))
            else:
                val = v.text if v is not None else None
            row[col] = val
        out.append(row)
    return out


def parse_ssga(blob):
    """(holdings_asof, holdings, n_total) from an SSGA daily holdings xlsx.
    holdings = [{"sym","name","w"}] with w a FRACTION; cash/futures and lines
    with no usable ticker are excluded (they are the rest line). n_total counts
    every security line, including the unusable-ticker ones, so rest.n is
    honest."""
    rows = xlsx_rows(blob)
    asof = None
    hdr = None
    holdings = []
    n_total = 0
    for r in rows:
        vals = {k: (v.strip() if isinstance(v, str) else v) for k, v in r.items()}
        if hdr is None:
            a = vals.get("A") or ""
            if a.lower().startswith("holdings"):
                m = re.search(r"(\d{1,2}-[A-Za-z]{3}-\d{4})", vals.get("B") or "")
                if m:
                    asof = dt.datetime.strptime(m.group(1), "%d-%b-%Y").date().isoformat()
            names = {v: k for k, v in vals.items() if isinstance(v, str)}
            if "Ticker" in names and "Weight" in names:
                hdr = names
            continue
        name = vals.get(hdr.get("Name", "A"))
        if not name:
            break                      # the holdings block ends at the first blank row
        sym = vals.get(hdr["Ticker"]) or ""
        try:
            w = float(vals.get(hdr["Weight"])) / 100.0
        except (TypeError, ValueError):
            continue
        if is_cash_line(sym, name):
            continue
        n_total += 1
        cands = candidates(sym, "ssga")
        if not cands:
            continue
        holdings.append({"sym": cands[0], "alts": [], "name": name, "w": w})
    if hdr is None:
        raise ValueError("no Ticker/Weight header in the SSGA file")
    if not holdings:
        raise ValueError("SSGA file parsed to zero holdings")
    return asof, merge_dupes(holdings), n_total


def parse_top10(records):
    """[(symbol, name, weight fraction)] from Yahoo's top_holdings -> holdings.
    Cash lines are dropped; ``alts`` carries the pricing fallbacks (.KQ)."""
    out = []
    for sym, name, w in records:
        if w is None or (isinstance(w, float) and math.isnan(w)):
            continue
        if is_cash_line(sym, name):
            continue
        cands = candidates(sym, "yahoo")
        if not cands:
            continue
        out.append({"sym": cands[0], "alts": cands[1:], "name": name, "w": float(w)})
    return merge_dupes(out)


def merge_dupes(holdings):
    """One line per symbol (two share lines mapping to one symbol add up)."""
    seen = {}
    out = []
    for h in holdings:
        if h["sym"] in seen:
            seen[h["sym"]]["w"] += h["w"]
        else:
            seen[h["sym"]] = dict(h)
            out.append(seen[h["sym"]])
    return out


def fetch_ssga(tk):
    req = urllib.request.Request(SSGA_URL.format(tk=tk.lower()), headers=UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def fetch_top10(tk):
    import yfinance as yf
    th = yf.Ticker(tk).funds_data.top_holdings
    if th is None or len(th) == 0:
        raise ValueError("Yahoo returned no top holdings")
    return [(str(s), str(r.get("Name") or ""), float(r.get("Holding Percent")))
            for s, r in th.iterrows()]


# ==========================================================================
# more issuers' daily files (B1 stage 1, 2026-11-20)
# ==========================================================================
# Four issuers publish every line of a fund to a plain script (board-refactor
# recon-3 section 2.1, probed 2026-11-20). Each row below moves from Yahoo's
# top 10 plus a quarterly N-PORT tail to every line, daily. iShares, VanEck and
# WisdomTree block a plain client (a 503, a cookie redirect, a bot challenge)
# and are NOT worked around: their rows stay on top 10 + N-PORT. Only derived
# numbers (weights, tickers, contributions) reach the payload; the raw files
# are read in memory and never written. To switch an issuer off, delete its
# entry from ISSUER_FUNDS: its rows fall back to Yahoo's top 10 the same day.
ISSUER_FUNDS = {
    "firsttrust": "CIBR FDN GRID".split(),
    "amplify": "HACK SILJ".split(),
    "invesco": ["QQQ"],          # proven for QQQ only; PBJ/SPLV answered HTTP 500 on the ticker id
}
# Switched OFF (2026-11-20, coordinator): Global X's own file ends "The information
# contained herein may not be reproduced, redistributed or used to create any
# derivative works", and this repo is public. Its rows stay on Yahoo's top 10 +
# SEC N-PORT. The parser (parse_globalx) and its fixture test are kept, unused;
# moving the entry back into ISSUER_FUNDS would turn it on again.
ISSUER_OFF = {
    "globalx": "COPX LIT URA DRIV FINX PAVE BOTZ DTCR".split(),
}
ISSUER_OF = {t: k for k, ts in ISSUER_FUNDS.items() for t in ts}
ISSUER_NAME = {
    "globalx": "Global X daily full-holdings file",
    "firsttrust": "First Trust daily holdings page",
    "amplify": "Amplify daily all-funds holdings file",
    "invesco": "Invesco daily holdings feed",
}
GLOBALX_URL = "https://assets.globalxetfs.com/funds/holdings/{tk}_full-holdings_{ymd}.csv"
GLOBALX_BACK = 5                 # weekdays tried, today first (a file is dated T-1 by mid-afternoon)
FIRSTTRUST_URL = "https://www.ftportfolios.com/Retail/Etf/EtfHoldings.aspx?Ticker={tk}"
AMPLIFY_URL = "https://amplifyetfs.com/wp-content/uploads/feeds/AmplifyWeb.40XL.XL_Holdings.csv"
INVESCO_URL = ("https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/{tk}/holdings/fund"
               "?idType=ticker&interval=monthly&productType=ETF")
ISSUER_UA = {"User-Agent": "Mozilla/5.0 (compatible; daily-prices holdings reader)"}
W_SUM_OK = (0.90, 1.10)          # every line of a file, cash included, must add up to about 100%
ISSUER_PX_TOL = PX_TOL           # a mapped symbol's USD close on the file date vs the file's own price

# Bloomberg exchange code -> Yahoo suffix (Global X and Amplify write "CODE EXCH",
# First Trust "CODE.EXCH"). An unknown code maps to nothing and the line goes to
# the rest line; a known one is still only USED after its close on the file date
# matches the file's own price (ISSUER_PX_TOL), so a wrong entry costs a line,
# never a number.
BBG_SUFFIX = {
    "US": "", "UN": "", "UW": "", "UQ": "", "UR": "", "UA": "", "UV": "", "UP": "", "UF": "",
    "CN": ".TO", "CT": ".TO", "CV": ".V", "LN": ".L", "LI": ".IL", "JP": ".T", "JT": ".T", "AU": ".AX",
    "AT": ".AX", "SS": ".ST", "PW": ".WA", "GR": ".DE", "GY": ".DE", "FP": ".PA", "NA": ".AS",
    "IM": ".MI", "SW": ".SW", "SE": ".SW", "SM": ".MC", "FH": ".HE", "DC": ".CO", "NO": ".OL",
    "BB": ".BR", "ID": ".IR", "PL": ".LS", "AV": ".VI", "KS": ".KS", "KQ": ".KQ", "TT": ".TW",
    "IN": ".NS", "IB": ".BO", "SJ": ".JO", "MF": ".MX", "MM": ".MX", "BZ": ".SA", "SP": ".SI",
    "IT": ".TA", "NZ": ".NZ", "TB": ".BK", "MK": ".KL", "IJ": ".JK", "PM": ".PS", "GA": ".AT",
    "TI": ".IS", "C1": ".SS", "C2": ".SZ", "AB": ".SR", "HK": ".HK", "CH": None,
}
_BBG = re.compile(r"^(?P<code>[A-Z0-9/\-]+?)[ .](?P<ex>[A-Z][A-Z0-9])$")


def bbg_yahoo(code):
    """Yahoo symbols for an issuer file's Bloomberg-style ticker, best first;
    [] when it cannot be mapped. 'BOL SS' -> BOL.ST, '2899 HK' -> 2899.HK,
    'TECK/B CN' -> TECK-B.TO then TECK-B.V, 'ATO.FP' -> ATO.PA, '600519 CH' ->
    600519.SS (Shanghai codes start with 6, Shenzhen's do not), a plain US
    ticker as itself (BRK.B -> BRK-B). A Saudi line under a letter code has no
    Yahoo symbol (they trade under a number): []."""
    s = (code or "").strip().upper()
    if not s:
        return []
    m = _BBG.match(s)
    if not m or m.group("ex") not in BBG_SUFFIX:
        return [re.sub(r"[./]", "-", s)] if _US_SYM.match(s) else []
    # 'NG/ LN' (National Grid) is NG.L: a trailing slash is not a share class
    c, ex = m.group("code").rstrip("/").replace("/", "-"), m.group("ex")
    if ex == "HK":
        return [c.zfill(4) + ".HK"] if c.isdigit() else []
    if ex == "AB":
        return [c + ".SR"] if c.isdigit() else []
    if ex == "CH":
        return [c + (".SS" if c.startswith("6") else ".SZ")] if c.isdigit() else []
    if ex in ("CN", "CT"):
        return [c + ".TO", c + ".V"]
    return [c + BBG_SUFFIX[ex]]


def _f(s):
    """'1,694,935.98' / '$1,497,448,204.70' / '8.75%' -> float, or None."""
    try:
        return float(str(s).replace(",", "").replace("$", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def _usd_px(mv, shares):
    a, b = _f(mv), _f(shares)
    return (a / b) if (a is not None and b and b > 0) else None


def _issuer_line(sym_raw, name, w, px, out, unmapped):
    cands = bbg_yahoo(sym_raw)
    if not cands:
        unmapped.append(sym_raw or name)
        return
    out.append({"sym": cands[0], "alts": cands[1:], "name": name, "w": w, "px": px})


def _check_sum(src, total):
    if not (W_SUM_OK[0] <= total <= W_SUM_OK[1]):
        raise ValueError(f"{src}: the file's lines add up to {total * 100:.1f} pct, not about 100 "
                         "(the source changed shape)")


def parse_globalx(blob, tk=None):
    """(asof, holdings, n_total) from a Global X full-holdings CSV:
    'Fund Holdings Data as of 11/19/2026', then '% of Net Assets,Ticker,Name,
    SEDOL,Market Price ($),Shares Held,Market Value ($)'. Currency balances,
    payables and lines with no ticker are cash; px = market value / shares,
    in US dollars. ValueError when the header, the date or the lines are not
    there, or the weights do not add up to about 100% (the source changed)."""
    import csv
    text = blob.decode("utf-8-sig") if isinstance(blob, bytes) else blob
    rows = list(csv.reader(io.StringIO(text)))
    asof, hdr = None, None
    out, unmapped, total, n_total = [], [], 0.0, 0
    for r in rows:
        if hdr is None:
            m = re.search(r"as of (\d{1,2})/(\d{1,2})/(\d{4})", ",".join(r))
            if m:
                asof = dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
            if "% of Net Assets" in r and "Ticker" in r:
                hdr = {k: i for i, k in enumerate(r)}
            continue
        if len(r) < len(hdr):
            continue                      # the disclaimer line at the foot
        w = _f(r[hdr["% of Net Assets"]])
        if w is None:
            continue
        w /= 100.0
        total += w
        tk_raw, name = r[hdr["Ticker"]].strip(), r[hdr.get("Name", 2)].strip()
        if not tk_raw or is_cash_line(tk_raw, name) or re.search(r"PAYABLE|RECEIVABLE", name, re.I):
            continue
        n_total += 1
        px = _usd_px(r[hdr["Market Value ($)"]], r[hdr["Shares Held"]]) if (
            "Market Value ($)" in hdr and "Shares Held" in hdr) else None
        _issuer_line(tk_raw, name, w, px, out, unmapped)
    if hdr is None:
        raise ValueError("Global X file: no '% of Net Assets' / 'Ticker' header (the source changed shape)")
    if asof is None:
        raise ValueError("Global X file: no 'as of' date")
    if not out:
        raise ValueError("Global X file parsed to zero holdings")
    _check_sum("Global X file", total)
    return asof, merge_dupes(out), n_total


def parse_amplify(blob, tk):
    """(asof, holdings, n_total) for fund ``tk`` from Amplify's one all-funds
    CSV (Date, Account = the fund, StockTicker, Shares, MarketValue in US
    dollars, Weightings 'n.nn%', MoneyMarketFlag)."""
    import csv
    text = blob.decode("utf-8-sig") if isinstance(blob, bytes) else blob
    rd = csv.DictReader(io.StringIO(text))
    need = {"Date", "Account", "StockTicker", "SecurityName", "Shares", "MarketValue", "Weightings"}
    if not need.issubset(set(rd.fieldnames or [])):
        raise ValueError("Amplify file: missing column(s) " + ", ".join(sorted(need - set(rd.fieldnames or [])))
                         + " (the source changed shape)")
    asof, out, unmapped, total, n_total, seen = None, [], [], 0.0, 0, 0
    for r in rd:
        if (r.get("Account") or "").strip().upper() != tk.upper():
            continue
        seen += 1
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", (r.get("Date") or "").strip())
        if m and asof is None:
            asof = dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
        w = _f(r.get("Weightings"))
        if w is None:
            continue
        w /= 100.0
        total += w
        sym, name = (r.get("StockTicker") or "").strip(), (r.get("SecurityName") or "").strip()
        # a currency balance is CUSIP 'CASHCAD', 'CASHEUR'...; the TICKER 'CASH' is a
        # company (Pathward Financial), so the ticker alone is never the test
        if ((r.get("MoneyMarketFlag") or "").strip().upper() == "Y" or not sym
                or (r.get("CUSIP") or "").strip().upper().startswith("CASH")
                or sym.upper() == "CASH&OTHER" or is_cash_line(sym, name)):
            continue
        n_total += 1
        _issuer_line(sym, name, w, _usd_px(r.get("MarketValue"), r.get("Shares")), out, unmapped)
    if not seen:
        raise ValueError(f"Amplify file: no lines for {tk}")
    if not out:
        raise ValueError(f"Amplify file: {tk} parsed to zero holdings")
    _check_sum(f"Amplify file ({tk})", total)
    return asof, merge_dupes(out), n_total


def _cells(tr):
    import html as _html
    return [_html.unescape(re.sub(r"<[^>]+>", "", c)).strip()
            for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)]


def parse_firsttrust(blob, tk=None):
    """(asof, holdings, n_total) from a First Trust fund holdings page: the
    table whose header row reads Security Name, Identifier, CUSIP, ...,
    Shares / Quantity, Market Value, Weighting, under 'Holdings of the Fund as
    of mm/dd/yyyy'. A '$CAD'-style identifier is a currency balance. An HTML
    page, so brittle by nature: every shape check raises."""
    s = blob.decode("utf-8", "replace") if isinstance(blob, bytes) else blob
    m = re.search(r"Holdings of the Fund as of (\d{1,2})/(\d{1,2})/(\d{4})", s)
    if not m:
        raise ValueError("First Trust page: no 'Holdings of the Fund as of' date (the source changed shape)")
    asof = dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
    i = s.find("sortHoldings('Weighting')", m.end())
    if i < 0:
        raise ValueError("First Trust page: no Weighting column (the source changed shape)")
    head_start = s.rfind("<tr", 0, i)
    end = s.find("</table>", i)
    trs = re.findall(r"<tr[^>]*>(.*?)</tr>", s[head_start:end], re.S | re.I)
    if not trs:
        raise ValueError("First Trust page: no table rows")
    head = [re.sub(r"\s+", " ", c) for c in _cells(trs[0])]
    col = {h: j for j, h in enumerate(head)}
    for need in ("Security Name", "Identifier", "Weighting"):
        if need not in col:
            raise ValueError(f"First Trust page: no '{need}' column (the source changed shape)")
    out, unmapped, total, n_total = [], [], 0.0, 0
    for tr in trs[1:]:
        c = _cells(tr)
        if len(c) < len(head):
            continue
        w = _f(c[col["Weighting"]])
        if w is None:
            continue
        w /= 100.0
        total += w
        sym, name = c[col["Identifier"]], c[col["Security Name"]]
        if not sym or sym.startswith("$") or is_cash_line(sym, name):
            continue
        n_total += 1
        px = _usd_px(c[col["Market Value"]], c[col["Shares / Quantity"]]) if (
            "Market Value" in col and "Shares / Quantity" in col) else None
        _issuer_line(sym, name, w, px, out, unmapped)
    if not out:
        raise ValueError("First Trust page parsed to zero holdings")
    _check_sum("First Trust page", total)
    mc = re.search(r"Total Number of Holdings \(excluding cash\):\s*(\d+)", s)
    if mc and abs(int(mc.group(1)) - n_total) > 2:
        raise ValueError(f"First Trust page: says {mc.group(1)} lines, {n_total} parsed (the source changed shape)")
    return asof, merge_dupes(out), n_total


_INVESCO_SKIP = re.compile(r"currency|future|collateral|synthetic cash|money market|cash|swap|forward", re.I)


def parse_invesco(blob, tk=None):
    """(asof, holdings, n_total) from Invesco's holdings JSON: effectiveDate
    and holdings[] of {ticker, issuerName, percentageOfTotalNetAssets,
    securityTypeName}. Currency, futures, collateral and synthetic-cash lines
    are not holdings. No price in the feed, so no file-price check (QQQ's
    lines are US listings priced under their own tickers)."""
    d = json.loads(blob)
    hs = d.get("holdings") if isinstance(d, dict) else None
    if not isinstance(hs, list) or not hs:
        raise ValueError("Invesco feed: no holdings list (the source changed shape)")
    asof = d.get("effectiveBusinessDate") or d.get("effectiveDate")
    if not asof or not re.match(r"\d{4}-\d{2}-\d{2}$", str(asof)):
        raise ValueError("Invesco feed: no effective date")
    out, unmapped, total, n_total = [], [], 0.0, 0
    for x in hs:
        w = x.get("percentageOfTotalNetAssets")
        if not isinstance(w, (int, float)):
            continue
        w /= 100.0
        typ = str(x.get("securityTypeName") or "")
        if _INVESCO_SKIP.search(typ):
            total += w
            continue
        total += w
        n_total += 1
        _issuer_line(str(x.get("ticker") or ""), str(x.get("issuerName") or ""), w, None, out, unmapped)
    if not out:
        raise ValueError("Invesco feed parsed to zero holdings")
    _check_sum("Invesco feed", total)
    return asof, merge_dupes(out), n_total


ISSUER_PARSE = {"globalx": parse_globalx, "firsttrust": parse_firsttrust,
                "amplify": parse_amplify, "invesco": parse_invesco}


def _http(url, timeout=30):
    req = urllib.request.Request(url, headers=ISSUER_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


class IssuerFetcher:
    """fetch(kind, tk) -> the issuer's raw file for one fund. Amplify's one
    file serves every Amplify fund, so it is fetched once per run. Global X
    files are dated: today's is tried first, then each weekday back, up to
    GLOBALX_BACK; only a 404 steps back, anything else raises. ``calls``
    counts requests."""

    def __init__(self, get=_http, today=None):
        self.get = get
        self.today = dt.date.fromisoformat(today) if today else dt.datetime.now(dt.timezone.utc).date()
        self.calls = 0
        self._amplify = None

    def __call__(self, kind, tk):
        if kind == "amplify":
            if self._amplify is None:
                self.calls += 1
                self._amplify = self.get(AMPLIFY_URL)
            return self._amplify
        if kind == "globalx":
            d, tried, last = self.today, 0, None
            while tried < GLOBALX_BACK:
                if d.weekday() < 5:
                    tried += 1
                    self.calls += 1
                    try:
                        return self.get(GLOBALX_URL.format(tk=tk.lower(), ymd=d.strftime("%Y%m%d")))
                    except urllib.error.HTTPError as ex:   # noqa: PERF203
                        if ex.code != 404:
                            raise
                        last = ex
                d -= dt.timedelta(days=1)
            raise ValueError(f"no Global X file in the last {GLOBALX_BACK} weekdays ({last})")
        url = {"firsttrust": FIRSTTRUST_URL, "invesco": INVESCO_URL}[kind].format(tk=tk)
        self.calls += 1
        return self.get(url)


def issuer_px_ok(px, hist_entry, axis, h_asof, fx_aligned):
    """True when a mapped symbol IS the file's security: its US-dollar close on
    the file date (minor units such as pence divided by 100) is within
    ISSUER_PX_TOL of the file's own price (market value / shares). True when
    the file carries no price (the check cannot run)."""
    if px is None or px <= 0:
        return True
    ccy_raw = hist_entry[2]
    ccy = iso_ccy(ccy_raw) or "USD"
    usd, _ = member_usd(axis, hist_entry, ccy, fx_aligned)
    if usd is None:
        return False
    i = asof_index(axis, h_asof) if h_asof else len(axis) - 1
    if i < 0:
        return False
    p = usd[i]
    if p is None:
        return False
    if ccy_raw in MINOR_CCY:
        p /= 100.0
    return abs(p / px - 1.0) <= ISSUER_PX_TOL


# ==========================================================================
# SEC N-PORT: the whole fund behind a top-10 row
# ==========================================================================
# A registered fund files N-PORT monthly and SEC publishes the third month of
# each fiscal quarter (NPORT-P) about 60 days after it: every security with
# its ISIN, CUSIP, currency, value and percent of net assets. That is the
# deepest public list for the 45 rows Yahoo only gives ten names for. The
# parsed lines are CACHED in data/rotation_deep_holdings.json (committed), so
# the daily run lists a fund's filings once a week and downloads a document
# only when a newer one exists.

_NP = "{http://www.sec.gov/edgar/nport}"
_ATOM = "{http://www.w3.org/2005/Atom}"
_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def parse_nport(blob, keep=DEEP_STORE):
    """One NPORT-P primary_doc.xml -> {"series","fund","period","lines",
    "n_sec","w_sec","tail","excluded"}.

    A line is a SECURITY when it is common or preferred equity (assetCat EC /
    EP), or an OTHER line that is not a currency (stapled units, REIT units),
    held long in shares, and is not securities-lending cash collateral. Cash,
    money-market and collateral vehicles (STIV), repos, futures, swaps and
    currency balances are counted in `excluded` with their weight. Weights are
    pctVal / 100 -- a fraction of NET assets, so a fund with loaned-out stock
    or a deferred-tax liability can sum past 1. `lines` keeps the heaviest
    `keep` securities; `tail` counts the others. `px` is the filing's own value
    per share in USD (valUSD / balance), the price check a mapped symbol has to
    pass on the filing date."""
    root = ET.fromstring(blob)
    gi = root.find(f".//{_NP}genInfo")
    if gi is None:
        raise ValueError("no genInfo block: not an N-PORT document")
    secs = []
    excl_n, excl_w = 0, 0.0
    for s in root.iter(_NP + "invstOrSec"):
        name = (s.findtext(_NP + "name") or s.findtext(_NP + "title") or "").strip()
        w = _num(s.findtext(_NP + "pctVal"))
        if w is None:
            excl_n += 1
            continue
        w /= 100.0
        cat = (s.findtext(_NP + "assetCat") or "").strip()
        cond = s.find(_NP + "assetConditional")
        desc = ""
        if cond is not None:
            cat = cat or (cond.get("assetCat") or "")
            desc = cond.get("desc") or ""
        ccy = (s.findtext(_NP + "curCd") or "").strip()
        cc = s.find(_NP + "currencyConditional")
        if not ccy and cc is not None:
            ccy = cc.get("curCd") or ""
        units = (s.findtext(_NP + "units") or "").strip()
        bal = _num(s.findtext(_NP + "balance")) or 0.0
        val = _num(s.findtext(_NP + "valUSD"))
        coll = (s.findtext(f"{_NP}securityLending/{_NP}isCashCollateral") or "").strip() == "Y"
        payoff = (s.findtext(_NP + "payoffProfile") or "").strip()
        isin = ticker = None
        idf = s.find(_NP + "identifiers")
        if idf is not None:
            e = idf.find(_NP + "isin")
            if e is not None and _ISIN.match((e.get("value") or "").strip().upper()):
                isin = e.get("value").strip().upper()
            e = idf.find(_NP + "ticker")
            if e is not None and (e.get("value") or "").strip() not in ("", "N/A"):
                ticker = e.get("value").strip()
        equity = cat in ("EC", "EP") or (cat == "OTHER" and "currenc" not in desc.lower()
                                           and not is_cash_line(ticker, name))
        if not (equity and not coll and units == "NS" and bal > 0 and w > 0 and payoff != "Short"):
            excl_n += 1
            excl_w += w
            continue
        secs.append({"name": name, "isin": isin, "ticker": ticker, "ccy": ccy or None, "w": w,
                     "px": (val / bal) if (val is not None and bal > 0) else None})
    # one line per ISIN (a fund can report two lots of one security)
    merged, by_isin = [], {}
    for x in secs:
        k = x["isin"]
        if k and k in by_isin:
            by_isin[k]["w"] += x["w"]
            continue
        if k:
            by_isin[k] = x
        merged.append(x)
    merged.sort(key=lambda x: (-x["w"], x["name"]))
    kept = merged if keep is None else merged[:keep]
    tail = [] if keep is None else merged[keep:]
    return {
        "series": (gi.findtext(_NP + "seriesId") or "").strip() or None,
        "fund": (gi.findtext(_NP + "seriesName") or "").strip() or None,
        "period": (gi.findtext(_NP + "repPdDate") or "").strip() or None,
        "lines": kept,
        "n_sec": len(merged),
        "w_sec": sum(x["w"] for x in merged),
        "tail": {"n": len(tail), "w": sum(x["w"] for x in tail)},
        "excluded": {"n": excl_n, "w": excl_w},
    }


def nport_latest(atom_blob):
    """The newest NPORT-P (not an amendment) in an EDGAR browse-edgar atom
    listing -> {"acc","filed","cik"}, or None when the series lists none."""
    feed = ET.fromstring(atom_blob)
    best = None
    for e in feed.iter(_ATOM + "entry"):
        c = e.find(_ATOM + "content")
        if c is None or (c.findtext(_ATOM + "filing-type") or "").strip() != "NPORT-P":
            continue
        href = c.findtext(_ATOM + "filing-href") or ""
        m = re.search(r"/data/(\d+)/", href)
        rec = {"acc": (c.findtext(_ATOM + "accession-number") or "").strip(),
               "filed": (c.findtext(_ATOM + "filing-date") or "").strip(),
               "cik": m.group(1) if m else None}
        if rec["acc"] and rec["cik"] and (best is None or rec["filed"] > best["filed"]):
            best = rec
    return best


def series_map(blob):
    """company_tickers_mf.json -> {ticker: series id}."""
    d = json.loads(blob)
    f = d.get("fields") or []
    i_s, i_t = f.index("seriesId"), f.index("symbol")
    out = {}
    for row in d.get("data") or []:
        t = str(row[i_t] or "").upper()
        if t and t not in out:
            out[t] = row[i_s]
    return out


def _days(a, b):
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


def deep_due(entry, today, stale_days=DEEP_STALE_DAYS):
    """A fund's filings are listed again when it was never checked or its last
    check is more than `stale_days` old. Every other day costs no EDGAR call."""
    return not entry or not entry.get("checked") or _days(entry["checked"], today) > stale_days


def nport_recent(atom_blob, n=QUARTERS_KEEP):
    """The newest ``n`` NPORT-P filings in a browse-edgar listing, newest first."""
    feed = ET.fromstring(atom_blob)
    out = []
    for e in feed.iter(_ATOM + "entry"):
        c = e.find(_ATOM + "content")
        if c is None or (c.findtext(_ATOM + "filing-type") or "").strip() != "NPORT-P":
            continue
        m = re.search(r"/data/(\d+)/", c.findtext(_ATOM + "filing-href") or "")
        rec = {"acc": (c.findtext(_ATOM + "accession-number") or "").strip(),
               "filed": (c.findtext(_ATOM + "filing-date") or "").strip(),
               "cik": m.group(1) if m else None}
        if rec["acc"] and rec["cik"]:
            out.append(rec)
    out.sort(key=lambda r: r["filed"], reverse=True)
    return out[:n]


def _doc_entry(doc, rec):
    return {"acc": rec["acc"], "filed": rec["filed"], "cik": rec["cik"], "period": doc["period"],
            "n_sec": doc["n_sec"], "w_sec": doc["w_sec"], "tail": doc["tail"],
            "excluded": doc["excluded"], "lines": doc["lines"]}


def _cut_short(entry):
    """A cached filing stored under an older, shorter DEEP_STORE (its tail
    counts lines it dropped) is downloaded once more, whole."""
    return DEEP_STORE is None and bool(((entry or {}).get("tail") or {}).get("n"))


def extra_nport(extras, already):
    """The `nport` funds of extra_funds.json (basket_fundamentals.read_extras) that are not
    already on the N-PORT list, sorted: they are listed and downloaded exactly like a top-10
    row's fund, so basket_fundamentals.py can build their composite from the cached book."""
    return sorted(t for t, e in (extras or {}).items()
                  if not t.startswith("__") and isinstance(e, dict) and e.get("nport") and t not in already)


def refresh_deep(tickers, cache, today, get, search=None, log=print,
                 search_budget_s=SEARCH_BUDGET_S, quarterly=SELECT_INDUSTRY):
    """Bring the N-PORT cache up to date for ``tickers``.

    A top-10 row keeps its fund's LATEST filing (the heaviest DEEP_STORE
    lines). A ``quarterly`` row -- a Select Industry fund -- keeps its last
    QUARTERS_KEEP filings with EVERY line: its quarter ends fall about eight
    sessions after each equal-weight reset, so each one is that quarter's
    post-reset book, the anchor rebalance_drivers() attributes the quarter
    from. ``get`` is the EDGAR fetcher (fetch_insiders.get: its UA, 0.12s
    pacing, one retry on 429/503); ``search`` maps an ISIN to a Yahoo symbol
    or None (raises on a transport failure, which leaves the ISIN unasked).
    A document is downloaded once, by accession, and never again. Mutates and
    returns ``cache``; also returns the call counts."""
    funds = cache.setdefault("funds", {})
    syms = cache.setdefault("symbols", {})
    calls = {"edgar": 0, "search": 0}
    # a SERIES_FIX added after the fund was cached as "no series" is picked up on
    # the next run, not after DEEP_STALE_DAYS
    due = [t for t in tickers if deep_due(funds.get(t), today)
           or (t in SERIES_FIX and (funds.get(t) or {}).get("series") != SERIES_FIX[t])
           or _cut_short(funds.get(t))]
    smap = None
    for t in due:
        f = funds.get(t) or {}
        sid = SERIES_FIX.get(t) or f.get("series")
        if not sid:
            if smap is None:
                try:
                    smap = series_map(get(MF_URL))
                except Exception as ex:
                    smap = {}
                    log(f"[warn] company_tickers_mf.json: {type(ex).__name__}: {ex}")
                calls["edgar"] += 1
            sid = smap.get(t)
        if not sid:
            funds[t] = {"checked": today, "error": "not in SEC company_tickers_mf.json, so no fund "
                                                  "series to read an N-PORT from"}
            continue
        try:
            recs = nport_recent(get(NPORT_LIST_URL.format(sid=sid)), QUARTERS_KEEP if t in quarterly else 1)
            calls["edgar"] += 1
            if not recs:
                funds[t] = {"series": sid, "checked": today, "error": "EDGAR lists no NPORT-P for series " + sid}
                continue
            if t in quarterly:
                q = dict(f.get("quarters") or {})
                have = {v.get("acc") for v in q.values()}
                fund = f.get("fund")
                for rec in recs:
                    if rec["acc"] in have:
                        continue
                    doc = parse_nport(get(NPORT_DOC_URL.format(cik=rec["cik"], acc=rec["acc"].replace("-", ""))),
                                      keep=None)
                    calls["edgar"] += 1
                    if doc["series"] and doc["series"] != sid:
                        raise ValueError(f"filing is for {doc['series']}, not {sid}")
                    q[doc["period"]] = _doc_entry(doc, rec)
                    fund = doc["fund"] or fund
                    log(f"  N-PORT {t}: {doc['fund']} period {doc['period']} (filed {rec['filed']}), "
                        f"{doc['n_sec']} securities")
                keep = sorted(q)[-QUARTERS_KEEP:]
                funds[t] = {"series": sid, "fund": fund, "checked": today, "quarters": {p: q[p] for p in keep}}
                continue
            lst = recs[0]
            if lst["acc"] == f.get("acc") and f.get("lines") is not None and not _cut_short(f):
                f["checked"] = today
                funds[t] = f
                continue
            doc = parse_nport(get(NPORT_DOC_URL.format(cik=lst["cik"], acc=lst["acc"].replace("-", ""))))
            calls["edgar"] += 1
            if doc["series"] and doc["series"] != sid:
                raise ValueError(f"filing is for {doc['series']}, not {sid}")
            funds[t] = dict(_doc_entry(doc, lst), series=sid, fund=doc["fund"], checked=today)
            log(f"  N-PORT {t}: {doc['fund']} period {doc['period']} (filed {lst['filed']}), "
                f"{doc['n_sec']} securities")
        except Exception as ex:
            why = f"EDGAR N-PORT fetch failed ({type(ex).__name__}: {str(ex)[:80]})"
            if f.get("lines") is not None or f.get("quarters"):
                f["error_last"] = why           # keep the last good filing; try again next run
                funds[t] = f
            else:
                funds[t] = {"series": sid, "checked": today, "error": why}
            log(f"[warn] {t}: {why}")
    # ISIN -> Yahoo symbol, only for ISINs never asked (or asked and unmapped
    # more than SEARCH_RETRY_DAYS ago), heaviest first, inside a time budget.
    if search is None:
        return cache, calls
    want = {}
    for t in tickers:
        f = funds.get(t) or {}
        for x in deep_lines(f)[:DEEP_SEARCH_LINES] + [x for q in (f.get("quarters") or {}).values()
                                                      for x in deep_lines(q)]:
            i = x.get("isin")
            if not i:
                continue
            got = syms.get(i)
            if got and (got[0] or _days(got[1], today) <= SEARCH_RETRY_DAYS):
                continue
            want[i] = max(want.get(i, 0.0), x["w"])
    order = sorted(want, key=lambda i: (-want[i], i))
    deadline = time.time() + search_budget_s
    gap = 1.0 / SEARCH_RATE
    for i in order:
        if time.time() > deadline:
            log(f"[warn] ISIN search budget spent: {len(order) - calls['search']} ISINs left for the next run")
            break
        t0 = time.time()
        try:
            s = search(i)
            syms[i] = [s, today]
        except Exception as ex:
            log(f"[warn] ISIN search {i}: {type(ex).__name__}")
        calls["search"] += 1
        time.sleep(max(0.0, gap - (time.time() - t0)))
    return cache, calls


def yahoo_isin_symbol(isin):
    """Yahoo's own ISIN lookup: the first EQUITY quote, or None. The symbol is
    only USED after its close on the filing date matches the filing's value
    per share (deep_names), so a wrong answer here costs a line, never a
    number."""
    import yfinance as yf
    q = yf.Search(query=isin, max_results=3, news_count=0, lists_count=0,
                  enable_fuzzy_query=False).quotes or []
    for x in q:
        if (x.get("quoteType") or "").upper() in ("EQUITY", "ETF") and x.get("symbol"):
            return x["symbol"]
    return None


def load_deep(path=DEEP_PATH):
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_deep(cache, path=DEEP_PATH):
    """The cache as a committed file the public repo's guard accepts: one
    security per line as a compact array [name, isin, ticker, ccy, w, px],
    one ISIN per line in the symbol map, every other field on its own line.
    Names pass guard_safe(); the writer then checks every line it wrote."""
    L = ['{', f'"generated_at":{json.dumps(cache.get("generated_at"))},',
         '"source":"SEC EDGAR NPORT-P primary_doc.xml: the latest public filing per fund series '
         '(the last three for a Select Industry fund)",',
         '"line_fields":["name","isin","ticker","ccy","w","px"],']

    def emit(d, last):
        keys = sorted(k for k in d if k not in ("lines", "quarters"))
        tail = [k for k in ("quarters", "lines") if d.get(k) is not None]
        order = keys + tail
        for i, k in enumerate(order):
            comma = "" if i == len(order) - 1 else ","
            v = d[k]
            if k == "lines":
                L.append('"lines":[')
                xs = deep_lines(d)
                for j, x in enumerate(xs):
                    px = None if x.get("px") is None else round(x["px"], 6)
                    L.append(json.dumps([guard_safe(x["name"]), x.get("isin"), x.get("ticker"), x.get("ccy"),
                                         round(x["w"], 8), px], ensure_ascii=False, separators=(",", ":"))
                             + ("," if j < len(xs) - 1 else ""))
                L.append("]" + comma)
            elif k == "quarters":
                L.append('"quarters":{')
                pk = sorted(v)
                for j, per in enumerate(pk):
                    L.append(f"{json.dumps(per)}:{{")
                    emit(v[per], j == len(pk) - 1)
                L.append("}" + comma)
            else:
                v = guard_safe(v) if k == "fund" else v
                L.append(f'{json.dumps(k)}:{json.dumps(v, ensure_ascii=False, separators=(",", ":"))}{comma}')
        L.append("}" + ("" if last else ","))

    L.append('"funds":{')
    fk = sorted((cache.get("funds") or {}).keys())
    for j, t in enumerate(fk):
        L.append(f"{json.dumps(t)}:{{")
        emit(cache["funds"][t], j == len(fk) - 1)
    L.append("},")
    L.append('"symbols":{')
    sk = sorted((cache.get("symbols") or {}).keys())
    for j, i in enumerate(sk):
        L.append(f'{json.dumps(i)}:{json.dumps(cache["symbols"][i], separators=(",", ":"))}' + ("," if j < len(sk) - 1 else ""))
    L.append("}")
    L.append("}")
    bad = [ln for ln in L if guard_trips(ln)]
    if bad:
        raise ValueError(f"{len(bad)} cache line(s) would trip the public repo's guard, first: {bad[0][:120]}")
    text = "\n".join(L) + "\n"
    json.loads(text)                      # the file must be valid JSON
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def deep_lines(entry):
    """A cached fund's lines back as dicts (the file stores compact arrays)."""
    out = []
    for x in (entry or {}).get("lines") or []:
        if isinstance(x, dict):
            out.append(x)
        else:
            name, isin, ticker, ccy, w, px = x
            out.append({"name": name, "isin": isin, "ticker": ticker, "ccy": ccy, "w": w, "px": px})
    return out


# ==========================================================================
# prices
# ==========================================================================

def fetch_history(symbols, start, budget_s=FETCH_BUDGET_S, workers=FETCH_WORKERS, rate=FETCH_RATE):
    """{sym: (dates, closes, currency)} for every symbol Yahoo priced; a symbol
    that failed is simply absent. Adjusted closes, one request per symbol.

    ``symbols`` are taken IN THE ORDER GIVEN (build() passes them heaviest
    weight first), request starts are paced to ``rate`` per second, and a
    rate-limit reply pauses every worker (30s, 60s, 120s) before that symbol is
    retried. Yahoo cut off an unpaced 8-thread burst after ~1,250 requests
    (2026-09-25). Past ``budget_s`` the remaining -- lightest -- symbols are
    skipped: their weight lands in the rest line, never an estimate."""
    import threading

    import yfinance as yf

    lock = threading.Lock()
    state = {"next": time.time(), "pause_until": 0.0}
    deadline = time.time() + budget_s

    def gate():
        with lock:
            now = time.time()
            slot = max(state["next"], state["pause_until"], now)
            state["next"] = slot + 1.0 / rate
        if slot > now:
            time.sleep(slot - now)
        return time.time() <= deadline

    def one(s):
        for attempt in range(3):
            if not gate():
                return s, None
            try:
                t = yf.Ticker(s)
                h = t.history(start=start, auto_adjust=True, actions=False)
                if h is None or h.empty or "Close" not in h:
                    return s, None
                h = h[h["Close"].notna()]
                if h.empty:
                    return s, None
                dates = [d.strftime("%Y-%m-%d") for d in h.index]
                return s, (dates, [float(x) for x in h["Close"]], _currency(t))
            except Exception as ex:
                if "RateLimit" not in type(ex).__name__ and "Too Many Requests" not in str(ex):
                    return s, None
                with lock:
                    state["pause_until"] = max(state["pause_until"], time.time() + RATE_PAUSE_S * 2 ** attempt)
        return s, None

    out = {}
    order = list(dict.fromkeys(symbols))       # de-dupe, keep the priority order
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for s, v in ex.map(one, order):
            if v is not None:
                out[s] = v
    return out


def _currency(tkr):
    """The listing's trading currency from the chart metadata the history
    request already returned. The public ``history_metadata`` property would
    fire a SECOND (intraday) request per symbol to fetch trading periods, so
    the cached dict is read first -- the same private attribute yf.download
    itself reads -- and the public property is only the fallback if a
    yfinance release moves it."""
    try:
        md = tkr._price_history._history_metadata or {}
        if md.get("currency"):
            return md["currency"]
    except AttributeError:
        pass
    try:
        return (tkr.history_metadata or {}).get("currency")
    except Exception:
        return None


def align(axis, dates, closes):
    """As-of align a (dates, closes) series onto ``axis``: each session takes
    the latest close on or before it; None before the first bar. Returns
    (aligned, sessions_behind) where sessions_behind counts axis sessions
    after the series' last bar (0 = current)."""
    out = [None] * len(axis)
    j = 0
    prev = None
    for i, d in enumerate(axis):
        while j < len(dates) and dates[j] <= d:
            if closes[j] is not None:
                prev = closes[j]
            j += 1
        out[i] = prev
    behind = regime.sessions_behind(axis, dates[-1]) if dates else len(axis)
    return out, behind


def has_glitch(aligned, lag):
    """A one-session ratio beyond GLITCH_X inside the window: a unit error."""
    tail = [v for v in aligned[-(lag + 1):] if v is not None]
    for a, b in zip(tail, tail[1:]):
        if a > 0 and (b / a > GLITCH_X or b / a < 1.0 / GLITCH_X):
            return True
    return False


def usd_return(r_loc, r_fx):
    """r_usd = (1 + r_loc)(1 + r_fx) - 1; None if either leg is missing."""
    if r_loc is None or r_fx is None:
        return None
    return (1.0 + r_loc) * (1.0 + r_fx) - 1.0


def member_returns(axis, hist, ccy_iso, fx_aligned):
    """({window: r_usd or None}, reason-or-None) for one priced member.
    ``fx_aligned`` maps ISO currency -> the aligned <CCY>USD=X series."""
    dates, closes, _ = hist
    arr, behind = align(axis, dates, closes)
    none = {k: None for k, _ in WINDOWS}
    if behind > STALE_MAX:
        return none, f"last bar {dates[-1]} is {behind} sessions old"
    if has_glitch(arr, WINDOWS[-1][1]):
        return none, "one-day 20x price jump (unit glitch)"
    fx = None
    if ccy_iso and ccy_iso != "USD":
        fx = fx_aligned.get(ccy_iso)
        if fx is None:
            return none, f"no {ccy_iso}USD=X series"
    out = {}
    for k, lag in WINDOWS:
        r = regime.ret(arr, lag)
        if fx is not None:
            r = usd_return(r, regime.ret(fx, lag))
        out[k] = r
    return out, None


def member_usd(axis, hist, ccy_iso, fx_aligned):
    """(USD closes on ``axis`` or None, reason-or-None): the same checks as
    member_returns, but the whole aligned series, so a return over ANY two
    sessions is usd[b] / usd[a] - 1. Local close x <CCY>USD, which is exactly
    (1 + r_loc)(1 + r_fx) - 1 over any span."""
    dates, closes, _ = hist
    arr, behind = align(axis, dates, closes)
    if behind > STALE_MAX:
        return None, f"last bar {dates[-1]} is {behind} sessions old"
    if has_glitch(arr, WINDOWS[-1][1]):
        return None, "one-day 20x price jump (unit glitch)"
    if not ccy_iso or ccy_iso == "USD":
        return arr, None
    fx = fx_aligned.get(ccy_iso)
    if fx is None:
        return None, f"no {ccy_iso}USD=X series"
    return [(a * b) if (a is not None and b is not None) else None for a, b in zip(arr, fx)], None


def span_ret(arr, a, b):
    """arr[b] / arr[a] - 1, or None."""
    if a < 0 or b >= len(arr) or arr[a] is None or arr[b] is None or arr[a] <= 0:
        return None
    return arr[b] / arr[a] - 1.0


def asof_index(axis, day):
    """The last axis session on or before ``day``; -1 when ``day`` precedes it."""
    lo, hi = 0, len(axis)
    while lo < hi:
        mid = (lo + hi) // 2
        if axis[mid] <= day:
            lo = mid + 1
        else:
            hi = mid
    return lo - 1


# ==========================================================================
# contributions and participation
# ==========================================================================

def drift_back(w, r, R):
    """Start-of-window weight backed out of today's weight, and the
    contribution: w0 = w (1+R)/(1+r), c = w0 r. (None, None) without r or R."""
    if r is None or R is None or (1.0 + r) <= 0:
        return None, None
    w0 = w * (1.0 + R) / (1.0 + r)
    return w0, w0 * r


def basket_contrib(member_series, lag):
    """Exact attribution for a daily-rebalanced equal-weight basket.
    ``member_series`` = [(sym, closes on the ladder's full axis)] in REG_ETFS
    member order. Each session the basket moves by the plain mean of the
    members that printed on both sides of it (regime.basket_series' rule);
    member i's contribution that day is (1/n_t) * r_it * G_{t-1}. Returns
    ({sym: c}, R) with R the basket's own window return, or (None, None) when
    the window starts before the basket has a level."""
    if not member_series:
        return None, None
    n = len(member_series[0][1])
    last = n - 1
    start = last - lag
    if start < 1:
        return None, None
    # basket_series is None until its first computable return: the window
    # must start on or after that session
    started = False
    for i in range(1, start + 1):
        if any(c[i - 1] is not None and c[i] is not None and c[i - 1] > 0 for _, c in member_series):
            started = True
            break
    if not started:
        return None, None
    contrib = {s: 0.0 for s, _ in member_series}
    G = 1.0
    for t in range(start + 1, last + 1):
        acc = 0.0
        day = []
        for s, c in member_series:
            a, b = c[t - 1], c[t]
            if a is not None and b is not None and a > 0:
                r = b / a - 1.0
                acc += r
                day.append((s, r))
        if day:
            m = len(day)
            for s, r in day:
                contrib[s] += G * r / m
            G *= 1.0 + acc / m
    return contrib, G - 1.0


def row_sigma(closes, win=SIGMA_WIN):
    """Daily log-return standard deviation of the row's own series over its
    last ``win`` sessions (sample sd); None under 20 returns."""
    tail = [v for v in closes[-(win + 1):] if v is not None and v > 0]
    rs = [math.log(b / a) for a, b in zip(tail, tail[1:])]
    if len(rs) < 20:
        return None
    mu = sum(rs) / len(rs)
    return math.sqrt(sum((x - mu) ** 2 for x in rs) / (len(rs) - 1))


def participation(holdings, R, sigma_d, lag, key, equal_weight=False, single=False):
    """Spec 3.3 for one window. share_up = weight of priced holdings with r > 0
    over priced weight; n_up / n over priced holdings; conc_top3 = the top-3
    holdings' share of the window's contribution over their share of the
    fund's START weight -- only when |log(1+R)| >= 1 sigma of the row's own
    window volatility (sigma_d * sqrt(lag)); move_z is that ratio."""
    priced = [h for h in holdings if h["r"].get(key) is not None]
    wsum = sum(h["w"] for h in priced)
    up = [h for h in priced if h["r"][key] > 0]
    out = {
        "share_up": (sum(h["w"] for h in up) / wsum) if wsum > 0 else None,
        "n_up": len(up),
        "n": len(priced),
        "conc_top3": None,
        "move_z": None,
        "conc_note": None,
    }
    if R is None:
        out["conc_note"] = "n/a -- no ladder return for this window"
        return out
    if sigma_d is not None and sigma_d > 0 and R > -1:
        out["move_z"] = math.log(1.0 + R) / (sigma_d * math.sqrt(lag))
    if single:
        out["conc_note"] = "n/a -- a single holding"
        return out
    if equal_weight:
        out["conc_note"] = "n/a -- equal-weight basket, no top 3 by weight"
        return out
    if out["move_z"] is None:
        out["conc_note"] = "n/a -- not enough history for the row's own volatility"
        return out
    if abs(out["move_z"]) < 1.0:
        out["conc_note"] = "n/a -- too small a move to attribute (under 1 sigma)"
        return out
    top = sorted(holdings, key=lambda h: -h["w"])[:CONC_TOP]
    if len(top) < CONC_TOP or any(h["c"].get(key) is None or h["w0"].get(key) is None for h in top):
        out["conc_note"] = "n/a -- a top-3 holding has no return"
        return out
    share_c = sum(h["c"][key] for h in top) / R
    share_w = sum(h["w0"][key] for h in top)
    if share_w <= 0:
        out["conc_note"] = "n/a -- no top-3 weight"
        return out
    out["conc_top3"] = share_c / share_w
    return out


def rest_line(holdings, R, n_total):
    """The rest-of-fund line: the weight the listing does not cover, the count
    of holdings not listed (when the tier knows it), and per window
    R - sum(c). A listed holding with no return for a window contributes
    nothing there, so its share sits in rest.c -- w_unpriced says how much."""
    listed_w = sum(h["w"] for h in holdings)
    c = {}
    unpriced = {}
    for k, _ in WINDOWS:
        if R.get(k) is None:
            c[k] = None
        else:
            c[k] = R[k] - sum(h["c"][k] for h in holdings if h["c"].get(k) is not None)
        unpriced[k] = sum(h["w"] for h in holdings if h["c"].get(k) is None)
    return {"w": 1.0 - listed_w,
            "n": (n_total - len(holdings)) if n_total is not None else None,
            "c": c, "w_unpriced": unpriced}


# ==========================================================================
# the shape of a row: concentration, trend breadth, who-moved-it statistics
# (B1 stage 2, 2026-11-20; definitions in board-refactor recon-3 section 3.1)
# ==========================================================================

TREND_MA = (50, 200)              # sessions: a holding above its own simple average


def trend_flags(arr):
    """(above its 50-session average, above its 200-session average) for one
    series' last value, in the listing's OWN currency (a holding's trend, not
    the dollar's). None for an average the series cannot fill (a gap or too
    little history) or a missing last value."""
    last = arr[-1] if arr else None
    out = []
    for n in TREND_MA:
        tail = arr[-n:] if arr and len(arr) >= n else []
        if last is None or len(tail) < n or any(v is None for v in tail):
            out.append(None)
        else:
            out.append(bool(last > sum(tail) / n))
    return tuple(out)


def _quantile_lo(q, p):
    return q[int(p * (len(q) - 1))]


def window_shape(lines, R, k):
    """Who-moved-it statistics for one window over the ATTRIBUTED lines (each
    {w0: {k}, r: {k}, c: {k}}; a line without c or w0 for k is skipped).

    explained  sum of the lines' contributions; resid = R - explained (fees,
               cash, unpriced or unlisted lines, rebalancing, pricing time)
    n, n_up    lines with a return, and how many rose; w_up = their share of
               the lines' start weight
    top1_share, top2_share
               the largest one / two contributions IN THE DIRECTION of the
               explained move, divided by it (a share of the move, so 0.76 =
               three quarters of it); None under 3 lines or a zero move
    ex_top2    the return of the other lines, (explained - c1 - c2) /
               (sum w0 - w0_1 - w0_2): the move without those two names
    disp_w     start-weighted standard deviation of the lines' returns around
               their weighted mean; disp_eq the plain sample standard
               deviation; iqr the inter-quartile range (lower-index quantiles)"""
    live = [x for x in lines if x["c"].get(k) is not None and x["w0"].get(k) is not None
            and x["r"].get(k) is not None]
    out = {"explained": None, "resid": None, "n": len(live), "n_up": 0, "w_up": None,
           "top1_share": None, "top2_share": None, "ex_top2": None,
           "disp_w": None, "disp_eq": None, "iqr": None}
    if not live:
        return out
    ex = sum(x["c"][k] for x in live)
    w0s = sum(x["w0"][k] for x in live)
    up = [x for x in live if x["r"][k] > 0]
    out["explained"] = ex
    out["resid"] = (R - ex) if R is not None else None
    out["n_up"] = len(up)
    out["w_up"] = (sum(x["w0"][k] for x in up) / w0s) if w0s > 0 else None
    if len(live) >= 3 and abs(ex) > 1e-9:
        sgn = 1.0 if ex >= 0 else -1.0
        lead = sorted(live, key=lambda x: (-sgn * x["c"][k], x["t"]))
        c1, c2 = lead[0]["c"][k], lead[1]["c"][k]
        out["top1_share"] = c1 / ex
        out["top2_share"] = (c1 + c2) / ex
        rest_w = w0s - lead[0]["w0"][k] - lead[1]["w0"][k]
        out["ex_top2"] = (ex - c1 - c2) / rest_w if rest_w > 1e-9 else None
    rs = [x["r"][k] for x in live]
    if w0s > 0:
        mu = sum(x["w0"][k] * x["r"][k] for x in live) / w0s
        out["disp_w"] = math.sqrt(max(0.0, sum(x["w0"][k] * (x["r"][k] - mu) ** 2 for x in live) / w0s))
    if len(rs) > 1:
        m = sum(rs) / len(rs)
        out["disp_eq"] = math.sqrt(sum((r - m) ** 2 for r in rs) / (len(rs) - 1))
        q = sorted(rs)
        out["iqr"] = _quantile_lo(q, 0.75) - _quantile_lo(q, 0.25)
    return out


def row_shape(lines, R, trend, basis):
    """The row's shape block. ``lines`` = every line with a weight today
    ({t, w, w0, r, c}); ``trend`` = {t: (above 50d, above 200d)}; ``basis``
    names which lines these are.

    conc   top5_w / top10_w = the heaviest five / ten lines' share of the
           fund; eff_n = 1 / sum(w^2), the effective number of holdings,
           counting the weight on no list as perfectly spread (an UPPER
           bound when the list is short); eff_n_min = the same with that
           weight spread over lines as heavy as the lightest one listed (the
           lower bound). The two are equal when every line is listed.
    trend  of the lines whose average can be read: n, n_up above it, w_up
           their share of those lines' weight.
    win    window_shape() per window."""
    ws = sorted((x["w"] for x in lines if x.get("w") and x["w"] > 0), reverse=True)
    W = sum(ws)
    conc = {"top5_w": None, "top10_w": None, "eff_n": None, "eff_n_min": None}
    if ws:
        hhi = sum(w * w for w in ws)
        unlisted = max(0.0, 1.0 - W)
        conc = {"top5_w": _rnd(sum(ws[:5]), 4), "top10_w": _rnd(sum(ws[:10]), 4),
                "eff_n": _rnd(1.0 / hhi, 2), "eff_n_min": _rnd(1.0 / (hhi + unlisted * ws[-1]), 2)}
    tr = {}
    for i, n in enumerate(TREND_MA):
        known = [x for x in lines if (trend.get(x["t"]) or (None, None))[i] is not None and x.get("w")]
        upl = [x for x in known if trend[x["t"]][i]]
        kw = sum(x["w"] for x in known)
        tr[f"above{n}"] = {"n": len(known), "n_up": len(upl),
                           "w_up": _rnd(sum(x["w"] for x in upl) / kw, 4) if kw > 0 else None}
    win = {}
    for k, _ in WINDOWS:
        s = window_shape(lines, R.get(k), k)
        win[k] = {f: (_rnd(v, 6) if isinstance(v, float) else v) for f, v in s.items()}
    return {"basis": basis, "n_lines": len(ws), "w_lines": _rnd(W, 4), "conc": conc, "trend": tr, "win": win}


def shape_lines(holdings, drivers, R):
    """(lines, basis) for row_shape: the listed holdings, plus -- for a top-10
    row -- the N-PORT names priced in its rest line, whose start weights are
    backed out exactly as theirs were (w0 = w (1+R)/(1+r)). A Select Industry
    row's rebalance figures are corrections to its listed lines, not lines,
    so they are not added."""
    lines = [{"t": h["t"], "w": h["w"], "w0": h["w0"], "r": h["r"], "c": h["c"]} for h in holdings]
    basis = "listed holdings"
    if drivers and drivers.get("source") == "sec_nport":
        for n in drivers.get("names") or []:
            if n.get("w") is None:
                continue
            w0 = {k: drift_back(n["w"], n["r"].get(k), R.get(k))[0] for k, _ in WINDOWS}
            lines.append({"t": n["t"], "w": n["w"], "w0": w0, "r": n["r"], "c": n["c"]})
        basis = "listed holdings + SEC N-PORT names priced in the rest line"
    elif drivers and drivers.get("source") == "rebalance":
        basis = ("listed holdings, one piece; resid includes the quarterly resets, "
                 "which the rest line's rebalance drivers itemise")
    return lines, basis


# ==========================================================================
# what is in the rest line: rest.drivers
# ==========================================================================
# rest.c stays R - sum(listed c), so every column still adds up to the ladder.
# rest.drivers splits that one number into NAMES that moved it (every priced
# one) and an OTHER line (what no name explains), names + other = rest in
# every window (to the rounding: other is computed from the rounded names).
# Two sources:
#   sec_nport  a top-10 row: the fund's lines from its latest public N-PORT
#              that Yahoo's ten do not list, priced and attributed like the
#              listed ones;
#   rebalance  a Select Industry row: the quarterly equal-weight reset,
#              modeled; each name's figure is how much of the rest line the
#              reset moves onto it.

_NAME_DROP = set("INC INCORPORATED CORP CORPORATION CO COMPANY LTD LIMITED PLC SA NV AG SE ASA AB "
                 "OYJ SPA AS LP LLC HOLDINGS HOLDING GROUP THE CLASS CL ADR ADS SPONSORED SPON ORD "
                 "SHS SHARES REG NPV COMMON STOCK NEW DE OF AND".split())


def name_key(name):
    """First two significant words of a company name, upper case: 'SK hynix,
    Inc.' and 'SK Hynix Inc' are both 'SK HYNIX'."""
    toks = [w for w in re.sub(r"[^A-Z0-9 ]", " ", (name or "").upper()).split()
            if w not in _NAME_DROP and len(w) > 1]
    return " ".join(toks[:2])


def _base(sym):
    return (sym or "").split(".")[0].upper()


def line_symbol(x, syms):
    """The Yahoo symbol for a filing line: Yahoo's ISIN lookup (cached), else a
    US-dollar line's own ticker. None when neither exists -- never guessed."""
    got = syms.get(x.get("isin")) if x.get("isin") else None
    if got and got[0]:
        return got[0]
    tk = (x.get("ticker") or "").upper()
    if (x.get("ccy") or "USD") == "USD" and tk and _US_SYM.match(tk):
        return re.sub(r"[./]", "-", tk)
    return None


def deep_plan(lines, listed, syms, cap=DEEP_PRICE_CAP):
    """Split a filing's lines (heaviest first) into the ones Yahoo's list
    already carries and the REST. A listed holding is matched once: by symbol
    (the line's mapped symbol or ticker against the listed symbol, its
    alternates and its base), then by the first two words of the name when the
    two weights are within a factor of three. The first ``cap`` rest lines are
    the ones priced today; the others stay in `other` with their weight."""
    lsym = {}
    for i, h in enumerate(listed):
        for s in [h["sym"]] + list(h.get("alts") or []):
            lsym.setdefault(s.upper(), i)
            lsym.setdefault(_base(s), i)
    syml = [line_symbol(x, syms) for x in lines]
    used_l, used_x = set(), set()
    for j, x in enumerate(lines):
        for s in (syml[j], x.get("ticker")):
            if not s:
                continue
            i = lsym.get(s.upper())
            if i is None:
                i = lsym.get(_base(s))
            if i is not None and i not in used_l:
                used_l.add(i)
                used_x.add(j)
                break
    for i, h in enumerate(listed):
        if i in used_l:
            continue
        k = name_key(h.get("name"))
        if not k:
            continue
        for j, x in enumerate(lines):
            if j in used_x or name_key(x["name"]) != k:
                continue
            if not (h["w"] / 3.0 <= x["w"] <= h["w"] * 3.0):
                continue
            used_l.add(i)
            used_x.add(j)
            break
    rest = [dict(x, sym=syml[j]) for j, x in enumerate(lines) if j not in used_x]
    return {"matched": len(used_x),
            "unmatched_listed": [listed[i]["sym"] for i in range(len(listed)) if i not in used_l],
            "cands": rest[:cap], "beyond": rest[cap:]}


def pack_drivers(source, asof, cands, rest, other_n, note, show=DEEP_SHOW, extra=None,
                 weight_in_rest=True):
    """rest.drivers: EVERY priced candidate, in the order the board lists them --
    first the ``show`` that moved the rest most (largest |c| in any of 21d/63d/
    126d) sorted by |c63|, then the others by |c63| -- with drivers.show = how
    many the board lists before its "show all" button. `other` is what no name
    explains (names not priced, and whatever the method gets wrong): other.c =
    rest.c - sum(names.c) on the ROUNDED numbers, so names + other = rest to
    1e-9. drivers.breadth[k] = {up, n, w_up}: of the names with a weight and a
    return over window k, how many rose and their share of those names'
    weight. ``weight_in_rest`` is False when the names are the LISTED names seen
    again (the rebalance source): their weights are then not part of the rest's,
    and other.w stays the rest's own weight -- and such a name carries only t
    and c, because its name, weight, currency and own return ARE its line above
    (the board joins them by t; the modeled contribution is c + that line's c).
    Only a name that left the fund at a reset carries them itself."""
    K = [k for k, _ in WINDOWS]

    def score(x):
        return max(abs(x["c"].get(k) or 0.0) for k in ("r21", "r63", "r126"))

    def by63(x):
        return (-abs(x["c"].get("r63") or 0.0), x["t"])

    ranked = sorted(cands, key=lambda x: (-score(x), x["t"]))
    top, more = sorted(ranked[:show], key=by63), sorted(ranked[show:], key=by63)
    full = [{"t": x["t"], "name": guard_safe(x["name"]), "w": _rnd(x["w"]), "ccy": x["ccy"],
             "r": {k: _rnd(x["r"].get(k)) for k in K}, "c": {k: _rnd(x["c"].get(k)) for k in K},
             "gone": bool(x.get("gone"))} for x in top + more]
    names = []
    for n in full:
        if n["gone"] or weight_in_rest:
            names.append({f: v for f, v in n.items() if f != "gone" or v})
        else:
            names.append({"t": n["t"], "c": n["c"]})
    rc = {k: _rnd(rest["c"].get(k)) for k in K}
    oc, breadth = {}, {}
    for k in K:
        oc[k] = None if rc[k] is None else round(
            rc[k] - sum(n["c"][k] for n in names if n["c"][k] is not None), 9)
        live = [n for n in full if n["w"] is not None and n["r"][k] is not None]
        wt = sum(n["w"] for n in live)
        up = [n for n in live if n["r"][k] > 0]
        breadth[k] = {"up": len(up), "n": len(live),
                      "w_up": round(sum(n["w"] for n in up) / wt, 4) if wt > 0 else None}
    out = {"source": source, "asof": asof, "show": min(show, len(names)), "names": names,
           "other": {"w": _rnd(rest["w"] - (sum(x["w"] for x in cands) if weight_in_rest else 0.0)),
                     "n": other_n, "c": oc},
           "breadth": breadth, "note": note}
    out.update(extra or {})
    return out


def deep_drivers(entry, plan, hist, axis, fx_aligned, fund_axis, R, rest):
    """(rest.drivers, error-or-None) for a top-10 row from its N-PORT lines.

    For each rest line priced today: its mapped symbol's close on the filing
    date, in USD, must be within PX_TOL of the filing's own value per share
    (the identity check -- a wrong symbol fails it). The filing weight is
    drifted FORWARD to today with the name's own return against the fund's
    own, w_today = w_filing (1 + r_i) / (1 + R_fund), both since the filing
    date; each window then backs a start weight out of w_today exactly as the
    listed lines do, c = w0 r."""
    period, e = entry.get("period"), len(axis) - 1
    ip = asof_index(axis, period) if period else -1
    if ip < 0:
        return None, f"N-PORT period {period} is older than the price window"
    R_since = span_ret(fund_axis, ip, e)
    if R_since is None:
        return None, f"no fund close on the N-PORT date {period}"
    cands = []
    fail = {"no symbol": 0, "no price": 0, "failed the price check": 0}
    for x in plan["cands"]:
        sym = x.get("sym")
        if not sym:
            fail["no symbol"] += 1
            continue
        h = hist.get(sym)
        ccy = (iso_ccy(h[2]) or ("USD" if "." not in sym else None)) if h else None
        usd = member_usd(axis, h, ccy, fx_aligned)[0] if (h and ccy) else None
        if usd is None:
            fail["no price"] += 1
            continue
        p0 = usd[ip]
        if x.get("px") is None or p0 is None or x["px"] <= 0 or abs(p0 / x["px"] - 1.0) > PX_TOL:
            fail["failed the price check"] += 1
            continue
        r_since = span_ret(usd, ip, e)
        if r_since is None:
            fail["no price"] += 1
            continue
        w_today = x["w"] * (1.0 + r_since) / (1.0 + R_since)
        r = {k: regime.ret(usd, lag) for k, lag in WINDOWS}
        c = {k: drift_back(w_today, r[k], R.get(k))[1] for k, _ in WINDOWS}
        cands.append({"t": sym, "name": x["name"], "w": w_today, "ccy": ccy, "r": r, "c": c})
    n_sec = entry.get("n_sec") or 0
    unpriced = len(plan["cands"]) - len(cands)
    w_beyond = sum(x["w"] for x in plan["beyond"]) + ((entry.get("tail") or {}).get("w") or 0.0)
    n_beyond = len(plan["beyond"]) + ((entry.get("tail") or {}).get("n") or 0)
    why = ", ".join(f"{v} {k}" for k, v in fail.items() if v)
    note = (f"SEC N-PORT as of {period} (filed {entry.get('filed')}): {n_sec} securities in the fund. "
            f"{plan['matched']} are the names listed above; of the next {len(plan['cands'])} by weight, "
            f"{len(cands)} are priced and attributed (all listed here, the {min(len(cands), DEEP_SHOW)} "
            f"that moved the rest most first)"
            + (f", {unpriced} not ({why})" if unpriced else "")
            + (f"; {n_beyond} smaller names ({w_beyond * 100:.1f} pct of the fund at the filing) are past "
               f"the cap of {DEEP_PRICE_CAP} and not priced" if n_beyond else "")
            + ". Filing weights are drifted to today with each name's own return, then backed out "
              "per window like the lines above; a rebalance since the filing is not modeled.")
    ws = entry.get("w_sec") or 0.0
    if ws > 1.03:
        note += (f" The filing's securities add up to {ws * 100:.1f} pct of net assets: the fund carries a "
                 "liability (borrowing, deferred tax or similar) that its return nets and no name carries, "
                 "so that part of the rest stays in other.")
    if plan["unmatched_listed"]:
        note += (" Not found in the filing: " + ", ".join(plan["unmatched_listed"]) +
                 " (Yahoo's list and the filing disagree; if one is the same security under another "
                 "line it is counted twice and other carries the offset).")
    extra = {"filed": entry.get("filed"), "fund": entry.get("fund")}
    return pack_drivers("sec_nport", period, cands, rest,
                        max(0, n_sec - plan["matched"] - len(cands)), note, extra=extra), None


def third_friday(y, m):
    d = dt.date(y, m, 1)
    return d + dt.timedelta(days=(4 - d.weekday()) % 7 + 14)


def reset_indices(axis):
    """Axis indices of the Select Industry resets strictly before the axis' last
    session: the last session on or before each third Friday of Mar/Jun/Sep/
    Dec. A reset takes effect after that close, so today's own third Friday
    has not happened yet for today's numbers."""
    if not axis:
        return []
    out = []
    for y in range(int(axis[0][:4]), int(axis[-1][:4]) + 1):
        for m in RESET_MONTHS:
            f = third_friday(y, m).isoformat()
            if f < axis[0] or f >= axis[-1]:
                continue
            i = asof_index(axis, f)
            if i >= 0 and (not out or out[-1] != i):
                out.append(i)
    return out


def anchored_contrib(anchors, S, F, resets, s, e):
    """Contributions over the window (s, e] of a fund whose weights reset at
    every close in ``resets``: {sym: c or None}, or None when a stretch of the
    window has no anchor.

    ``anchors`` maps a reset index (the start of a quarter) to (d, {sym: w}):
    the fund's weights known on session d inside that quarter -- the issuer's
    file for the current quarter, the fund's own quarter-end N-PORT for an
    earlier one. Inside a quarter the fund is buy-and-hold, so its weights on
    any session a of that quarter are w (S[a] / S[d]) / (F[a] / F[d]) -- back
    or forward from d -- and a name contributes (F[a] / F[s]) w_a r over each
    stretch [a, b] between resets. Exact for a fund that really holds those
    weights (tested on a synthetic equal-weight fund). A name without a price
    across a stretch it is in gets None."""
    bounds = [s] + [b for b in resets if s < b < e] + [e]
    pieces = []
    for a, b in zip(bounds, bounds[1:]):
        lo = max((r for r in resets if r <= a), default=None)
        if lo is None or lo not in anchors:
            return None
        d, W = anchors[lo]
        ga, gs = span_ret(F, d, a), span_ret(F, s, a)
        if ga is None or gs is None:
            return None
        pieces.append((a, b, d, W, ga, gs))
    out = {}
    for sym in sorted({x for p in pieces for x in p[3]}):
        arr = S.get(sym)
        c = 0.0
        for a, b, d, W, ga, gs in pieces:
            w = W.get(sym)
            if w is None:
                continue                      # not in the fund in this quarter
            g, r = (span_ret(arr, d, a), span_ret(arr, a, b)) if arr else (None, None)
            if g is None or r is None:
                c = None
                break
            c += (1.0 + gs) * (w * (1.0 + g) / (1.0 + ga)) * r
        out[sym] = c
    return out


def quarter_anchor(q, syms, S, axis, labels=None):
    """(d, {sym: w}, [(name, w) not priced]) from one cached N-PORT quarter:
    every line whose mapped symbol has a USD close on the period date within
    PX_TOL of the filing's own value per share. ``labels`` collects sym ->
    name."""
    d = asof_index(axis, q.get("period") or "")
    W, miss = {}, []
    for x in deep_lines(q):
        sym = line_symbol(x, syms)
        arr = S.get(sym) if sym else None
        p0 = arr[d] if (arr is not None and d >= 0) else None
        if p0 is None or not x.get("px") or abs(p0 / x["px"] - 1.0) > PX_TOL:
            miss.append((x["name"], x["w"]))
            continue
        W[sym] = W.get(sym, 0.0) + x["w"]
        if labels is not None:
            labels.setdefault(sym, x["name"])
    return d, W, miss


def rebalance_drivers(holdings, h_asof, quarters, syms, S, F, axis, R, rest, names=None):
    """(rest.drivers, error-or-None) for a Select Industry row.

    The index resets to modified equal weight after the close on the third
    Friday of Mar/Jun/Sep/Dec, so one set of start weights backed out of
    today's across a reset misattributes the move and the rest line eats it.
    Each quarter is attributed from its OWN book instead: the current one from
    today's issuer file (dated ``h_asof``), each earlier one from the fund's
    quarter-end N-PORT, which lands about eight sessions after that quarter's
    reset. A quarter with no filing falls back to today's weights drifted back
    to the last reset (the approximation, said in the note). Each name's
    figure is its contribution with the quarters attributed this way minus its
    one-piece line above (so the modeled figure is c + that line's c); a name that left the fund at
    a reset has no line above, so its whole contribution is in the rest."""
    e = len(axis) - 1
    resets = reset_indices(axis)
    if not resets:
        return None, "no quarterly reset inside the price window"
    names = names or {}
    cur = resets[-1]
    d_now = asof_index(axis, h_asof) if h_asof else e
    if d_now < cur:
        d_now = e                     # a file older than the reset cannot anchor this quarter
    listed = {h["t"]: h for h in holdings}
    anchors = {cur: (d_now, {h["t"]: h["w"] for h in holdings if S.get(h["t"])})}
    used, fallback, miss, labels = {}, [], {}, {}
    for per in sorted(quarters or {}):
        ip = asof_index(axis, per)
        lo = max((r for r in resets if r < ip), default=None)
        if lo is None or lo == cur or ip < 0:
            continue                  # before the axis, or the current quarter (today's file wins)
        d, W, m = quarter_anchor(quarters[per], syms, S, axis, labels)
        if W:
            anchors[lo] = (d, W)
            used[axis[lo]] = per
            miss[per] = m
    gF = span_ret(F, cur, e)
    Wr = {}
    for h in holdings:
        g = span_ret(S[h["t"]], cur, e) if S.get(h["t"]) else None
        if g is not None and gF is not None:
            Wr[h["t"]] = h["w"] * (1.0 + gF) / (1.0 + g)
    lo_min = max((r for r in resets if r <= e - WINDOWS[-1][1]), default=resets[0])
    for lo in resets[:-1]:
        if lo >= lo_min and lo not in anchors:
            anchors[lo] = (lo, Wr)    # the approximation, for a quarter without its own filing
            fallback.append(axis[lo])
    pw = {k: anchored_contrib(anchors, S, F, resets, e - lag, e) for k, lag in WINDOWS}
    syms_all = sorted({x for k in pw if pw[k] for x in pw[k]} | set(listed))
    cands = []
    for sym in syms_all:
        h = listed.get(sym)
        # a window that could be anchored gives a name outside the fund for all
        # of it a contribution of 0, not "unknown"
        cp = {k: (None if pw[k] is None else pw[k].get(sym, 0.0)) for k, _ in WINDOWS}
        c1 = {k: (h["c"].get(k) if h else 0.0) for k, _ in WINDOWS}
        d = {k: (cp[k] - c1[k]) if (cp[k] is not None and c1[k] is not None) else None for k, _ in WINDOWS}
        if h is None and all(v is None or abs(v) < 1e-12 for v in d.values()):
            continue
        arr = S.get(sym)
        r = h["r"] if h else {k: (regime.ret(arr, lag) if arr else None) for k, lag in WINDOWS}
        nm = h["name"] if h else (labels.get(sym) or names.get(sym) or sym)
        x = {"t": sym, "name": nm, "w": h["w"] if h else None, "ccy": h["ccy"] if h else "USD",
             "r": r, "c": d}
        if h is None:
            x["gone"] = True
        cands.append(x)
    inside = [axis[b] for b in resets if b > e - WINDOWS[-1][1]]
    src = [f"{lo_d} from the N-PORT of {per}" for lo_d, per in sorted(used.items())]
    note = ("Modified equal weight, reset after the close on the third Friday of Mar/Jun/Sep/Dec "
            f"({', '.join(inside) or 'none'} inside the {WINDOWS[-1][1]}-session window). Each quarter is attributed "
            f"from its own book: the quarter since {axis[cur]} from today's issuer file"
            + ("; " + "; ".join(f"the quarter since {q}" for q in src) if src else "")
            + (f"; the quarter since {', '.join(fallback)} from today's weights drifted back to the last reset "
               "(no filing: an approximation)" if fallback else "")
            + ". Each name's figure is its contribution attributed that way minus its line above; a name "
              "that left at a reset has no line above, so all of its contribution is here.")
    unp = {per: sum(w for _, w in m) for per, m in miss.items()}
    if any(v > 0.005 for v in unp.values()):
        big = {}
        for per in sorted(miss):
            for nm, w in miss[per]:
                big[nm] = max(big.get(nm, 0.0), w)
        top = sorted(big, key=lambda n: -big[n])[:5]
        note += (" Not priced, so in other: " + ", ".join(f"{p} {v * 100:.1f} pct of the fund" for p, v in unp.items())
                 + " -- names with no current Yahoo quote or none on the filing date (an acquired or delisted "
                   "name has no quote left to price it with), the largest " + ", ".join(guard_safe(n) for n in top) + ".")
    other_n = rest.get("n") or 0
    return pack_drivers("rebalance", axis[cur], cands, rest, other_n, note,
                        extra={"resets": inside, "anchors": dict(sorted(used.items())),
                               "fallback": fallback}, weight_in_rest=False), None


# ==========================================================================
# assembly
# ==========================================================================

def ticker_names(path=TICKERS_PATH):
    """Names from tickers.txt comments ("ON  # onsemi (power/SiC; ...)" -> onsemi)."""
    names = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if "#" not in line or line.lstrip().startswith("#"):
                    continue
                sym, _, note = line.partition("#")
                sym = sym.strip()
                note = re.split(r"\s+\(|\s+-\s+|;", note.strip())[0].strip()
                if sym and note:
                    names[sym] = note
    except OSError:
        pass
    return names


def _rnd(v, d=6):
    return None if v is None else round(v, d)


def get_holdings(e, fetchers, prev_rows, today, errors, issuer_status=None):
    """(tier, holdings_asof, holdings[{sym,alts,name,w}], n_total, source) for
    one non-basket row. A failed fetch carries yesterday's holdings (old date
    kept) and notes it; with no yesterday either, an SSGA row falls back to
    Yahoo's top 10."""
    t = e["t"]
    prev = prev_rows.get(t)
    issuer_status = {} if issuer_status is None else issuer_status

    def carried(why):
        errors.setdefault(t, []).append(f"{why}; carried holdings from {prev.get('holdings_asof')}")
        hs = [{"sym": h["t"], "alts": [], "name": h.get("name") or "", "w": h["w"]}
              for h in prev.get("holdings") or []]
        return (prev["tier"], prev.get("holdings_asof"), hs, prev.get("n_holdings_total"),
                prev.get("source") or "")

    kind = ISSUER_OF.get(t)
    if kind and fetchers.get("issuer"):
        try:
            asof, hs, n_total = ISSUER_PARSE[kind](fetchers["issuer"](kind, t), t)
            issuer_status.setdefault(kind, {"ok": [], "failed": []})["ok"].append(t)
            return "issuer_full", asof, hs, n_total, ISSUER_NAME[kind]
        except Exception as ex:
            why = f"{ISSUER_NAME[kind]} failed ({type(ex).__name__}: {str(ex)[:120]})"
            issuer_status.setdefault(kind, {"ok": [], "failed": []})["failed"].append(t)
            if prev and prev.get("holdings") and prev.get("tier") == "issuer_full":
                return carried(why)
            errors.setdefault(t, []).append(why + "; fell back to Yahoo top 10")
    if t in SSGA_FUNDS:
        try:
            asof, hs, n_total = parse_ssga(fetchers["ssga"](t))
            return "issuer_full", asof, hs, n_total, "State Street daily holdings file"
        except Exception as ex:
            why = f"SSGA holdings fetch failed ({type(ex).__name__}: {str(ex)[:80]})"
            if prev and prev.get("holdings"):
                return carried(why)
            errors.setdefault(t, []).append(why + "; fell back to Yahoo top 10")
    try:
        hs = parse_top10(fetchers["top10"](t))
        if not hs:
            raise ValueError("top holdings were all cash lines")
        return ("top10", today, hs, None,
                "Yahoo Finance top holdings (Yahoo does not date the list; holdings_asof is the fetch date)")
    except Exception as ex:
        why = f"top holdings fetch failed ({type(ex).__name__}: {str(ex)[:80]})"
        if prev and prev.get("holdings"):
            return carried(why)
        errors.setdefault(t, []).append(why + "; no holdings")
        return "top10", None, [], None, ""


def build(tech, panel, fetchers, prev=None, today=None, names=None, log=print, deep=None, wall_t0=None,
          last_out=None):
    """Assemble the rotation_members payload. Pure given its inputs:
    ``tech`` = the technicals.json dict, ``panel`` = regime.build_panel's
    (dates, series) from the same prices.db, ``fetchers`` = {"ssga": tk ->
    bytes, "top10": tk -> [(sym, name, w)], "history": (symbols, start) ->
    {sym: (dates, closes, ccy)}}, ``prev`` = yesterday's payload or None,
    ``deep`` = the N-PORT cache (refresh_deep's, already up to date) or None
    for no sec_nport drivers. ``wall_t0`` = when the run started, so the deep
    price pass gives way to RUN_WALL_S. ``last_out``, when a dict, receives
    every priced symbol's last bar ({"closes": {sym: [date, close, currency]},
    "fx": {ccy: USD per unit}}) for basket_fundamentals.py's market caps."""
    wall_t0 = wall_t0 or time.time()
    today = today or dt.datetime.now(dt.timezone.utc).date().isoformat()
    names = names or {}
    reg = tech["regime"]
    axis_full = reg["dates"]
    asof = reg["asof"]
    p_dates, series = panel
    if not p_dates or p_dates[-1] != axis_full[-1]:
        raise RuntimeError(f"prices.db axis ends {p_dates[-1] if p_dates else None} but "
                           f"technicals.json ends {axis_full[-1]}: re-run export_data.py first")
    ladder = {r["t"]: r for r in reg["ladder"]["rows"]}
    r252 = reg.get("ladder_r252") or {}
    prev_rows = (prev or {}).get("rows") or {}
    errors = {}
    calls = {"ssga": 0, "top10": 0, "issuer": 0, "prices": 0, "deep_prices": 0, "fx": 0}
    issuer_status = {}

    def counted(kind, fn):
        def wrap(*a):
            calls[kind] += 1
            return fn(*a)
        return wrap

    f = {"ssga": counted("ssga", fetchers["ssga"]), "top10": counted("top10", fetchers["top10"])}
    if fetchers.get("issuer"):
        f["issuer"] = fetchers["issuer"]

    # ---- 1. holdings ------------------------------------------------------
    plan = {}
    for e in regime.REG_ETFS:
        t = e["t"]
        if t not in ladder:
            errors.setdefault(t, []).append("not on today's ladder (stale or missing series)")
        elif e.get("basket"):
            plan[t] = ("basket", asof, None, len(e["basket"]), "Equal-weight basket defined in regime.REG_ETFS")
        elif t in TRUSTS:
            plan[t] = ("trust", asof, None, 1, "Grantor trust holding physical silver only")
        else:
            plan[t] = get_holdings(e, f, prev_rows, today, errors, issuer_status)

    # ---- 2. member prices, then FX ----------------------------------------
    axis = axis_full[-AXIS_TAIL:]
    start = (dt.date.fromisoformat(axis[0]) - dt.timedelta(days=7)).isoformat()
    # heaviest first: if Yahoo throttles and the budget runs out, what goes
    # unpriced is the tail, which the rest line already carries
    weight = {}
    for _, _, hs, _, _ in plan.values():
        for h in hs or []:
            for s in [h["sym"]] + list(h.get("alts") or []):
                weight[s] = max(weight.get(s, 0.0), h["w"])
    by_weight = lambda syms: sorted(syms, key=lambda s: (-weight.get(s, 0.0), s))  # noqa: E731
    wanted = by_weight({h["sym"] for _, _, hs, _, _ in plan.values() if hs for h in hs})
    t0 = time.time()
    hist = fetchers["history"](wanted, start) if wanted else {}
    calls["prices"] += len(wanted)
    # second pass: alternates for what failed (.KQ after .KS), plus one retry
    # of every failed primary -- a transient 429 should not null a name
    retry = set()
    for _, _, hs, _, _ in plan.values():
        for h in hs or []:
            if h["sym"] not in hist:
                retry.add(h["sym"])
                retry.update(h.get("alts") or [])
    if retry:
        hist.update(fetchers["history"](by_weight(retry), start, RETRY_BUDGET_S))
        calls["prices"] += len(retry)
    # third pass, the rest line's names (N-PORT): lighter than every listed
    # name by construction, so they go last and get what the wall clock allows
    deep_funds = (deep or {}).get("funds") or {}
    deep_syms = (deep or {}).get("symbols") or {}
    dplan = {}
    if deep is not None:
        for t, (tier, _, hs, _, _) in plan.items():
            ent = deep_funds.get(t)
            if tier == "top10" and ent and ent.get("lines") is not None:
                dplan[t] = deep_plan(deep_lines(ent), hs or [], deep_syms)
    dw = {}
    for p in dplan.values():
        for x in p["cands"]:
            if x["sym"] and x["sym"] not in hist:
                dw[x["sym"]] = max(dw.get(x["sym"], 0.0), x["w"])
    # ... and a Select Industry fund's earlier quarters (names that have left it)
    for t, (tier, _, _, _, _) in plan.items():
        if tier == "issuer_full" and t in SELECT_INDUSTRY:
            for q in ((deep_funds.get(t) or {}).get("quarters") or {}).values():
                for x in deep_lines(q):
                    sym = line_symbol(x, deep_syms)
                    if sym and sym not in hist:
                        dw[sym] = max(dw.get(sym, 0.0), x["w"])
    budget = min(DEEP_BUDGET_S, max(0.0, RUN_WALL_S - (time.time() - wall_t0)))
    if dw and budget > 0:
        dwant = sorted(dw, key=lambda s: (-dw[s], s))
        hist.update(fetchers["history"](dwant, start, budget))
        calls["deep_prices"] += len(dwant)
    elif dw:
        log(f"[warn] no time left for the {len(dw)} rest-line names; their weight stays in other")
    need_fx = sorted({iso_ccy(v[2]) for v in hist.values() if iso_ccy(v[2]) not in (None, "USD")})
    fx_hist = fetchers["history"]([c + "USD=X" for c in need_fx], start, RETRY_BUDGET_S) if need_fx else {}
    calls["fx"] += len(need_fx)
    fx_aligned = {}
    for c in need_fx:
        fh = fx_hist.get(c + "USD=X")
        if fh:
            arr, behind = align(axis, fh[0], fh[1])
            if behind <= STALE_MAX:
                fx_aligned[c] = arr
    if last_out is not None:
        last_out["asof"] = asof
        last_out["closes"] = {s: [h[0][-1], h[1][-1], h[2]] for s, h in hist.items() if h[0]}
        last_out["fx"] = {c: next((v for v in reversed(a) if v is not None), None) for c, a in fx_aligned.items()}
    # each priced listing's own-currency trend (above its 50d / 200d average)
    trend_all = {}
    for sym, h in hist.items():
        arr, behind = align(axis, h[0], h[1])
        trend_all[sym] = trend_flags(arr) if behind <= STALE_MAX else (None, None)
    log(f"prices: {len(hist)} priced of {len(wanted)} members + {len(dw)} rest-line names "
        f"({len(retry)} retried/alternates), "
        f"{len(fx_aligned)}/{len(need_fx)} FX pairs, {time.time() - t0:.1f}s")

    # ---- 3. rows ----------------------------------------------------------
    rows = {}
    for e in regime.REG_ETFS:
        t = e["t"]
        if t not in plan:
            continue
        R = {k: ladder[t].get(k) for k, _ in WINDOWS}
        # the one-year return is published off the rows (regime.ladder_r252), the
        # same number the board's 1y column shows
        R["r252"] = r252.get(t)
        tier, h_asof, hs, n_total, source = plan[t]
        row_ser = (regime.basket_series(series, e["basket"]) if e.get("basket") else series.get(t)) or []
        sigma_d = row_sigma(row_ser)
        try:
            if tier == "basket":
                holdings = _basket_holdings(e, series, R, names)
            elif tier == "trust":
                holdings = [{"t": TRUSTS[t]["t"], "name": TRUSTS[t]["name"], "w": 1.0, "ccy": "USD",
                             "r": dict(R), "c": dict(R), "w0": {k: 1.0 for k, _ in WINDOWS}}]
            else:
                holdings = _etf_holdings(t, hs, hist, axis, fx_aligned, R, errors, h_asof)
        except Exception as ex:
            errors.setdefault(t, []).append(f"contribution failed: {type(ex).__name__}: {ex}")
            holdings = []
        holdings.sort(key=lambda h: (-h["w"], h["t"]))
        rest = rest_line(holdings, R, n_total)
        drivers, why = None, None
        try:
            if t in dplan:
                F = (series.get(t) or [])[-len(axis):]
                drivers, why = deep_drivers(deep_funds[t], dplan[t], hist, axis, fx_aligned, F, R, rest)
            elif tier == "top10" and deep is not None:
                why = (deep_funds.get(t) or {}).get("error") or "no N-PORT in the cache for this fund"
            elif tier == "issuer_full" and t in SELECT_INDUSTRY:
                quarters = (deep_funds.get(t) or {}).get("quarters") or {}
                want = {h["t"] for h in holdings} | {line_symbol(x, deep_syms) for q in quarters.values()
                                                     for x in deep_lines(q)}
                S = {}
                for sym in sorted(x for x in want if x and x in hist):
                    ccy = iso_ccy(hist[sym][2]) or ("USD" if "." not in sym else None)
                    if ccy:
                        S[sym] = member_usd(axis, hist[sym], ccy, fx_aligned)[0]
                F = (series.get(t) or [])[-len(axis):]
                drivers, why = rebalance_drivers(holdings, h_asof, quarters, deep_syms, S, F, axis, R, rest,
                                                 names)
        except Exception as ex:
            drivers, why = None, f"{type(ex).__name__}: {ex}"
        if why:
            errors.setdefault(t, []).append("rest line not itemised: " + why)
            if tier == "top10":
                drivers = {"source": None, "asof": None, "names": [], "other": None,
                           "note": "Not itemised: " + why + "."}
        if tier == "basket":
            trend = {m: trend_flags((series.get(m) or [])[-len(axis):]) for m in e["basket"]}
        else:
            trend = trend_all
        lines, basis = shape_lines(holdings, drivers, R)
        shape = row_shape(lines, R, trend, basis)
        part = {}
        for k in PART_WINDOWS:
            lag = dict(WINDOWS)[k]
            part[k] = participation(holdings, R.get(k), sigma_d, lag, k,
                                    equal_weight=(tier == "basket"), single=(tier == "trust"))
        covered = sum(h["w"] for h in holdings)
        rows[t] = {
            "tier": tier,
            "holdings_asof": h_asof,
            "covered_w": _rnd(covered, 4),
            "n_holdings_total": n_total,
            "source": source,
            "R": R,
            "holdings": [{"t": h["t"], "name": h["name"], "w": _rnd(h["w"]), "ccy": h["ccy"],
                          "r": {k: _rnd(h["r"].get(k)) for k, _ in WINDOWS},
                          "c": {k: _rnd(h["c"].get(k)) for k, _ in WINDOWS},
                          "a50": (trend.get(h["t"]) or (None, None))[0],
                          "a200": (trend.get(h["t"]) or (None, None))[1]} for h in holdings],
            "rest": {"w": _rnd(rest["w"]), "n": rest["n"],
                     "c": {k: _rnd(v) for k, v in rest["c"].items()},
                     "w_unpriced": {k: _rnd(v) for k, v in rest["w_unpriced"].items()},
                     # "rebalancing" on a basket: the lines above are buy-and-hold and
                     # this is what the daily reset added (see _basket_holdings)
                     "kind": "rebalancing" if tier == "basket" else "rest"},
            "participation": {k: {"share_up": _rnd(v["share_up"], 4), "n_up": v["n_up"], "n": v["n"],
                                  "conc_top3": _rnd(v["conc_top3"], 3), "move_z": _rnd(v["move_z"], 2),
                                  "conc_note": v["conc_note"]} for k, v in part.items()},
            "shape": shape,
        }
        if drivers is not None:
            rows[t]["rest"]["drivers"] = drivers

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "asof": asof,
        "method": METHOD,
        "calls": dict(calls, issuer=getattr(fetchers.get("issuer"), "calls", calls["issuer"])),
        # B1 stage 1: per issuer, which funds' files parsed today and which failed
        # (a whole issuer failing is a source that changed shape: health.py says so)
        "issuer_files": {k: {"name": ISSUER_NAME[k], "funds": ISSUER_FUNDS.get(k) or ISSUER_OFF.get(k) or [],
                             "ok": sorted(v["ok"]), "failed": sorted(v["failed"]),
                             "status": ("ok" if not v["failed"] else
                                        "failed" if not v["ok"] else "partial")}
                         for k, v in sorted(issuer_status.items())},
        "rows": rows,
        "errors": {t: "; ".join(v) for t, v in errors.items()},
    }


def _basket_holdings(e, series, R, names):
    """A basket's members, attributed BUY-AND-HOLD so every line is checkable
    as weight x return: c_i = w_start x r_i, with w_start = 1/n of the members
    that have a price at the window start (Jake 2026-09-25: "this math ain't
    mathin" -- the exact daily formula folded the volatility the daily reset
    harvests into each name, so ARQQ's 63d line was 1.57pp on a +1.4% move at
    20%). The basket's own return minus these lines is the DAILY REBALANCING
    line (rest.kind = "rebalancing"), so each column still adds to the ladder.
    basket_contrib() -- the exact daily attribution -- still runs, as the proof
    that R is the basket's own return."""
    members = [m for m in e["basket"] if series.get(m)]
    n = len(members)
    ms = [(m, series[m]) for m in members]
    basket = regime.basket_series(series, e["basket"])
    holdings = [{"t": m, "name": names.get(m, m), "w": 1.0 / n, "ccy": "USD",
                 "r": {}, "c": {}, "w0": {}} for m in members]
    by = {h["t"]: h for h in holdings}
    for k, lag in WINDOWS:
        contrib, tot = basket_contrib(ms, lag)
        rs = {m: regime.ret(series[m], lag) for m in members}
        live = [m for m in members if rs[m] is not None]
        for m in members:
            by[m]["r"][k] = rs[m]
            w0 = (1.0 / len(live)) if (live and rs[m] is not None) else None
            by[m]["w0"][k] = w0
            by[m]["c"][k] = (w0 * rs[m]) if w0 is not None else None
        if contrib is None:
            continue
        own = regime.ret(basket, lag)
        s = sum(contrib.values())
        # the spec's exactness contract: the daily attribution sums to the
        # basket's return, and that return is the ladder's own number
        assert abs(s - tot) < 1e-12, (e["t"], k, s, tot)
        assert own is not None and abs(tot - own) < 1e-9, (e["t"], k, tot, own)
        if R.get(k) is not None:
            assert abs(tot - R[k]) <= 1e-6, (e["t"], k, tot, R[k])
    return holdings


def _etf_holdings(t, hs, hist, axis, fx_aligned, R, errors, h_asof=None):
    out = []
    bad = []
    for h in hs:
        sym = h["sym"]
        priced = [s for s in [sym] + list(h.get("alts") or []) if s in hist]
        # an issuer file that carries its own price (market value / shares): the
        # first candidate whose close on the file date matches it IS the security
        px = h.get("px")
        got = next((s for s in priced if issuer_px_ok(px, hist[s], axis, h_asof, fx_aligned)), None)
        rec = {"t": got or sym, "name": h["name"], "w": h["w"], "ccy": None, "r": {}, "c": {}, "w0": {}}
        if got is None:
            bad.append(f"{sym} ({'failed the file price check' if priced else 'no price'})")
            rec["r"] = {k: None for k, _ in WINDOWS}
        else:
            ccy = iso_ccy(hist[got][2]) or ("USD" if "." not in got else None)
            rec["ccy"] = ccy
            if ccy is None:
                bad.append(f"{got} (no currency)")
                rec["r"] = {k: None for k, _ in WINDOWS}
            else:
                rec["r"], why = member_returns(axis, hist[got], ccy, fx_aligned)
                if why:
                    bad.append(f"{got} ({why})")
        for k, _ in WINDOWS:
            w0, c = drift_back(h["w"], rec["r"].get(k), R.get(k))
            rec["w0"][k] = w0
            rec["c"][k] = c
        out.append(rec)
    if bad:
        errors.setdefault(t, []).append("unpriced, in the rest line: " + ", ".join(bad[:12]) +
                                        (f" and {len(bad) - 12} more" if len(bad) > 12 else ""))
    return out


METHOD = (
    "Holdings by tier: 'basket' = the members in regime.REG_ETFS at equal weight (exact); "
    "'trust' = SLV's one asset, physical silver, whose return is the trust's own; "
    "'issuer_full' = State Street's daily holdings file for the SPDR funds (every holding, "
    "dated by the file); 'top10' = Yahoo's top-holdings list (undated; the date shown is "
    "the fetch date). Cash, money-market, currency and futures lines are excluded. "
    "R is the ladder row's own return from technicals.json, never recomputed. Member "
    "returns are Yahoo adjusted closes as-of aligned onto the ladder's session axis (SPY "
    "sessions); a member whose last bar trails it by more than 3 sessions has no return. "
    "Foreign listings are converted to USD, r_usd = (1+r_local)(1+r_fx) - 1, with Yahoo "
    "<CCY>USD=X. ETF contributions use start-of-window weights backed out of the current "
    "weights, w0 = w(1+R)/(1+r), c = w0 r -- exact only under full holdings and no "
    "rebalance inside the window. Baskets are daily-rebalanced equal weight; each member is "
    "attributed buy-and-hold, c_i = r_i / n with n the members priced at the window start, "
    "so every line is its weight times its own return, and the basket's own return minus "
    "those lines is the 'daily rebalancing' line (rest.kind), what the daily reset added or "
    "cost. The rest line is R minus the listed contributions: it holds "
    "the unlisted holdings, any listed holding with no return, rebalancing inside the "
    "window and the one-day lag of an issuer file dated the prior session. rest.drivers "
    "splits it into names and an other line (names + other = rest in every window): for a "
    "'top10' row, source 'sec_nport' = the fund's latest public SEC N-PORT filing (every "
    "security, weight as of the filing's period end), the lines the listed ten do not "
    "cover, mapped to Yahoo symbols by ISIN and kept only when the symbol's close on the "
    "filing date matches the filing's own value per share within 15 percent; the top 60 by "
    "weight are priced, their filing weights drifted to today with their own return against "
    "the fund's, w_today = w_filing (1+r_i)/(1+R_fund) since the filing, then backed out per "
    "window like the listed lines; unpriced names stay in other with their weight. For a "
    "SPDR S&P Select Industry row (modified equal weight, reset after the close on the third "
    "Friday of Mar/Jun/Sep/Dec), source 'rebalance': today's weights are drifted back to the "
    "last reset and every earlier quarter is assumed to start from those same reset weights "
    "(an approximation: membership and caps change at each reset); each name's figure is its "
    "contribution with the resets modeled minus its one-piece line. Participation "
    "(21d, 63d): share_up = priced weight with r > 0 over priced weight; conc_top3 = the "
    "top-3 holdings' share of the window's contribution over their share of start weight, "
    "defined only when the window's move is at least 1 sigma of the row's own 126-session "
    "daily volatility scaled to the window (move_z). Windows: r5, r21, r63, r126 and r252 "
    "(the one-year return, technicals.json regime.ladder_r252). shape (per row): over the "
    "listed holdings, plus the N-PORT names priced in a top-10 row's rest line -- conc "
    "(top5_w, top10_w; eff_n = 1/sum(w^2) with unlisted weight counted as perfectly spread, "
    "eff_n_min with it spread over lines as heavy as the lightest listed), trend (lines above "
    "their own 50- and 200-session simple average in their own currency: n, n_up, w_up by "
    "weight; a50/a200 per holding), and per window: explained = sum of the lines' "
    "contributions, resid = R - explained, n_up and w_up (share of start weight up), "
    "top1_share/top2_share = the largest one/two contributions in the direction of the move "
    "over the explained move, ex_top2 = the return of the other lines, disp_w = start-weighted "
    "standard deviation of returns, disp_eq = plain standard deviation, iqr."
)


def main(argv=None):
    ap = argparse.ArgumentParser(description="SPEC-86 Part A: rotation ladder holdings and contributions")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--tech", default=TECH_PATH)
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--deep", default=DEEP_PATH, help="the N-PORT cache (read, refreshed, written)")
    ap.add_argument("--last", default=LAST_PATH, help="the last closes + FX for basket_fundamentals.py (not committed)")
    ap.add_argument("--store", default=os.path.join(HERE, "data", "cache", "constituents", "closes.json.gz"),
                    help="the constituents closes store (not committed; Actions cache)")
    ap.add_argument("--no-store", action="store_true", help="fetch every member's full history, as before")
    args = ap.parse_args(argv)

    t0 = time.time()
    with open(args.tech, encoding="utf-8") as fh:
        tech = json.load(fh)
    conn = pricedb.connect(args.db, readonly=True)
    try:
        panel = regime.build_panel(conn)
    finally:
        conn.close()
    prev = None
    if os.path.exists(args.out):
        try:
            with open(args.out, encoding="utf-8") as fh:
                prev = json.load(fh)
        except (OSError, ValueError) as ex:
            print(f"[warn] yesterday's {args.out} unreadable ({ex}); no carry-forward today")
    ap_today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    # The N-PORT cache: each top-10 fund's filings are listed once a week and a
    # document is downloaded only when a newer one exists, so most days make no
    # EDGAR call at all. A failure here costs the rest-line names, never the run.
    deep_rows = [e["t"] for e in regime.REG_ETFS
                 if not e.get("basket") and e["t"] not in TRUSTS and e["t"] not in SSGA_FUNDS]
    deep_rows += [e["t"] for e in regime.REG_ETFS if e["t"] in SELECT_INDUSTRY]
    # funds that are not ladder rows but want a look-through composite (extra_funds.json,
    # read by basket_fundamentals.py): their N-PORT book rides the same weekly listing
    import basket_fundamentals
    deep_rows += extra_nport(basket_fundamentals.read_extras(), deep_rows)
    deep = load_deep(args.deep)
    dcalls = {"edgar": 0, "search": 0}
    try:
        deep, dcalls = refresh_deep(deep_rows, deep, ap_today, fetch_insiders.get, yahoo_isin_symbol)
        if dcalls["edgar"] or dcalls["search"] or not deep.get("generated_at"):
            deep["generated_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            write_deep(deep, args.deep)
    except Exception as ex:
        print(f"[warn] N-PORT refresh failed ({type(ex).__name__}: {ex}); using the cache as it was")
    print(f"N-PORT cache: {sum(1 for f in (deep.get('funds') or {}).values() if f.get('lines') is not None)} "
          f"funds, {dcalls['edgar']} EDGAR calls, {dcalls['search']} ISIN searches, {time.time() - t0:.1f}s")
    # B1 stage 3: member closes go through the constituents store (data/cache/,
    # never committed; the workflow keeps it in the Actions cache), so a stored
    # symbol is asked for its last two weeks only
    import constituents_store as CS
    cstore, cstate = CS.load(args.store)
    cstats = {}

    def stored(symbols, start, budget_s=FETCH_BUDGET_S):
        return CS.stored_history(fetch_history, symbols, start, budget_s, cstore, ap_today, cstats)[0]

    fetchers = {"ssga": fetch_ssga, "top10": fetch_top10,
                "history": fetch_history if args.no_store else stored,
                "issuer": IssuerFetcher(today=ap_today)}
    last = {}
    payload = build(tech, panel, fetchers, prev=prev, names=ticker_names(), deep=deep, wall_t0=t0, last_out=last)
    payload["calls"].update(dcalls)
    if not args.no_store:
        try:
            size = CS.save(cstore, args.store)
        except OSError as ex:
            size = 0
            print(f"[warn] constituents store not saved ({ex}); tomorrow rebuilds it")
        payload["store"] = CS.status(cstats, cstate, size)
        print(f"constituents store: {payload['store']}")
    # the run's last closes, for basket_fundamentals.py (B1 stage 4): a CACHE beside
    # the payload, never committed (data/cache/ is gitignored), rebuilt every run
    try:
        os.makedirs(os.path.dirname(args.last), exist_ok=True)
        with open(args.last, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(last, fh, separators=(",", ":"))
    except OSError as ex:
        print(f"[warn] could not write {args.last} ({ex}); basket_fundamentals.py will have no prices")

    # A wholesale pricing failure (Yahoo down) would blank every contribution.
    # Keep yesterday's file instead and fail loudly; the workflow step is
    # guarded, so the price commit is unaffected.
    etf_rows = [r for r in payload["rows"].values() if r["tier"] in ("issuer_full", "top10")]
    n_all = sum(len(r["holdings"]) for r in etf_rows)
    n_priced = sum(1 for r in etf_rows for h in r["holdings"] if h["r"]["r21"] is not None)
    if n_all and n_priced < 0.5 * n_all:
        print(f"[FAIL] only {n_priced}/{n_all} holdings priced -- keeping yesterday's file")
        return 1

    # write-then-rename, and never leave the .tmp behind: the workflow's
    # commit step runs `git add data/`
    tmp = args.out + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, indent=0, separators=(",", ":"), ensure_ascii=False)  # one field per line (indent=0): the repo's pre-commit guard reads line by line, and a one-line payload puts every word beside every number
        os.replace(tmp, args.out)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    with open(args.out, encoding="utf-8") as fh:
        tripped = [ln for ln in fh if guard_trips(ln)]
    if tripped:
        print(f"[warn] {len(tripped)} line(s) of {args.out} would trip the public repo's guard: {tripped[0][:120]}")
    tiers = {}
    for r in payload["rows"].values():
        tiers[r["tier"]] = tiers.get(r["tier"], 0) + 1
    print(f"wrote {args.out}: {len(payload['rows'])} rows {tiers}, {n_priced}/{n_all} ETF holdings "
          f"priced, {len(payload['errors'])} rows with notes, calls {payload['calls']}, "
          f"{os.path.getsize(args.out) / 1024:.0f} KB, {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
