#!/usr/bin/env python3
"""
Point-in-time history of the rotation ladder's vehicle composites (B1 stage 5,
2026-11-20; design: board-refactor-2026-11 recon-3 section 5).

A ONE-OFF, RESUMABLE BACK-FILL, dispatched by hand in GitHub Actions
(.github/workflows/basket-history.yml, workflow_dispatch only -- never on a
schedule). It never runs from a laptop: every SEC request goes through the
repo's own paced clients (fetch_insiders.get for EDGAR filings, sec_facts.py
for companyfacts) on the Actions runner. Each dispatch works inside a request
budget and a wall-clock budget, commits what it finished, and the next
dispatch carries on from there.

THREE PHASES, in order; a phase starts only when the one before is complete
---------------------------------------------------------------------------
1 books      For each row, the fund's quarter-end NPORT-P books from 2019-Q3 on
             (EDGAR's filing list for the fund series, then each filing's
             primary_doc.xml, parsed by rotation_members.parse_nport): every
             line's weight (% of net assets) and its own price on the period
             date (value / balance). One book per calendar quarter (the latest
             period inside it). SPY is a unit investment trust and files no
             N-PORT: its book is IVV's (the same index), labelled. A basket's
             book is today's members at equal weight (membership hindsight,
             labelled), priced from prices.db on each calendar quarter end.
2 companies  Each company in any book (by CIK), its companyfacts fetched ONCE,
             and one VINTAGE built per book date it appears on: the board's own
             parser (fundamentals.build_company) run on the facts FILED ON OR
             BEFORE that date only. So a quarter's numbers are what a reader
             could have known that day: a later restatement, a 10-Q filed after
             the date, or a later share count cannot reach it. A declared split
             (fundamentals.DECLARED_SPLITS) is never applied to a vintage dated
             before the split.
3 composite  For each row and book date, basket_fundamentals.composite() over
             the covered companies: P/E (harmonic, losses in), EV/sales,
             EV/EBIT, EV/EBITDA, FCF yield, P/B, revenue growth, operating
             margins, net debt / EBITDA -- shown only at >= 70% coverage, as
             today. Market value = the line's own price on the date x the
             vintage's share count. Statements in a currency other than the
             dollar are uncovered (no historical FX here).

Output: data/rotation_fundamentals_history.json, per row the quarterly series
and its coverage. basket_fundamentals.py (nightly) reads it and adds each
row's `history` block -- today's composite as a percentile of the row's own
quarters and, as a ratio to SPY, of the same ratio's quarters -- to
data/rotation_fundamentals.json. Inputs kept for the next dispatch (committed,
gzip): data/basket_history/nport_books.json.gz and
data/basket_history/company_vintages.json.gz.

KNOWN BIASES (printed in the payload): companies that have since lost their
ticker are not in SEC's current ticker map, so early books cover fewer lines
(each quarter's coverage is printed and the 70% rule applies per quarter); a
basket uses today's members.

Run:   python basket_history.py --budget-min 50 --max-requests 2500 --rows shown
Test:  python test_basket_history.py   (offline, fixtures only)
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
STATE_DIR = os.path.join(DATA, "basket_history")
BOOKS_PATH = os.path.join(STATE_DIR, "nport_books.json.gz")
VINT_PATH = os.path.join(STATE_DIR, "company_vintages.json.gz")
OUT_PATH = os.path.join(DATA, "rotation_fundamentals_history.json")
FUND_PATH = os.path.join(DATA, "rotation_fundamentals.json")
MEMBERS_PATH = os.path.join(DATA, "rotation_members.json")
DEEP_PATH = os.path.join(DATA, "rotation_deep_holdings.json")
CIK_PATH = os.path.join(DATA, "cik_map.json")
DB_PATH = os.path.join(HERE, "prices.db")

START = "2019-07-01"            # the first N-PORT books are for 2019-Q3
BOOK_PROXY = {"SPY": "IVV"}     # a unit investment trust files no N-PORT; IVV tracks the same index
LIST_URL = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={sid}"
            "&type=NPORT-P&dateb=&owner=include&count=100&start={start}&output=atom")
LIST_PAGES = 3
VINT_FIELDS = ("end", "known", "rev", "oi", "ni", "ebitda", "fcf", "capex", "cash", "debt", "equity",
               "rev_p", "oi_p", "ni_p", "sh", "kind", "ccy", "adr")
MEASURES = ("pe", "earn_yield", "ev_sales", "ev_ebit", "ev_ebitda", "fcf_yield", "pb",
            "rev_growth", "op_margin", "op_margin_prev", "nd_ebitda", "loss_w")
MIN_QUARTERS = 16               # a percentile needs at least this many quarters (recon-3 rule V)
SPY_MATCH_DAYS = 46             # a row's quarter is compared with SPY's book within this many days


def _days(a, b):
    return (dt.date.fromisoformat(b[:10]) - dt.date.fromisoformat(a[:10])).days


def quarter_of(d):
    return f"{d[:4]}Q{(int(d[5:7]) - 1) // 3 + 1}"


def quarter_ends(start=START, today=None):
    today = today or dt.date.today().isoformat()
    out = []
    y, q = int(start[:4]), (int(start[5:7]) - 1) // 3 + 1
    while True:
        m = q * 3
        d = (dt.date(y + (m == 12), (m % 12) + 1, 1) - dt.timedelta(days=1)).isoformat()
        if d >= today:
            return out
        if d >= start:
            out.append(d)
        q += 1
        if q == 5:
            y, q = y + 1, 1


def gz_load(path, default):
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else default
    except (OSError, ValueError, EOFError):
        return default


def gz_save(obj, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(obj, fh, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, path)
    return os.path.getsize(path)


class Budget:
    """Requests and wall clock for one dispatch. `ok()` False = stop and commit."""

    def __init__(self, max_requests, minutes, clock=time.time):
        self.max, self.deadline, self.clock, self.used = max_requests, clock() + minutes * 60.0, clock, 0

    def ok(self):
        return self.used < self.max and self.clock() < self.deadline

    def spend(self, n=1):
        self.used += n


# ==========================================================================
# point in time: one company as known on a date
# ==========================================================================

def facts_known_on(cf, asof):
    """companyfacts with every fact FILED AFTER ``asof`` removed -- what a reader
    had on that date. Unfiled (no date) facts are dropped too."""
    out = {"entityName": cf.get("entityName"), "facts": {}}
    for tax, cs in (cf.get("facts") or {}).items():
        o = {}
        for name, c in cs.items():
            u = {}
            for unit, arr in (c.get("units") or {}).items():
                keep = [x for x in arr if x.get("filed") and x["filed"] <= asof]
                if keep:
                    u[unit] = keep
            if u:
                o[name] = {"units": u}
        out["facts"][tax] = o
    return out


def vintage(cf, tk, asof):
    """The compact record (VINT_FIELDS) of a company as known on ``asof``, or
    None. Built by fundamentals.build_company on facts_known_on(cf, asof); a
    declared split effective after ``asof`` is taken back out (the line's price
    on that date is pre-split)."""
    import basket_fundamentals as B
    import fundamentals as F
    rec = F.build_company(facts_known_on(cf, asof), tk)
    if not rec.get("quarters"):
        return None
    F._round(rec)
    s = B.summarise(rec, None, t=tk, today=asof)
    if s.get("status") not in ("ok", "partial") or not s.get("end") or not s.get("sh"):
        return None
    for tag in s.get("decl") or []:
        eff, ratio = tag.split("x")
        if eff > asof:
            s["sh"] = s["sh"] / float(ratio)
    return [s.get(f) for f in VINT_FIELDS]


# ==========================================================================
# phase 1: books
# ==========================================================================

def pick_quarterly(books):
    """{period: book} -> the latest period inside each calendar quarter."""
    best = {}
    for p in sorted(books):
        best[quarter_of(p)] = p
    return {p: books[p] for p in best.values()}


def fetch_books(fund, sid, state, get, budget, log=print):
    """Bring one fund's N-PORT books up to date in ``state`` (resumable: every
    accession done is recorded, and a fund whose listing was finished is not
    listed again). Returns True when the fund is complete."""
    import rotation_members as RM
    f = state.setdefault(fund, {"sid": sid, "done": [], "books": {}, "complete": False})
    if f.get("complete"):
        return True
    if "listing" not in f:
        recs, start = [], 0
        for _ in range(LIST_PAGES):
            if not budget.ok():
                return False
            budget.spend()
            page = RM.nport_recent(get(LIST_URL.format(sid=sid, start=start)), n=1000)
            recs += page
            if len(page) < 100 or min((r["filed"] for r in page), default=START) < START:
                break
            start += 100
        f["listing"] = sorted({r["acc"]: r for r in recs if r["filed"] >= START[:8] + "01"}.values(),
                              key=lambda r: r["filed"])
    done = set(f["done"])
    for r in f["listing"]:
        if r["acc"] in done:
            continue
        if not budget.ok():
            return False
        budget.spend()
        try:
            doc = RM.parse_nport(get(RM.NPORT_DOC_URL.format(cik=r["cik"], acc=r["acc"].replace("-", ""))),
                                 keep=None)
        except Exception as ex:                     # one bad filing never stops the fund
            log(f"[warn] {fund} {r['acc']}: {type(ex).__name__}")
            f["done"].append(r["acc"])
            continue
        if doc.get("series") and doc["series"] != sid:
            log(f"[warn] {fund} {r['acc']}: filing is for {doc['series']}, not {sid}")
        elif doc.get("period") and doc["period"] >= START:
            f["books"][doc["period"]] = {"filed": r["filed"], "acc": r["acc"],
                                         "lines": [[x.get("isin"), x.get("ticker"), round(x["w"], 8),
                                                    None if x.get("px") is None else round(x["px"], 6)]
                                                   for x in doc["lines"]]}
        f["done"].append(r["acc"])
    f["books"] = pick_quarterly(f["books"])
    f["complete"] = True
    return True


# ==========================================================================
# mapping a book line to a company
# ==========================================================================

def line_cik(isin, ticker, cmap, isin_sym):
    """The CIK of a book line: its ticker in SEC's current ticker map, else its
    ISIN's mapped symbol (rotation_deep_holdings' ISIN map). None otherwise (a
    company that has since lost its ticker: uncovered, counted)."""
    for s in (ticker, (isin_sym.get(isin) or [None])[0] if isin else None):
        if not s or "." in s:
            continue
        s = s.strip().upper().replace(".", "-").replace("/", "-")
        c = cmap.get(s)
        if c:
            return c, s
    return None, None


def basket_book(members, date, db_close):
    """Today's members at equal weight, priced from prices.db on ``date``. The
    fifth field marks the price as prices.db's: split-adjusted to TODAY's basis,
    so composite_on() puts the share count on that basis too."""
    lines = []
    for m in members:
        px = db_close(m, date)
        if px:
            lines.append([None, m, 1.0, px, "db"])
    n = len(lines)
    return [[a, b, 1.0 / n, d, e] for a, b, _, d, e in lines] if n else []


def later_split_factor(splits, date):
    """The product of the splits (detected or declared, [[after, ratio]]) that
    took effect after ``date``: what turns that date's share count into today's
    basis, the basis of prices.db's split-adjusted closes."""
    f = 1.0
    for after, ratio in splits or []:
        if after > date:
            f *= ratio
    return f


