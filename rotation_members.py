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
* A basket is daily-rebalanced equal weight, so
  ``c_i = sum_t (1/n_t) * r_it * G_{t-1}`` with G the basket's growth to the
  prior session and n_t the members printing that day (regime.basket_series'
  rule). This sums to the basket's return exactly; build() asserts it.
* A foreign listing's local return is converted,
  ``r_usd = (1+r_loc)(1+r_fx) - 1``, with ``<CCY>USD=X`` from Yahoo.
* Every series is as-of aligned onto the ladder's own session axis (SPY's
  sessions, technicals.json regime.dates). A member whose last bar trails that
  axis by more than STALE_MAX sessions gets ``r: null`` everywhere.

Honesty rules (spec section 2): a number that cannot be sourced is null with a
reason in ``errors``; nothing is estimated. No position facts: these are the
published holdings of public funds.

Run:  python rotation_members.py      (network: 26 SSGA files, ~48 Yahoo
                                       top-holdings calls, ~1,400 paced Yahoo
                                       price requests + FX; ~2.5 minutes)
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
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor

import regime

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(HERE, "data", "rotation_members.json")
TECH_PATH = os.path.join(HERE, "data", "technicals.json")
DB_PATH = os.path.join(HERE, "prices.db")
TICKERS_PATH = os.path.join(HERE, "tickers.txt")

WINDOWS = (("r5", 5), ("r21", 21), ("r63", 63), ("r126", 126))
PART_WINDOWS = ("r21", "r63")     # spec 3.3: participation on 21d and 63d
STALE_MAX = 3                     # sessions a member's last bar may trail the axis
SIGMA_WIN = 126                   # the row's own daily-vol window (spec 4 uses 126d)
CONC_TOP = 3
AXIS_TAIL = 160                   # sessions of member history fetched (r126 + slack)
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
UA = {"User-Agent": "Mozilla/5.0 (jr-dash daily-prices rotation_members)"}

TRUSTS = {"SLV": {"t": "XAG", "name": "Physical silver bullion (the trust's only asset)"}}

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


def get_holdings(e, fetchers, prev_rows, today, errors):
    """(tier, holdings_asof, holdings[{sym,alts,name,w}], n_total, source) for
    one non-basket row. A failed fetch carries yesterday's holdings (old date
    kept) and notes it; with no yesterday either, an SSGA row falls back to
    Yahoo's top 10."""
    t = e["t"]
    prev = prev_rows.get(t)

    def carried(why):
        errors.setdefault(t, []).append(f"{why}; carried holdings from {prev.get('holdings_asof')}")
        hs = [{"sym": h["t"], "alts": [], "name": h.get("name") or "", "w": h["w"]}
              for h in prev.get("holdings") or []]
        return (prev["tier"], prev.get("holdings_asof"), hs, prev.get("n_holdings_total"),
                prev.get("source") or "")

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


def build(tech, panel, fetchers, prev=None, today=None, names=None, log=print):
    """Assemble the rotation_members payload. Pure given its inputs:
    ``tech`` = the technicals.json dict, ``panel`` = regime.build_panel's
    (dates, series) from the same prices.db, ``fetchers`` = {"ssga": tk ->
    bytes, "top10": tk -> [(sym, name, w)], "history": (symbols, start) ->
    {sym: (dates, closes, ccy)}}, ``prev`` = yesterday's payload or None."""
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
    prev_rows = (prev or {}).get("rows") or {}
    errors = {}
    calls = {"ssga": 0, "top10": 0, "prices": 0, "fx": 0}

    def counted(kind, fn):
        def wrap(*a):
            calls[kind] += 1
            return fn(*a)
        return wrap

    f = {"ssga": counted("ssga", fetchers["ssga"]), "top10": counted("top10", fetchers["top10"])}

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
            plan[t] = get_holdings(e, f, prev_rows, today, errors)

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
    log(f"prices: {len(hist)} of {len(wanted)} members priced ({len(retry)} retried/alternates), "
        f"{len(fx_aligned)}/{len(need_fx)} FX pairs, {time.time() - t0:.1f}s")

    # ---- 3. rows ----------------------------------------------------------
    rows = {}
    for e in regime.REG_ETFS:
        t = e["t"]
        if t not in plan:
            continue
        R = {k: ladder[t].get(k) for k, _ in WINDOWS}
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
                holdings = _etf_holdings(t, hs, hist, axis, fx_aligned, R, errors)
        except Exception as ex:
            errors.setdefault(t, []).append(f"contribution failed: {type(ex).__name__}: {ex}")
            holdings = []
        holdings.sort(key=lambda h: (-h["w"], h["t"]))
        rest = rest_line(holdings, R, n_total)
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
                          "c": {k: _rnd(h["c"].get(k)) for k, _ in WINDOWS}} for h in holdings],
            "rest": {"w": _rnd(rest["w"]), "n": rest["n"],
                     "c": {k: _rnd(v) for k, v in rest["c"].items()},
                     "w_unpriced": {k: _rnd(v) for k, v in rest["w_unpriced"].items()}},
            "participation": {k: {"share_up": _rnd(v["share_up"], 4), "n_up": v["n_up"], "n": v["n"],
                                  "conc_top3": _rnd(v["conc_top3"], 3), "move_z": _rnd(v["move_z"], 2),
                                  "conc_note": v["conc_note"]} for k, v in part.items()},
        }

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "asof": asof,
        "method": METHOD,
        "calls": calls,
        "rows": rows,
        "errors": {t: "; ".join(v) for t, v in errors.items()},
    }


