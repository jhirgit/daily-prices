#!/usr/bin/env python3
"""
Vehicle composites for the sector rotation ladder: what each row's companies
are worth and what they are doing, from their own SEC filings (B1 stage 4,
2026-11-20; design: board-refactor-2026-11 recon-3 section 4).

For every row of data/rotation_members.json this writes
data/rotation_fundamentals.json: look-through valuation (P/E, EV/sales,
EV/EBIT, EV/EBITDA, free-cash-flow, dividend and buyback yields, P/B) and
aggregate fundamentals (revenue and earnings growth, operating margin now and
a year ago, net debt / EBITDA, the share of weight in net-cash companies,
interest cover, capex intensity, ROIC, share-count change), plus each line's
own P/E and revenue growth for the holdings table. A composite is SHOWN only
when the companies it covers make up at least COVER_MIN of the row's weight; below
that the row says how much of its weight files with the SEC and why the rest
does not.

THE ARITHMETIC (look-through; recon-3 section 4.2)
-------------------------------------------------
A fund with weight w_i in company i owns w_i / mcap_i of that company per 1 of
net assets. Every vehicle measure is an aggregate numerator over an aggregate
denominator of those owned slices:
    P/E          sum(w) / sum(w E / mcap)    the weighted HARMONIC mean; losses
                                             are IN (they lower the earnings
                                             yield); "n/m" when the sum <= 0
    EV/X         sum(w EV / mcap) / sum(w X / mcap)     operating companies only
    yields       sum(w X / mcap) / sum(w)
    growth       sum(w X_t / mcap) / sum(w X_t-4q / mcap) - 1   (today's
                                             ownership held fixed)
    margin       sum(w OI / mcap) / sum(w S / mcap), now and four quarters ago
Companies are aggregated by CIK (two share classes are one company). Banks and
insurers enter P/E, P/B and dividend yield only (fundamentals.py `kind`).
Statements are converted to US dollars at the run's FX (a ratio at today's
price). Market cap = the listing's last close x the latest filed share count
(split-adjusted), divided by the ADR ratio for an ADR.

WHAT COUNTS AS COVERED
----------------------
A line is covered when it is a US listing in SEC's ticker map (data/cik_map.json),
its company has usable SEC financials whose trailing twelve months end no more
than STALE_DAYS ago, it has a last close, and its filing currency has an FX
rate. Everything else is uncovered with its reason, counted by weight. No
estimates: analyst forecasts and revisions have no free source and are not
shown (said on the page).

WHERE THE COMPANY NUMBERS COME FROM
-----------------------------------
data/basket_companies.json (committed): one compact record per CIK -- the
latest quarter's trailing twelve months, the same four quarters earlier, the
share counts, plus dividends paid, buybacks and interest expense -- built by
fundamentals.build_company (the Name tab's own parser) from SEC companyfacts
fetched through sec_facts.py (its paced client and its cache). A company is
fetched again only when its latest quarter is old enough that a new filing is
due (REFETCH_AFTER_DAYS) and it was not checked in the last week, heaviest
weight first, inside SEC_BUDGET_S and SEC_MAX. Until a company has a record of its own,
data/fundamentals.json's (the board's coverage names) stands in for it.

Run:  python basket_fundamentals.py            (network: SEC, paced, budgeted)
      python basket_fundamentals.py --offline  (cached companyfacts only)
Test: python test_basket_fundamentals.py       (offline, fixtures only)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
MEMBERS_PATH = os.path.join(DATA, "rotation_members.json")
DEEP_PATH = os.path.join(DATA, "rotation_deep_holdings.json")
CIK_PATH = os.path.join(DATA, "cik_map.json")
FUND_PATH = os.path.join(DATA, "fundamentals.json")
STORE_PATH = os.path.join(DATA, "basket_companies.json")
LAST_PATH = os.path.join(DATA, "cache", "rotation_last_closes.json")
OUT_PATH = os.path.join(DATA, "rotation_fundamentals.json")
DB_PATH = os.path.join(HERE, "prices.db")
EXTRA_PATH = os.path.join(HERE, "extra_funds.json")   # funds that are not ladder rows (extra_rows)

COVER_MIN = 0.70            # a composite is shown only above this share of the row's weight
STALE_DAYS = 200            # a company whose TTM ends longer ago is uncovered (annual 20-F/40-F filers)
REFETCH_AFTER_DAYS = 130    # a quarter this old means the next one is due: fetch again...
RECHECK_DAYS = 7            # ...at most weekly while waiting for it
ANNUAL_RECHECK_DAYS = 30    # a company already past STALE_DAYS: monthly
ERROR_RETRY_DAYS = 1
NA_RETRY_DAYS = 30
SEC_BUDGET_S = 360.0        # wall clock for companyfacts fetches per run (a cold store fills in ~2 nights)
SEC_MAX = 1200              # and a request cap per run
TAX = 0.21                  # ROIC's tax rate
EXTRA = {                   # TTM flows fundamentals.py does not carry in its quarter rows
    "div": {"us-gaap": ["PaymentsOfDividends", "PaymentsOfDividendsCommonStock"],
            "ifrs-full": ["DividendsPaidClassifiedAsFinancingActivities", "DividendsPaid"]},
    "bb": {"us-gaap": ["PaymentsForRepurchaseOfCommonStock"],
           "ifrs-full": ["PaymentsToAcquireOrRedeemEntitysShares"]},
    "intexp": {"us-gaap": ["InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt"],
               "ifrs-full": ["FinanceCosts", "InterestExpense"]},
}
REC_FIELDS = ("t", "fetched", "status", "why", "kind", "ccy", "adr", "end", "known", "rev", "oi", "ni",
              "ebitda", "fcf", "capex", "cash", "debt", "equity", "rev_p", "oi_p", "ni_p", "end_p",
              "sh", "sh_p", "div", "bb", "intexp", "src", "sh_end", "decl")


def _days(a, b):
    return (dt.date.fromisoformat(b[:10]) - dt.date.fromisoformat(a[:10])).days


def _load(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


# ==========================================================================
# one company: a compact record from fundamentals.build_company's output
# ==========================================================================

def summarise(rec, extras=None, t=None, today=None, src="companyfacts"):
    """The compact record basket composites read, from a build_company-style
    record (quarters in millions of the filing currency, shares in millions):
    the latest quarter's TTM, the row four quarters (350-380 days) earlier,
    the latest filed share count and the one about a year before it."""
    out = {"t": t, "fetched": today, "src": src, "status": rec.get("status") or "na",
           "why": rec.get("why"), "kind": rec.get("kind"), "ccy": rec.get("currency"),
           "adr": rec.get("adr_ratio") or 1.0}
    qs = [q for q in rec.get("quarters") or [] if q.get("rev") is not None or q.get("ni") is not None]
    if not qs or out["status"] == "na":
        out["status"] = "na"
        out["why"] = out["why"] or "no quarters with revenue or earnings"
        return out
    q = qs[-1]
    p = next((x for x in reversed(qs[:-1]) if 350 <= _days(x["end"], q["end"]) <= 380), None)
    sh = [s for s in rec.get("shares") or [] if s.get("adj")]
    s1 = sh[-1] if sh else None
    s0 = next((s for s in reversed(sh[:-1]) if 330 <= _days(s["end"], s1["end"]) <= 400), None) if s1 else None
    out.update(end=q["end"], known=q.get("known"), rev=q.get("rev"), oi=q.get("oi"), ni=q.get("ni"),
               ebitda=q.get("ebitda"), fcf=q.get("fcf"), capex=q.get("capex"), cash=q.get("cash"),
               debt=q.get("debt"), equity=q.get("equity"),
               rev_p=p.get("rev") if p else None, oi_p=p.get("oi") if p else None,
               ni_p=p.get("ni") if p else None, end_p=p["end"] if p else None,
               sh=s1["adj"] if s1 else None, sh_p=s0["adj"] if s0 else None,
               sh_end=s1["end"] if s1 else None, decl=list(rec.get("declared_applied") or []))
    for k in EXTRA:
        out[k] = (extras or {}).get(k)
    if out["sh"] is None:
        out["status"], out["why"] = "partial", "no share count tagged in the filings"
    elif out["status"] not in ("ok", "partial"):
        out["status"] = "ok"
    return out


def extras_from_facts(cf, rec, end):
    """{div, bb, intexp}: TTM dividends paid, buybacks and interest expense in
    millions, at the record's last quarter end, read with fundamentals.py's own
    period logic. None where untagged."""
    import fundamentals as F
    facts = cf.get("facts", {})
    out = {}
    for k, spec in EXTRA.items():
        P = F.flow_periods(facts, rec.get("taxonomy"), spec.get(rec.get("taxonomy"), []), rec.get("currency"))
        Q = F.quarters_from_periods(P)
        v = F.ttm_at(P, Q, end)
        out[k] = round(v[0] / 1e6, 1) if v else None
    return out


def company_from_facts(cf, t, today):
    import fundamentals as F
    rec = F.build_company(cf, t)
    end = (rec.get("quarters") or [{}])[-1].get("end")
    ex = extras_from_facts(cf, rec, end) if end else {}
    return summarise(F._round(rec), ex, t=t, today=today, src="SEC companyfacts")


def company_from_fundamentals(rec, t, today):
    """data/fundamentals.json's record (already in millions) as a stand-in."""
    return summarise(rec, None, t=t, today=today, src="fundamentals.json (no dividends, buybacks or interest)")