# ==========================================================================
# phase 3: the composite on one date
# ==========================================================================

def composite_on(lines, date, cmap, isin_sym, vint):
    """(measures or None, coverage, gaps) for one book on one date. ``vint`` =
    {cik: {"v": {date: VINT_FIELDS row}}}. Only a vintage AT ``date`` is used --
    built from what was filed by then."""
    import basket_fundamentals as B
    total = sum(x[2] for x in lines if x[2] and x[2] > 0)
    by_cik, gaps = {}, {}

    def gap(k, w):
        gaps[k] = gaps.get(k, 0.0) + w

    for ln in lines:
        isin, tk, w, px = ln[:4]
        rebased = len(ln) > 4 and ln[4] == "db"
        if not w or w <= 0:
            continue
        cik, sym = line_cik(isin, tk, cmap, isin_sym)
        if not cik:
            gap("no current SEC ticker (foreign, or since delisted)", w)
            continue
        if cik in by_cik:
            by_cik[cik]["w"] += w
            continue
        row = ((vint.get(cik) or {}).get("v") or {}).get(date)
        if not row:
            gap("no SEC figures known on the date", w)
            continue
        c = dict(zip(VINT_FIELDS, row))
        if (c.get("ccy") or "USD") != "USD":
            gap("reports in another currency (no historical FX)", w)
            continue
        if _days(c["end"], date) > B.STALE_DAYS:
            gap("latest SEC figures older than 200 days", w)
            continue
        if not px or not c.get("sh"):
            gap("no price or share count", w)
            continue
        sh = c["sh"] * (later_split_factor((vint.get(cik) or {}).get("splits"), date) if rebased else 1.0)
        mcap = px * sh / (c.get("adr") or 1.0)
        doubt = B.mcap_doubt(mcap, c)        # USD only here (other currencies are gapped above)
        if doubt:
            gap(doubt, w)
            continue
        by_cik[cik] = dict(c, cik=cik, t=sym, w=w, mcap=mcap, kind=c.get("kind") or "operating",
                           div=None, bb=None, intexp=None, sh_p=None)
    cov = sum(c["w"] for c in by_cik.values())
    share = (cov / total) if total else 0.0
    if not total or share < B.COVER_MIN:
        return None, share, gaps
    m = B.composite(list(by_cik.values()), cov)
    return {k: m.get(k) for k in MEASURES}, share, gaps