def _basket_holdings(e, series, R, names):
    members = [m for m in e["basket"] if series.get(m)]
    n = len(members)
    ms = [(m, series[m]) for m in members]
    basket = regime.basket_series(series, e["basket"])
    holdings = [{"t": m, "name": names.get(m, m), "w": 1.0 / n, "ccy": "USD",
                 "r": {}, "c": {}, "w0": {}} for m in members]
    by = {h["t"]: h for h in holdings}
    for k, lag in WINDOWS:
        contrib, tot = basket_contrib(ms, lag)
        for m in members:
            by[m]["r"][k] = regime.ret(series[m], lag)
            by[m]["c"][k] = contrib[m] if contrib is not None else None
            by[m]["w0"][k] = 1.0 / n
        if contrib is None:
            continue
        own = regime.ret(basket, lag)
        s = sum(contrib.values())
        # the spec's exactness contract: the column sums to the basket's return
        assert abs(s - tot) < 1e-12, (e["t"], k, s, tot)
        assert own is not None and abs(tot - own) < 1e-9, (e["t"], k, tot, own)
        if R.get(k) is not None:
            assert abs(tot - R[k]) <= 1e-6, (e["t"], k, tot, R[k])
    return holdings


def _etf_holdings(t, hs, hist, axis, fx_aligned, R, errors):
    out = []
    bad = []
    for h in hs:
        sym = h["sym"]
        got = sym if sym in hist else next((a for a in h.get("alts") or [] if a in hist), None)
        rec = {"t": got or sym, "name": h["name"], "w": h["w"], "ccy": None, "r": {}, "c": {}, "w0": {}}
        if got is None:
            bad.append(f"{sym} (no price)")
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
    "rebalance inside the window. Baskets are daily-rebalanced equal weight, c_i = sum over "
    "sessions of r_i,t G_t-1 / n_t, which sums to the basket's return exactly (prices.db, "
    "the ladder's own series). The rest line is R minus the listed contributions: it holds "
    "the unlisted holdings, any listed holding with no return, rebalancing inside the "
    "window and the one-day lag of an issuer file dated the prior session. Participation "
    "(21d, 63d): share_up = priced weight with r > 0 over priced weight; conc_top3 = the "
    "top-3 holdings' share of the window's contribution over their share of start weight, "
    "defined only when the window's move is at least 1 sigma of the row's own 126-session "
    "daily volatility scaled to the window (move_z)."
)


def main(argv=None):
    ap = argparse.ArgumentParser(description="SPEC-86 Part A: rotation ladder holdings and contributions")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--tech", default=TECH_PATH)
    ap.add_argument("--out", default=OUT_PATH)
    args = ap.parse_args(argv)

    t0 = time.time()
    with open(args.tech, encoding="utf-8") as fh:
        tech = json.load(fh)
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
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
    fetchers = {"ssga": fetch_ssga, "top10": fetch_top10, "history": fetch_history}
    payload = build(tech, panel, fetchers, prev=prev, names=ticker_names())

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
    tiers = {}
    for r in payload["rows"].values():
        tiers[r["tier"]] = tiers.get(r["tier"], 0) + 1
    print(f"wrote {args.out}: {len(payload['rows'])} rows {tiers}, {n_priced}/{n_all} ETF holdings "
          f"priced, {len(payload['errors'])} rows with notes, calls {payload['calls']}, "
          f"{os.path.getsize(args.out) / 1024:.0f} KB, {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