def due(c, today):
    """Is a company's record due a fetch? Never fetched, an error a day ago, a
    'no financials' answer a month ago, or a latest quarter old enough that the
    next filing is out (and not checked this week -- monthly once it is an
    annual filer's)."""
    if not c or not c.get("fetched"):
        return True
    age_f = _days(c["fetched"], today)
    st = c.get("status")
    if st == "error":
        return age_f >= ERROR_RETRY_DAYS
    if st == "na":
        return age_f >= NA_RETRY_DAYS
    if not c.get("end"):
        return age_f >= NA_RETRY_DAYS
    age_e = _days(c["end"], today)
    if age_e <= REFETCH_AFTER_DAYS:
        return False
    return age_f >= (RECHECK_DAYS if age_e <= STALE_DAYS else ANNUAL_RECHECK_DAYS)


# ==========================================================================
# a vehicle's lines
# ==========================================================================

def vehicle_lines(row, deep_fund=None, deep_syms=None):
    """([{sym, w}], source text) for one rotation_members row: the issuer file
    or basket members when the row has every line; for a top-10 row the fund's
    whole latest N-PORT book when the cache has it (weights at the filing's
    period end), else just the ten listed names."""
    import rotation_members as RM
    tier = row.get("tier")
    if tier in ("issuer_full", "basket"):
        src = (row.get("source") or tier) + (f", {row.get('holdings_asof')}" if row.get("holdings_asof") else "")
        return [{"sym": h["t"], "w": h["w"]} for h in row.get("holdings") or [] if h.get("w")], src
    if tier == "trust":
        return [], "a trust holding one physical asset: no companies"
    if deep_fund and deep_fund.get("lines"):
        out = []
        for x in RM.deep_lines(deep_fund):
            out.append({"sym": RM.line_symbol(x, deep_syms or {}), "w": x["w"], "name": x.get("name")})
        tail = deep_fund.get("tail") or {}
        if tail.get("w"):
            out.append({"sym": None, "w": tail["w"], "gap": "lines past the stored N-PORT book"})
            return out, f"SEC N-PORT as of {deep_fund.get('period')} (heaviest {len(out) - 1} lines stored)"
        return out, f"SEC N-PORT, every line as of {deep_fund.get('period')}"
    return ([{"sym": h["t"], "w": h["w"]} for h in row.get("holdings") or [] if h.get("w")],
            "Yahoo top-10 list only (no N-PORT book cached)")