def percentile(today, values):
    """Share of ``values`` at or below ``today`` (0..1), or None."""
    vs = [v for v in values if v is not None]
    if today is None or len(vs) < MIN_QUARTERS:
        return None
    return sum(1 for v in vs if v <= today) / len(vs)


def history_block(t, today_m, hist, spy_hist, spy_today):
    """The `history` block basket_fundamentals adds to a row: today's measures as
    percentiles of the row's own quarters, and as a ratio to SPY, of the same
    ratio's quarters. A percentile needs MIN_QUARTERS quarters with a value."""
    series = [q for q in (hist or {}).get("q") or [] if q.get("m")]
    out = {"n": len(series), "from": series[0]["date"] if series else None,
           "to": series[-1]["date"] if series else None, "book": (hist or {}).get("book"),
           "min_quarters": MIN_QUARTERS, "pct": {}, "pct_vs_spy": {}}
    if not today_m:
        out["why"] = "no composite today"
        return out
    spy_q = {q["date"]: q["m"] for q in (spy_hist or {}).get("q") or [] if q.get("m")}
    for k in ("pe", "ev_sales", "ev_ebit", "fcf_yield", "pb"):
        out["pct"][k] = _r(percentile(today_m.get(k), [q["m"].get(k) for q in series]))
        ratios = []
        for q in series:
            near = [d for d in spy_q if abs(_days(d, q["date"])) <= SPY_MATCH_DAYS]
            if near:
                s = spy_q[min(near, key=lambda d: abs(_days(d, q["date"])))].get(k)
                v = q["m"].get(k)
                if s and v is not None:
                    ratios.append(v / s)
        now = (today_m.get(k) / spy_today[k]) if (spy_today and spy_today.get(k) and today_m.get(k) is not None) else None
        out["pct_vs_spy"][k] = _r(percentile(now, ratios)) if t != "SPY" else None
    if len(series) < MIN_QUARTERS:
        out["why"] = f"{len(series)} quarters with a composite; a percentile needs {MIN_QUARTERS}"
    return out