def declared_factor(tk, c):
    """The share-basis factor fundamentals.DECLARED_SPLITS (the one table) owes a
    company record: the product of every declared split the record was NOT built
    with (`decl` lists those it was) whose effective date is after the record's
    share count was dated. A record whose count is already post-split, or that
    was built with the split, gets 1."""
    import fundamentals as F
    f = 1.0
    for eff, ratio in F.DECLARED_SPLITS.get(tk, []):
        if f"{eff}x{ratio:g}" in (c.get("decl") or []):
            continue
        if (c.get("sh_end") or c.get("end") or "") < eff:
            f *= ratio
    return f


def is_us_symbol(sym):
    return bool(sym) and "." not in sym and "=" not in sym and not sym.startswith("^")


# ==========================================================================
# the composite
# ==========================================================================

def _r(v, d=4):
    return None if v is None else round(v, d)


def resolve(lines, cik_of, company_of, price_of, fx_of, today):
    """Covered companies (aggregated by CIK) and the uncovered weight by reason.
    Each company: {cik, t, w, mcap (USD millions), and its record converted to
    USD millions}."""
    by_cik, gaps = {}, {}

    def gap(why, w):
        gaps[why] = gaps.get(why, 0.0) + w

    for ln in lines:
        sym, w = ln.get("sym"), ln.get("w") or 0.0
        if w <= 0:
            continue
        if not sym:
            gap(ln.get("gap") or "no listing symbol for the line", w)
            continue
        if not is_us_symbol(sym):
            gap("listed outside the US (no SEC filer under this symbol)", w)
            continue
        cik = cik_of(sym)
        if not cik:
            gap("not an SEC filer (not in SEC's ticker map)", w)
            continue
        if cik in by_cik:
            by_cik[cik]["w"] += w
            continue
        c = company_of(cik, sym)
        if not c or c.get("status") not in ("ok", "partial") or not c.get("end"):
            gap("SEC figures not fetched yet" if not c else
                "no usable SEC financials" if c.get("status") == "na" else "SEC fetch failed, retried next run", w)
            continue
        if _days(c["end"], today) > STALE_DAYS:
            gap(f"latest SEC figures older than {STALE_DAYS} days (annual filer)", w)
            continue
        px = price_of(sym)
        if not px or not c.get("sh"):
            gap("no price or share count", w)
            continue
        fx = fx_of(c.get("ccy") or "USD")
        if not fx:
            gap(f"no FX rate for {c.get('ccy')}", w)
            continue
        f = declared_factor(c.get("t") or sym, c)
        mcap = px * c["sh"] * f / (c.get("adr") or 1.0)
        conv = {k: (None if c.get(k) is None else c[k] * fx) for k in
                ("rev", "oi", "ni", "ebitda", "fcf", "capex", "cash", "debt", "equity",
                 "rev_p", "oi_p", "ni_p", "div", "bb", "intexp")}
        by_cik[cik] = dict(conv, cik=cik, t=sym, w=w, mcap=mcap, kind=c.get("kind") or "operating",
                           end=c["end"], sh=c["sh"] * f, sh_p=(c["sh_p"] * f) if c.get("sh_p") else None)
    return list(by_cik.values()), gaps