def _r(v, d=4):
    return None if v is None else round(v, d)


# ==========================================================================
# the run
# ==========================================================================

def targets(mode, fund_payload, members):
    rows = (members or {}).get("rows") or {}
    if mode == "shown":
        ts = [t for t, r in ((fund_payload or {}).get("rows") or {}).items() if r.get("shown")]
    elif mode == "all":
        ts = [t for t, r in rows.items() if r.get("tier") != "trust"]
    else:
        ts = [x.strip().upper() for x in mode.split(",") if x.strip()]
    if "SPY" not in ts:
        ts.append("SPY")
    return sorted(set(ts))


def run(rows_in, members, deep, cmap, books, vints, get, companyfacts, db_close, budget, today,
        series_of, log=print):
    """One dispatch. Mutates ``books`` and ``vints``; returns (history payload,
    progress dict)."""
    import regime
    mrows = (members or {}).get("rows") or {}
    baskets = {e["t"]: e["basket"] for e in regime.REG_ETFS if e.get("basket")}
    isin_sym = (deep or {}).get("symbols") or {}
    prog = {"rows": len(rows_in), "funds": 0, "funds_complete": 0, "companies": 0, "companies_done": 0,
            "requests": 0, "phase": "books"}
    # ---- phase 1
    funds = sorted({BOOK_PROXY.get(t, t) for t in rows_in if t not in baskets})
    prog["funds"] = len(funds)
    for fund in funds:
        sid = series_of(fund)
        if not sid:
            books.setdefault(fund, {"complete": True, "books": {}, "why": "no N-PORT fund series found"})
        elif not fetch_books(fund, sid, books, get, budget, log):
            break
        prog["funds_complete"] += 1
        f = books.get(fund) or {}
        log(f"[books] {fund}: {len(f.get('books') or {})} quarterly books ({'complete' if f.get('complete') else 'partial'})")
    books_done = prog["funds_complete"] == len(funds)
    grid = quarter_ends(START, today)
    # ---- phase 2 (only once every book is in)
    need = {}
    if books_done:
        prog["phase"] = "companies"
        for t in rows_in:
            if t in baskets:
                for d in grid:
                    for isin, tk, w, px, _ in basket_book(baskets[t], d, db_close):
                        c, s = line_cik(isin, tk, cmap, isin_sym)
                        if c:
                            need.setdefault(c, [s, set(), 0.0])[1].add(d)
                continue
            for d, b in ((books.get(BOOK_PROXY.get(t, t)) or {}).get("books") or {}).items():
                for isin, tk, w, px in b["lines"]:
                    c, s = line_cik(isin, tk, cmap, isin_sym)
                    if c:
                        e = need.setdefault(c, [s, set(), 0.0])
                        e[1].add(d)
                        e[2] = max(e[2], w or 0.0)
        prog["companies"] = len(need)
        order = sorted(need, key=lambda c: (-need[c][2], c))
        for c in order:
            v = vints.setdefault(c, {"t": need[c][0], "v": {}})
            missing = sorted(d for d in need[c][1] if d not in v["v"])
            if not missing or v.get("status") == "na":
                prog["companies_done"] += 1
                continue
            if not budget.ok():
                break
            budget.spend()
            try:
                cf = companyfacts(c)
            except Exception as ex:
                v["status"] = "na" if getattr(ex, "code", None) == 404 else "error"
                v["why"] = type(ex).__name__
                if v["status"] == "na":
                    prog["companies_done"] += 1
                continue
            if cf is None:
                continue
            import fundamentals as F
            full = F.build_company(cf, need[c][0])        # today's view: every split since, for re-based prices
            v["splits"] = [[s["after"], s["ratio"]] for s in full.get("splits") or []]
            for d in missing:
                v["v"][d] = vintage(cf, need[c][0], d)
            v["status"] = "ok"
            prog["companies_done"] += 1
            if prog["companies_done"] % 100 == 0:
                log(f"[companies] {prog['companies_done']}/{prog['companies']}")
    companies_done = books_done and prog["companies_done"] == prog["companies"]
    # ---- phase 3 (always: partial results are labelled)
    out_rows = {}
    for t in rows_in:
        if t in baskets:
            dated = [(d, basket_book(baskets[t], d, db_close), None) for d in grid]
            book = "today's members at equal weight, priced on each quarter end (membership hindsight)"
        else:
            fund = BOOK_PROXY.get(t, t)
            fb = (books.get(fund) or {}).get("books") or {}
            dated = [(d, b["lines"], b.get("filed")) for d, b in sorted(fb.items())]
            book = (f"SEC N-PORT books of {fund}" + (f" (a proxy: {t} files no N-PORT)" if fund != t else ""))
        qs = []
        for d, lines, filed in dated:
            m, cov, gaps = composite_on(lines, d, cmap, isin_sym, vints) if lines else (None, 0.0, {})
            qs.append({"date": d, "public": filed, "cov": _r(cov),
                       "m": {k: (_r(v, 4) if isinstance(v, float) else v) for k, v in m.items()} if m else None,
                       "gap": max(gaps.items(), key=lambda kv: kv[1])[0] if (gaps and not m) else None})
        out_rows[t] = {"book": book, "q": qs, "n": sum(1 for q in qs if q["m"])}
    prog["requests"] = budget.used
    prog["complete"] = books_done and companies_done
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "complete" if prog["complete"] else "partial",
        "progress": prog,
        "method": METHOD,
        "rules": {"start": START, "cover_min": 0.70, "min_quarters": MIN_QUARTERS, "book_proxy": BOOK_PROXY},
        "rows": out_rows,
    }
    return payload, prog