def composite(cos, total_w):
    """The measures over the covered companies ``cos`` (resolve()'s). Every
    ratio is aggregate over aggregate (look-through); None when its
    denominator is not positive or nothing carries both parts."""
    ops = [c for c in cos if c["kind"] == "operating"]

    def own(c, k):
        return c["w"] * c[k] / c["mcap"]

    def ratio(num, den, pool, num_fn=None):
        both = [c for c in pool if (num_fn or c.get(num) is not None) and c.get(den) is not None]
        if not both:
            return None
        a = sum((c["w"] * num_fn(c) / c["mcap"]) if num_fn else own(c, num) for c in both)
        b = sum(own(c, den) for c in both)
        return a / b if b > 0 else None

    def yld(k, pool):
        both = [c for c in pool if c.get(k) is not None]
        w = sum(c["w"] for c in both)
        return (sum(own(c, k) for c in both) / w) if w > 0 else None

    m = {}
    e_cos = [c for c in cos if c.get("ni") is not None]
    ws = sum(c["w"] for c in e_cos)
    ey = sum(own(c, "ni") for c in e_cos)
    m["earn_yield"] = (ey / ws) if ws > 0 else None
    m["pe"] = (ws / ey) if ey > 0 else None
    m["pe_nm"] = "aggregate loss" if (ws > 0 and ey <= 0) else None
    pos = [c["mcap"] / c["ni"] for c in e_cos if c["ni"] > 0]
    m["pe_median_profitable"] = statistics.median(pos) if pos else None
    m["loss_w"] = sum(c["w"] for c in e_cos if c["ni"] < 0) / total_w if total_w else None
    m["loss_n"] = sum(1 for c in e_cos if c["ni"] < 0)
    ev = lambda c: c["mcap"] + c["debt"] - c["cash"] if (c.get("debt") is not None and c.get("cash") is not None) else None  # noqa: E731
    ev_pool = [c for c in ops if ev(c) is not None]
    m["ev_sales"] = ratio(None, "rev", ev_pool, ev)
    m["ev_ebit"] = ratio(None, "oi", ev_pool, ev)
    m["ev_ebitda"] = ratio(None, "ebitda", ev_pool, ev)
    m["fcf_yield"] = yld("fcf", ops)
    m["div_yield"] = yld("div", cos)
    m["buyback_yield"] = yld("bb", ops)
    g = ratio("rev", "rev_p", [c for c in cos if c.get("rev") is not None])
    m["rev_growth"] = (g - 1.0) if g is not None else None
    g = ratio("ni", "ni_p", [c for c in cos if c.get("ni") is not None])
    m["earn_growth"] = (g - 1.0) if g is not None else None
    m["op_margin"] = ratio("oi", "rev", ops)
    prev_pool = [c for c in ops if c.get("oi_p") is not None and c.get("rev_p") is not None]
    m["op_margin_prev"] = ratio("oi_p", "rev_p", prev_pool)
    nd_pool = [c for c in ops if c.get("ebitda") is not None and c.get("debt") is not None and c.get("cash") is not None]
    nd_b = sum(own(c, "ebitda") for c in nd_pool)
    m["nd_ebitda"] = (sum(c["w"] * (c["debt"] - c["cash"]) / c["mcap"] for c in nd_pool) / nd_b) if nd_b > 0 else None
    m["net_cash_w"] = (sum(c["w"] for c in nd_pool if c["cash"] > c["debt"]) / total_w) if total_w else None
    ic = [c["oi"] / c["intexp"] for c in ops if c.get("intexp") and c["intexp"] > 0 and c.get("oi") is not None]
    m["int_cover_median"] = statistics.median(ic) if ic else None
    m["int_cover_n"] = len(ic)
    m["capex_sales"] = ratio("capex", "rev", ops)
    rp = [c for c in ops if c.get("oi") is not None and c.get("equity") is not None and c.get("debt") is not None]
    a = sum(c["w"] * c["oi"] * (1 - TAX) / c["mcap"] for c in rp)
    b = sum(c["w"] * (c["equity"] + c["debt"] - (c.get("cash") or 0.0)) / c["mcap"] for c in rp)
    m["roic"] = (a / b) if (rp and b > 0) else None
    sc = [(c["w"], c["sh"] / c["sh_p"] - 1.0) for c in cos if c.get("sh_p")]
    m["share_change"] = (sum(w * x for w, x in sc) / sum(w for w, _ in sc)) if sc else None
    bv = [c for c in cos if c.get("equity") is not None]
    bsum = sum(own(c, "equity") for c in bv)
    m["pb"] = (sum(c["w"] for c in bv) / bsum) if bsum > 0 else None
    m["fin_w"] = (sum(c["w"] for c in cos if c["kind"] in ("bank", "insurer")) / total_w) if total_w else None
    return m


ROUND = {"pe": 2, "pe_median_profitable": 2, "ev_sales": 3, "ev_ebit": 2, "ev_ebitda": 2, "nd_ebitda": 3,
         "int_cover_median": 2, "pb": 3}


def row_composite(lines, src, cik_of, company_of, price_of, fx_of, today):
    total = sum(ln.get("w") or 0.0 for ln in lines if (ln.get("w") or 0) > 0)
    cos, gaps = resolve(lines, cik_of, company_of, price_of, fx_of, today)
    cov = sum(c["w"] for c in cos)
    out = {"lines_source": src, "n_lines": len([x for x in lines if (x.get("w") or 0) > 0]),
           "n_companies": len(cos), "coverage_w": _r(cov / total if total else 0.0),
           "uncovered": {k: _r(v / total) for k, v in sorted(gaps.items(), key=lambda kv: -kv[1])} if total else {},
           "shown": bool(total) and cov / total >= COVER_MIN}
    if not total:
        out["why"] = src
        out["m"] = None
        return out, cos
    if not out["shown"]:
        big = max(gaps.items(), key=lambda kv: kv[1])[0] if gaps else "no companies"
        out["why"] = (f"not shown: {cov / total * 100:.0f} pct of the weight is covered by SEC filings "
                      f"(the rule is {COVER_MIN * 100:.0f}); the largest gap is: {big}")
        out["m"] = None
        return out, cos
    m = composite(cos, cov)
    out["m"] = {k: (_r(v, ROUND.get(k, 4)) if isinstance(v, float) else v) for k, v in m.items()}
    out["why"] = None
    return out, cos


def per_line(cos):
    """{symbol: [own P/E or None, revenue growth or None, TTM end]} for the
    holdings table: each covered company's OWN figures (P/E is None for a loss)."""
    out = {}
    for c in sorted(cos, key=lambda c: -c["w"]):
        pe = (c["mcap"] / c["ni"]) if (c.get("ni") and c["ni"] > 0) else None
        g = (c["rev"] / c["rev_p"] - 1.0) if (c.get("rev") is not None and c.get("rev_p")) else None
        out[c["t"]] = [_r(pe, 1), _r(g, 4), c["end"]]
    return out


# ==========================================================================
# the run
# ==========================================================================