METHOD = (
    "Point in time, quarterly from 2019-Q3. Each row's book on each date: the fund's SEC N-PORT filing for "
    "that quarter (every line's weight and its own price on the period date; SPY's book is IVV's, the same "
    "index, because SPY files no N-PORT), or for a basket today's members at equal weight priced from prices.db "
    "on each quarter end (membership hindsight). Each company's figures as known on the date: fundamentals.py's "
    "parser run on the SEC facts filed on or before it, so later restatements and later filings cannot reach "
    "it. Market value = the line's price on the date x the share count known then. Then the same look-through "
    "composite and 70 pct coverage rule as rotation_fundamentals.json. 'public' is the N-PORT's filing date, "
    "when that quarter's book became public. Companies that have lost their ticker since are uncovered, so "
    "early quarters can cover less; each quarter prints its coverage."
)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Resumable point-in-time back-fill of the vehicle composites")
    ap.add_argument("--budget-min", type=float, default=50.0, help="wall-clock minutes for this dispatch")
    ap.add_argument("--max-requests", type=int, default=2500, help="SEC requests for this dispatch")
    ap.add_argument("--rows", default="shown", help="'shown' (default), 'all', or a comma list; SPY always")
    args = ap.parse_args(argv)
    import fetch_insiders
    import rotation_members as RM
    import sec_facts

    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    members = json.load(open(MEMBERS_PATH, encoding="utf-8"))
    fundp = json.load(open(FUND_PATH, encoding="utf-8")) if os.path.exists(FUND_PATH) else {}
    deep = json.load(open(DEEP_PATH, encoding="utf-8")) if os.path.exists(DEEP_PATH) else {}
    cmap = (json.load(open(CIK_PATH, encoding="utf-8")).get("map")) or {}
    books = gz_load(BOOKS_PATH, {})
    vints = gz_load(VINT_PATH, {})
    budget = Budget(args.max_requests, args.budget_min)
    smap = {}

    def series_of(fund):
        f = ((deep.get("funds") or {}).get(fund) or {})
        if f.get("series") or RM.SERIES_FIX.get(fund):
            return f.get("series") or RM.SERIES_FIX[fund]
        if not smap:
            budget.spend()
            smap.update(RM.series_map(fetch_insiders.get(RM.MF_URL)))
        return smap.get(fund)

    def companyfacts(cik):
        cf = sec_facts.companyfacts(cik, max_age_h=1e9)
        p = os.path.join(sec_facts.CACHE_DIR, "CIK%s.json.gz" % str(cik).zfill(10))
        if os.path.exists(p):
            os.remove(p)            # the runner's disk: the vintages are kept, the raw file is not
        return cf

    import basket_fundamentals as B
    import pricedb
    con = pricedb.connect(DB_PATH, readonly=True)
    dbc = {}

    def db_close(sym, date):
        """The close on or before ``date`` (through pricedb only)."""
        k = (sym, date)
        if k not in dbc:
            r = con.execute("SELECT close FROM daily_prices WHERE ticker=? AND date<=? AND close IS NOT NULL "
                            "ORDER BY date DESC LIMIT 1", (sym, date)).fetchone()
            dbc[k] = r[0] if r else None
        return dbc[k]

    rows_in = targets(args.rows, fundp, members)
    print(f"[start] {len(rows_in)} rows ({args.rows}): {', '.join(rows_in)}; budget {args.max_requests} "
          f"requests / {args.budget_min:.0f} min")
    t0 = time.time()
    payload, prog = run(rows_in, members, deep, cmap, books, vints, fetch_insiders.get, companyfacts, db_close,
                        budget, today, series_of)
    nb = gz_save(books, BOOKS_PATH)
    nv = gz_save(vints, VINT_PATH)
    B.write_payload(payload, OUT_PATH)          # one field per line, refused if the public guard would trip
    n16 = sum(1 for r in payload["rows"].values() if r["n"] >= MIN_QUARTERS)
    print(f"[sizes] books {nb / 1048576:.1f} MB, vintages {nv / 1048576:.1f} MB, history "
          f"{os.path.getsize(OUT_PATH) / 1024:.0f} KB")
    print(f"HISTORY {payload['status'].upper()}: phase {prog['phase']}; funds {prog['funds_complete']}/{prog['funds']} "
          f"complete; companies {prog['companies_done']}/{prog['companies']}; {prog['requests']} SEC requests in "
          f"{time.time() - t0:.0f}s; rows with >= {MIN_QUARTERS} quarters: {n16}/{len(rows_in)}; "
          f"{'DONE - no further dispatch needed' if prog['complete'] else 'RUN AGAIN to continue'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