def read_extras(path=EXTRA_PATH, log=print):
    """extra_funds.json -> {ticker: entry}. Each entry is {"same_book_as": <ladder row>} (a fund
    tracking the same index as a ladder row: its composite IS that row's) or {"nport": true} (its
    own latest SEC N-PORT book, which rotation_members.py lists weekly). Absent file -> {}; an
    unreadable one is reported, never silently dropped."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return (json.load(fh).get("funds")) or {}
    except Exception as ex:
        log(f"[warn] {os.path.basename(path)} unreadable ({type(ex).__name__}: {ex}); no extra funds tonight")
        return {"__error__": f"{os.path.basename(path)} unreadable ({type(ex).__name__})"}


def extra_plan(extras, rows_in, funds, syms):
    """{ticker: (lines, src)} for the N-PORT extras that are not already ladder rows."""
    plan = {}
    for t, e in sorted((extras or {}).items()):
        if t.startswith("__") or t in rows_in or not isinstance(e, dict) or not e.get("nport"):
            continue
        fund = funds.get(t)
        if fund and fund.get("lines"):
            plan[t] = vehicle_lines({"tier": "extra"}, fund, syms)
        else:
            plan[t] = ([], "no SEC N-PORT book cached yet for this fund (rotation_members.py lists it weekly)")
    return plan


def build(members, deep, cik_map, store, fund_names, last, db_close, today, fetch=None,
          budget_s=SEC_BUDGET_S, max_fetch=SEC_MAX, log=print, history=None, extras=None):
    """Pure given its inputs (``fetch`` = cik -> companyfacts dict, raising on
    failure; None = offline). Returns (payload, store, calls)."""
    cmap = (cik_map or {}).get("map") or {}
    comps = store.setdefault("companies", {})
    funds = (deep or {}).get("funds") or {}
    syms = (deep or {}).get("symbols") or {}
    closes = (last or {}).get("closes") or {}
    fxr = dict((last or {}).get("fx") or {})
    fxr["USD"] = 1.0

    def cik_of(sym):
        return cmap.get(sym) or cmap.get(sym.replace("-", "."))

    def price_of(sym):
        v = closes.get(sym)
        if v and v[1] and (v[2] in (None, "USD")):
            return v[1]
        return db_close(sym) if db_close else None

    rows_in = (members or {}).get("rows") or {}
    plan = {}
    want = {}
    xplan = extra_plan(extras, rows_in, funds, syms)
    for t, row in list(rows_in.items()) + [(t, None) for t in xplan]:
        lines, src = vehicle_lines(row, funds.get(t), syms) if row is not None else xplan[t]
        if row is not None:
            plan[t] = (lines, src)
        for ln in lines:
            s = ln.get("sym")
            if is_us_symbol(s) and cik_of(s) and price_of(s):
                c = cik_of(s)
                if c not in want or ln["w"] > want[c][0]:
                    want[c] = (ln["w"], s)

    # ---- refresh the company store, heaviest first, inside the budget ----
    calls = {"sec": 0, "sec_failed": 0, "unavailable": 0, "due": 0, "budget_left": 0}
    order = sorted((c for c in want if due(comps.get(c), today)), key=lambda c: (-want[c][0], c))
    calls["due"] = len(order)
    t0 = time.time()
    if fetch is not None:
        for i, cik in enumerate(order):
            if time.time() - t0 > budget_s or calls["sec"] >= max_fetch:
                calls["budget_left"] = len(order) - i
                log(f"[warn] SEC budget spent: {len(order) - i} companies wait for the next run")
                break
            sym = want[cik][1]
            try:
                cf = fetch(cik)
                if cf is None:
                    calls["unavailable"] += 1
                    continue
                calls["sec"] += 1
                comps[cik] = company_from_facts(cf, sym, today)
            except Exception as ex:
                calls["sec"] += 1
                calls["sec_failed"] += 1
                code = getattr(ex, "code", None)
                if code == 404:
                    comps[cik] = {"t": sym, "fetched": today, "status": "na", "why": "SEC has no companyfacts"}
                else:
                    old = dict(comps.get(cik) or {})
                    if old.get("end"):
                        old["fetched"] = today      # keep the last good record; due again next week
                    else:
                        old.update(t=sym, fetched=today, status="error", why=f"fetch failed ({type(ex).__name__})")
                    comps[cik] = old
    else:
        calls["budget_left"] = len(order)
    if fetch is not None and calls["unavailable"] and not calls["budget_left"]:
        calls["budget_left"] = calls["unavailable"]

    def company_of(cik, sym):
        c = comps.get(cik)
        if c and c.get("status") in ("ok", "partial") and c.get("end"):
            return c
        fr = fund_names.get(sym)
        if fr and fr.get("status") in ("ok", "partial"):
            return company_from_fundamentals(fr, sym, today)
        return c

    def fx_of(ccy):
        return fxr.get(ccy)

    rows, spy_m = {}, None
    for t, (lines, src) in plan.items():
        out, cos = row_composite(lines, src, cik_of, company_of, price_of, fx_of, today)
        out["per_line"] = per_line(cos)
        rows[t] = out
    spy = rows.get("SPY")
    spy_m = (spy or {}).get("m") if (spy or {}).get("shown") else None
    for t, out in rows.items():
        if out.get("m") and spy_m:
            out["vs_spy"] = {k: (_r(out["m"][k] / spy_m[k], 3) if (out["m"].get(k) and spy_m.get(k)) else None)
                             for k in ("pe", "ev_sales", "ev_ebit", "fcf_yield", "pb")}
    # B1 stage 5: today's composite against the row's own point-in-time quarters
    # (data/rotation_fundamentals_history.json, written by basket_history.py)
    if history and history.get("rows"):
        import basket_history as H
        hrows = history["rows"]
        for t, out in rows.items():
            if t in hrows:
                out["history"] = H.history_block(t, out.get("m"), hrows[t], hrows.get("SPY"), spy_m)
                out["history"]["status"] = history.get("status")
    # extra funds (extra_funds.json): not ladder rows, the same machinery, a separate key so
    # every reader of `rows` sees exactly the ladder
    extra_rows = {}
    for t, e in sorted((extras or {}).items()):
        if t.startswith("__") or t in rows:
            continue
        if isinstance(e, dict) and e.get("same_book_as"):
            b = e["same_book_as"]
            if b in rows:
                out = json.loads(json.dumps(rows[b]))
                out["lines_source"] = f"the same book as {b} (both track the same index): " + (rows[b].get("lines_source") or "")
            else:
                out = {"shown": False, "m": None, "why": f"not shown: its book, the ladder row {b}, has no composite tonight"}
            out["same_book_as"] = b
        elif t in xplan:
            out, cos = row_composite(xplan[t][0], xplan[t][1], cik_of, company_of, price_of, fx_of, today)
            out["per_line"] = per_line(cos)
            if out.get("m") and spy_m:
                out["vs_spy"] = {k: (_r(out["m"][k] / spy_m[k], 3) if (out["m"].get(k) and spy_m.get(k)) else None)
                                 for k in ("pe", "ev_sales", "ev_ebit", "fcf_yield", "pb")}
            out["history"] = {"n": 0, "why": "no own history: the back-fill covers ladder rows only"}
        else:
            out = {"shown": False, "m": None, "why": "extra_funds.json entry names neither same_book_as nor nport"}
        out["extra"] = True
        extra_rows[t] = out
    n_shown = sum(1 for r in rows.values() if r.get("shown"))
    fresh = sum(1 for c in comps.values() if c.get("end") and _days(c["end"], today) <= STALE_DAYS)
    status = "ok"
    errors = []
    if not rows:
        status, errors = "failed", ["rotation_members.json has no rows"]
    elif not closes:
        status = "partial"
        errors.append("no last closes from rotation_members (data/cache/rotation_last_closes.json): "
                      "only prices.db names have a market cap")
    if calls["sec_failed"] and calls["sec_failed"] >= max(5, calls["sec"] // 2):
        status = "partial"
        errors.append(f"SEC: {calls['sec_failed']} of {calls['sec']} companyfacts fetches failed")
    if calls["budget_left"]:
        errors.append(f"{calls['budget_left']} companies still wait for their first or next SEC fetch")
    if (extras or {}).get("__error__"):
        errors.append(extras["__error__"])
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "asof": (members or {}).get("asof"),
        "status": status,
        "error": "; ".join(errors) or None,
        "method": METHOD,
        "rules": {"cover_min": COVER_MIN, "stale_days": STALE_DAYS, "tax_roic": TAX},
        "store": {"companies": len(comps), "fresh": fresh, "wanted": len(want), "due": calls["due"],
                  "fetched": calls["sec"], "failed": calls["sec_failed"], "waiting": calls["budget_left"]},
        "n_rows": len(rows), "n_shown": n_shown,
        "rows": rows,
        "n_extra": len(extra_rows), "extra_rows": extra_rows,
    }
    return payload, store, calls


METHOD = (
    "Look-through composites from each company's own SEC filings (XBRL companyfacts, parsed by "
    "fundamentals.py). A fund with weight w in a company of market value mcap owns w/mcap of it; every "
    "measure is the sum of those owned slices of a numerator over the same of a denominator. P/E = sum(w) / "
    "sum(w x earnings / mcap), the weighted harmonic mean, losses included ('aggregate loss' when the "
    "sum is not positive). EV measures and margins over operating companies only (banks and insurers: P/E, "
    "P/B, dividend yield). Growth holds today's ownership fixed: sum(w x X now / mcap) / sum(w x X a year "
    "earlier / mcap) - 1. Trailing twelve months, GAAP, as filed; statements converted to US dollars at the "
    "run's FX; market value = last close x latest filed share count (per ADR for an ADR); companies "
    "aggregated by CIK. Covered = a US listing in SEC's ticker map whose company has usable SEC figures "
    "ending no more than 200 days ago, a last close and an FX rate. A composite is shown only when covered "
    "companies make up at least 70 pct of the row's weight. No analyst estimates or revisions: there is no free "
    "source. A fund's own published figures use other definitions (vendor earnings, losses often excluded) "
    "and are a cross-check, not this number."
)


# ==========================================================================
# files
# ==========================================================================

def write_store(store, path=STORE_PATH):
    """One company per line (a compact array in REC_FIELDS order), so a day's
    refresh is a small diff and the public repo's line guard reads each alone."""
    import rotation_members as RM
    comps = store.get("companies") or {}
    L = ["{", f'"generated_at":{json.dumps(store.get("generated_at"))},',
         '"source":"SEC XBRL companyfacts via sec_facts.py, parsed by fundamentals.build_company; '
         'money in millions of the filing currency, shares in millions",',
         f'"fields":{json.dumps(list(REC_FIELDS))},', '"companies":{']
    ks = sorted(comps)
    for i, k in enumerate(ks):
        c = comps[k]
        L.append(f"{json.dumps(k)}:" + json.dumps([c.get(f) for f in REC_FIELDS], ensure_ascii=False,
                                                  separators=(",", ":")) + ("," if i < len(ks) - 1 else ""))
    L += ["}", "}"]
    bad = [ln for ln in L if RM.guard_trips(ln)]
    if bad:
        raise ValueError(f"{len(bad)} store line(s) would trip the public repo's guard: {bad[0][:120]}")
    text = "\n".join(L) + "\n"
    json.loads(text)
    _atomic(path, text)


def read_store(path=STORE_PATH):
    d = _load(path, {}) or {}
    comps = {}
    fields = d.get("fields") or list(REC_FIELDS)
    for k, v in (d.get("companies") or {}).items():
        comps[k] = dict(zip(fields, v)) if isinstance(v, list) else v
    return {"generated_at": d.get("generated_at"), "companies": comps}


def _atomic(path, text):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def write_payload(payload, path=OUT_PATH):
    import rotation_members as RM
    text = json.dumps(payload, indent=0, separators=(",", ":"), ensure_ascii=False)
    bad = [ln for ln in text.splitlines() if RM.guard_trips(ln)]
    if bad:
        raise ValueError(f"{len(bad)} payload line(s) would trip the public repo's guard: {bad[0][:120]}")
    _atomic(path, text + "\n")


def db_closer(path=DB_PATH):
    """sym -> the latest close in the price database (through pricedb only), or None."""
    try:
        import pricedb
        conn = pricedb.connect(path, readonly=True)
    except Exception:
        return None
    cache = {}

    def close(sym):
        if sym not in cache:
            try:
                r = conn.execute("SELECT close FROM daily_prices WHERE ticker=? AND close IS NOT NULL "
                                 "ORDER BY date DESC LIMIT 1", (sym,)).fetchone()
            except Exception:
                r = None
            cache[sym] = r[0] if r else None
        return cache[sym]
    return close


def main(argv=None):
    ap = argparse.ArgumentParser(description="Look-through SEC composites for the rotation ladder rows")
    ap.add_argument("--offline", action="store_true", help="cached companyfacts only, no SEC requests")
    ap.add_argument("--members", default=MEMBERS_PATH)
    ap.add_argument("--deep", default=DEEP_PATH)
    ap.add_argument("--last", default=LAST_PATH)
    ap.add_argument("--store", default=STORE_PATH)
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--budget", type=float, default=SEC_BUDGET_S)
    args = ap.parse_args(argv)
    t0 = time.time()
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    members = _load(args.members)
    if not members or not members.get("rows"):
        print(f"[FAIL] {args.members} missing or empty -- keeping yesterday's {args.out}")
        return 1
    store = read_store(args.store)
    fund_names = ((_load(FUND_PATH, {}) or {}).get("names")) or {}
    fetch = None
    if not args.offline:
        import sec_facts
        fetch = lambda cik: sec_facts.companyfacts(cik, max_age_h=20.0)  # noqa: E731
    else:
        import sec_facts
        fetch = lambda cik: sec_facts.companyfacts(cik, offline=True)  # noqa: E731  (cache or None)
    payload, store, calls = build(members, _load(args.deep, {}), _load(CIK_PATH, {}), store, fund_names,
                                  _load(args.last, {}), db_closer(args.db), today, fetch=fetch,
                                  budget_s=args.budget,
                                  history=_load(os.path.join(DATA, "rotation_fundamentals_history.json"), {}),
                                  extras=read_extras())
    payload["calls"] = calls
    payload["run_s"] = round(time.time() - t0, 1)
    if calls["sec"] and not args.offline:
        store["generated_at"] = payload["generated_at"]
    if not args.offline:
        write_store(store, args.store)
    write_payload(payload, args.out)
    print(f"wrote {args.out}: {payload['n_shown']} of {payload['n_rows']} rows shown, status {payload['status']}, "
          f"store {payload['store']}, {os.path.getsize(args.out) / 1024:.0f} KB, {payload['run_s']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
