"""Valuation multiples over time and against peers -> data/valuation.json
(+ data/valuation_history.json for the charts).

jr-dash: the Name tab's "Valuation multiples" section (SPEC-78 amendment, 2026-11-04).

Inputs, and nothing else:
  * prices.db `daily_prices.close` -- the source of record for every price here
    (split-adjusted, so fundamentals.py puts share counts on the same basis);
  * data/fundamentals.json -- quarterly TTM statements + point-in-time share counts;
  * data/peers.json -- hand-editable peer overrides; defaults from tickers.txt sections;
  * FX closes `<CCY>USD=X` in prices.db for filers reporting in another currency.

No look-ahead: on each session a quarter's statements apply only from the date its
filing first carried them (`known`), and the share count is the latest one FILED on
or before that session.

EV = market cap + debt - (cash + short-term marketable securities). Operating leases
are excluded from debt; minority interests and preferred are not added (stated on the
tab). Banks and insurers get P/E and P/B only.

The flags are INFORMATION. They describe where a multiple sits; they never move an
anchor or a level (anchors move only on Jake's word) and never say buy or sell.

    python valuation.py
    python valuation.py --at GOOGL=285.16 --show GOOGL     # one-name check at a quoted price
"""
import argparse
import bisect
import datetime as dt
import json
import math
import os
import sqlite3
import statistics
import sys

import fundamentals as F

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
DB = os.path.join(HERE, "prices.db")
OUT = os.path.join(DATA, "valuation.json")
OUT_HIST = os.path.join(DATA, "valuation_history.json")

# ============================================================================
# THRESHOLDS -- the one constants block. Every flag on the tab is computed from
# these; the payload carries them so the tab can print them. Jake's to change.
# ============================================================================
THRESHOLDS = {
    "own_window_years": 5,        # "own history" = this many years of daily values
    "own_min_sessions": 504,      # fewer valid sessions than this (~2y): no own-history flag
    "own_cheap_pct": 10,          # at/below this percentile of own history = cheap vs itself
    "own_rich_pct": 90,           # at/above = rich vs itself
    "own_z": 1.5,                 # ...or a z-score beyond +/- this many standard deviations
    "peer_gap": 0.30,             # >= 30% below / above the peer median = cheap / rich vs peers
    "peer_min": 2,                # a peer median needs at least this many peers with a value
    "min_agree": 2,               # core metrics that must agree (same side) for any flag
    "fund_min_rev_m": 100.0,      # below $100M TTM revenue, growth/margin moves are noise: no fundamentals read
    "growth_floor": 0.0,          # TTM revenue growth below this = deteriorating
    "margin_drop_pts": 2.0,       # TTM operating margin down more than this (pts, y/y) = deteriorating
    "stale_days": F.STALE_DAYS,   # statements older than this are shown, never flagged
    "history_daily_sessions": 260,
    "history_years": 10,
}

# key, label, lower-is-cheaper, core (votes in a flag), fmt
METRICS = [
    ("pe", "P/E (TTM)", True, True, "x"),
    ("pe_fwd", "P/E (forward)", True, False, "x"),
    ("ps", "P/S", True, False, "x"),
    ("ev_s", "EV/Sales", True, True, "x"),
    ("ev_ebitda", "EV/EBITDA", True, True, "x"),
    ("ev_ebit", "EV/EBIT", True, False, "x"),
    ("p_fcf", "P/FCF", True, True, "x"),
    ("fcf_y", "FCF yield", False, False, "%"),
    ("pb", "P/B", True, False, "x"),
]
# Above these a multiple says "the denominator is near zero", not "expensive": it is
# shown as n/m with that reason and kept out of history stats and peer medians.
MAX_MEANINGFUL = {"pe": 150.0, "ev_ebitda": 150.0, "ev_ebit": 150.0, "p_fcf": 150.0,
                  "ev_s": 100.0, "ps": 100.0, "pb": 100.0}
EV_METRICS = {"ev_s", "ev_ebitda", "ev_ebit"}


def partial_inputs(row):
    """{metric: reason} for metrics whose CURRENT inputs are incomplete. Such a metric
    is shown with its reason, never flagged, and kept out of peer medians."""
    out = {}
    if not row:
        return out
    if row.get("debt_asof"):
        for k in EV_METRICS:
            out[k] = "debt as of %s (no debt tag on the %s balance sheet)" % (row["debt_asof"], row.get("end"))
    if row.get("da_partial"):
        out.setdefault("ev_ebitda", "D&A only partly tagged (amortization missing or annualised)")
    if row.get("oi_proxy"):
        for k in ("ev_ebit", "ev_ebitda"):
            out.setdefault(k, "no operating-income line; EBIT is pretax income + interest")
    ni, oi = row.get("ni"), row.get("oi")
    if ni is not None and ni > 0 and oi is not None and (oi <= 0 or ni > 1.5 * oi):
        out["pe"] = ("net income is mostly non-operating (TTM net income %s vs operating income %s): "
                     "gains on investments or tax items, not earnings power" % (round(ni), round(oi)))
    if row.get("cfo_advances"):
        for k in ("p_fcf", "fcf_y"):
            out.setdefault(k, "operating cash flow is mostly customer advances (the rise in contract "
                              "liabilities equals %d%% of TTM CFO), not recurring free cash flow"
                           % round(row["cfo_advances"] * 100))
    return out


BANK_METRICS = {"pe", "pb", "ps"}
BANK_CORE = {"pe", "pb"}
HIST_METRICS = ["ev_ebitda", "ev_s", "pe", "p_fcf", "pb"]
PE_FWD_WHY = ("no forward EPS source: the DCF street blocks carry price targets, not EPS consensus, "
              "so a forward P/E would be invented")


# ---- prices ------------------------------------------------------------------
def load_closes(con, tk):
    rows = con.execute("SELECT date, close FROM daily_prices WHERE ticker=? AND close IS NOT NULL ORDER BY date",
                       (tk,)).fetchall()
    return [r[0] for r in rows], [float(r[1]) for r in rows]


def asof_value(dates, vals, d):
    i = bisect.bisect_right(dates, d) - 1
    return vals[i] if i >= 0 else None


# ---- per-session multiples ------------------------------------------------------
def fund_index(rec):
    """Quarter rows sorted by `known`, and share observations sorted by `filed`."""
    q = sorted([r for r in rec.get("quarters", []) if r.get("known")], key=lambda r: (r["known"], r["end"]))
    s = sorted([x for x in rec.get("shares", []) if x.get("filed")], key=lambda x: x["filed"])
    return q, [r["known"] for r in q], s, [x["filed"] for x in s]


def latest_known(q, qk, d):
    """The newest quarter (by period end) whose filing was public on d."""
    i = bisect.bisect_right(qk, d)
    if i == 0:
        return None
    return max(q[:i], key=lambda r: r["end"])


def multiples(row, mcap, kind):
    """{metric: value or None} for one session. Money in $M."""
    out = {}
    if row is None or mcap is None or mcap <= 0:
        return out
    rev, ni, oi, ebitda, fcf, eq = (row.get(k) for k in ("rev", "ni", "oi", "ebitda", "fcf", "equity"))
    bank = kind in ("bank", "insurer")
    # Debt or cash unknown -> EV unknown. Never a zero fill (coordinator review,
    # 2026-11-04: ORCL had been valued with debt = 0 and flagged on it).
    ev = None if bank or row.get("debt") is None or row.get("cash") is None else \
        mcap + row["debt"] - row["cash"]
    out["pe"] = mcap / ni if ni and ni > 0 else None
    out["ps"] = mcap / rev if rev and rev > 0 else None
    out["pb"] = mcap / eq if eq and eq > 0 else None
    if not bank:
        out["ev_s"] = ev / rev if ev is not None and rev and rev > 0 else None
        out["ev_ebitda"] = ev / ebitda if ev is not None and ebitda and ebitda > 0 and ev > 0 else None
        out["ev_ebit"] = ev / oi if ev is not None and oi and oi > 0 and ev > 0 else None
        out["p_fcf"] = mcap / fcf if fcf and fcf > 0 else None
        out["fcf_y"] = fcf / mcap * 100.0 if fcf is not None else None
    for k, cap in MAX_MEANINGFUL.items():
        if out.get(k) is not None and out[k] > cap:
            out[k] = None
    return out


def why_none(key, row, kind):
    """Plain-English reason a metric has no value."""
    if key == "pe_fwd":
        return PE_FWD_WHY
    if kind in ("bank", "insurer") and key not in BANK_METRICS:
        return kind + ": EV-based multiples are not meaningful for a balance-sheet business"
    if row is None:
        return "no statements filed before this date"
    if key in EV_METRICS and row.get("debt") is None:
        return row.get("debt_why") or "debt not tagged in XBRL"
    if key in EV_METRICS and row.get("cash") is None:
        return "cash not tagged in XBRL for this balance sheet"
    need = {"pe": ("ni", "a loss (TTM net income is negative)"), "ps": ("rev", "no revenue"),
            "pb": ("equity", "negative or untagged book equity"), "ev_s": ("rev", "no revenue"),
            "ev_ebitda": ("ebitda", "EBITDA is negative" if row.get("ebitda") is not None else
                          "D&A is not tagged, so EBITDA cannot be built"),
            "ev_ebit": ("oi", "operating income is negative" if row.get("oi") is not None else "no operating-income line"),
            "p_fcf": ("fcf", "free cash flow is negative" if row.get("fcf") is not None else "no capex line tagged"),
            "fcf_y": ("fcf", "no capex line tagged")}
    k, msg = need.get(key, (None, "not available"))
    v = row.get(k) if k else None
    if v is not None and v > 0 and key in MAX_MEANINGFUL:
        return "above %dx - the denominator is near zero, so the multiple is not meaningful" % MAX_MEANINGFUL[key]
    return msg


def series_for(tk, rec, con, fx_cache):
    """Per-session multiples for one name: (dates, {metric: [values]}, info)."""
    dates, closes = load_closes(con, tk)
    q, qk, s, sf = fund_index(rec)
    kind, ccy, adr = rec.get("kind"), rec.get("currency") or "USD", rec.get("adr_ratio") or 1.0
    fx_d = fx_v = None
    if ccy != "USD":
        if ccy not in fx_cache:
            fx_cache[ccy] = load_closes(con, ccy + "USD=X")
        fx_d, fx_v = fx_cache[ccy]
        if not fx_d:
            return dates, {}, {"why": "reports in %s and prices.db has no %sUSD=X history yet - FX history pending" % (ccy, ccy)}
    out = {m[0]: [] for m in METRICS}
    keep = []
    for d, px in zip(dates, closes):
        row = latest_known(q, qk, d)
        i = bisect.bisect_right(sf, d) - 1
        if row is None or i < 0:
            continue
        sh = s[i]["adj"]
        mcap = px * sh / adr
        r = row
        if fx_d:
            fx = asof_value(fx_d, fx_v, d)
            if fx is None:
                continue
            r = {k: (v * fx if isinstance(v, (int, float)) and k not in ("eps",) else v) for k, v in row.items()}
        m = multiples(r, mcap, kind)
        keep.append(d)
        for key, *_ in METRICS:
            out[key].append(m.get(key))
    return keep, out, {}


# ---- statistics ----------------------------------------------------------------
def own_stats(dates, vals, cur, end):
    """Median, percentile, z-score of `cur` against the trailing window."""
    start = (dt.date.fromisoformat(end) - dt.timedelta(days=int(365.25 * THRESHOLDS["own_window_years"]))).isoformat()
    w = [v for d, v in zip(dates, vals) if v is not None and start <= d <= end]
    first = next((d for d, v in zip(dates, vals) if v is not None and d >= start), None)
    res = {"n": len(w), "from": first}
    if len(w) < 20 or cur is None:
        return res
    res["med"] = statistics.median(w)
    res["pct"] = round(100.0 * sum(1 for v in w if v <= cur) / len(w), 1)
    sd = statistics.pstdev(w)
    res["z"] = round((cur - statistics.fmean(w)) / sd, 2) if sd > 0 else 0.0
    res["lo"], res["hi"] = min(w), max(w)
    return res


def growth(rec, row):
    """TTM revenue growth y/y, operating margin and its y/y change (pts)."""
    if row is None:
        return {}
    prev = None
    for r in rec.get("quarters", []):
        n = (dt.date.fromisoformat(row["end"]) - dt.date.fromisoformat(r["end"])).days
        if 350 <= n <= 380:
            prev = r
    g = {}
    if prev and row.get("rev") and prev.get("rev") and prev["rev"] > 0:
        g["rev_g"] = row["rev"] / prev["rev"] - 1.0
    if row.get("rev") and row.get("oi") is not None and row["rev"] > 0:
        g["om"] = row["oi"] / row["rev"]
        if prev and prev.get("rev") and prev.get("oi") is not None and prev["rev"] > 0:
            g["om_chg"] = (g["om"] - prev["oi"] / prev["rev"]) * 100.0
    if (row.get("rev") or 0) < THRESHOLDS["fund_min_rev_m"]:
        g["small"] = True          # growth and margin moves on a tiny base say nothing
        return g
    if "rev_g" in g or "om_chg" in g:
        bad = (g.get("rev_g") is not None and g["rev_g"] < THRESHOLDS["growth_floor"]) or \
              (g.get("om_chg") is not None and g["om_chg"] < -THRESHOLDS["margin_drop_pts"])
        g["holds"] = not bad
    return g


# ---- peers ---------------------------------------------------------------------
def load_peers():
    try:
        with open(os.path.join(DATA, "peers.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"overrides": {}}


def default_peers(tk, sections, mcaps, n=6):
    sec = sections.get(tk)
    if not sec or tk not in mcaps:
        return []
    cands = [t for t, s in sections.items() if s == sec and t != tk and t in mcaps]
    cands.sort(key=lambda t: abs(math.log(mcaps[t] / mcaps[tk])))
    return cands[:n]


# ---- flags ---------------------------------------------------------------------
def pct_word(p):
    p = int(round(p))
    suf = "th" if 10 <= p % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(p % 10, "th")
    return "%d%s percentile" % (p, suf)


def fmt_val(key, v):
    if v is None:
        return "n/m"
    return ("%.1f%%" % v) if key == "fcf_y" else ("%.1fx" % v)


def metric_flags(m, kind):
    """Annotate one metric dict with own / peer reads ('cheap' / 'rich' / None)."""
    T = THRESHOLDS
    own = peer = None
    if m.get("partial"):               # partial / stale inputs never raise a flag
        m["own"], m["peer"] = None, None
        return m
    if m.get("now") is not None and m.get("n", 0) >= T["own_min_sessions"] and m.get("pct") is not None:
        if m["pct"] <= T["own_cheap_pct"] or (m.get("z") is not None and m["z"] <= -T["own_z"]):
            own = "cheap"
        elif m["pct"] >= T["own_rich_pct"] or (m.get("z") is not None and m["z"] >= T["own_z"]):
            own = "rich"
    if m.get("prem") is not None:
        if m["prem"] <= -T["peer_gap"]:
            peer = "cheap"
        elif m["prem"] >= T["peer_gap"]:
            peer = "rich"
    m["own"], m["peer"] = own, peer
    return m


def name_flags(tk, rec_v, peers_used):
    """[{kind, text}] for one name, strongest first, plus a sort score."""
    T = THRESHOLDS
    kind = rec_v.get("kind")
    core = BANK_CORE if kind in ("bank", "insurer") else {m[0] for m in METRICS if m[3]}
    M = rec_v["metrics"]
    # a metric with partial/stale inputs neither votes nor counts toward "of N"
    ev = [k for k in core if M.get(k, {}).get("now") is not None and not M[k].get("partial")]
    both = [k for k in ev if M[k].get("n", 0) >= T["own_min_sessions"] and M[k].get("peer_med") is not None]
    cheap = [k for k in both if M[k]["own"] == "cheap" and M[k]["peer"] == "cheap"]
    rich = [k for k in both if M[k]["own"] == "rich" and M[k]["peer"] == "rich"]
    # Strictly min_agree metrics (coordinator review 2026-11-04): a name with fewer
    # evaluable core multiples than that gets no flag at all, rather than a flag
    # resting on its one remaining multiple.
    need = T["min_agree"]
    g = rec_v.get("growth", {})
    holds = g.get("holds")
    out = []
    label = {k: next(m[1] for m in METRICS if m[0] == k) for k in M}
    pnames = ", ".join(peers_used[:5]) + ("..." if len(peers_used) > 5 else "")

    def own_bit(k):
        x = M[k]
        return "%s %s sits at the %s of its own %s (median %s)" % (
            label[k], fmt_val(k, x["now"]), pct_word(x["pct"]), x.get("window", "history"), fmt_val(k, x["med"]))

    def peer_bit(k):
        x = M[k]
        return "%d%% %s its peers' median %s (%s)" % (abs(round(x["prem"] * 100)), "below" if x["prem"] < 0 else "above",
                                                     fmt_val(k, x["peer_med"]), pnames)

    def fund_bit():
        parts = []
        if g.get("rev_g") is not None:
            parts.append("TTM revenue %s %.0f%% y/y" % ("grew" if g["rev_g"] >= 0 else "fell", abs(g["rev_g"] * 100)))
        if g.get("om_chg") is not None:
            parts.append("operating margin %s %.1f pts" % ("rose" if g["om_chg"] >= 0 else "fell", abs(g["om_chg"])))
        return " and ".join(parts) if parts else "no year-ago quarter to compare"

    if rec_v.get("stale"):
        return [], 0.0
    if cheap and len(cheap) >= need:
        k = max(cheap, key=lambda k: abs(M[k].get("z") or 0))
        if holds is True:
            out.append({"kind": "OPPORTUNITY", "metrics": cheap,
                        "text": "%s, and %s, while %s - cheap against its own history and its peers with the "
                                "fundamentals intact." % (own_bit(k), peer_bit(k), fund_bit())})
        elif holds is False:
            out.append({"kind": "VALUE-TRAP WATCH", "metrics": cheap,
                        "text": "%s, and %s, but %s - cheap on both counts while the fundamentals are "
                                "deteriorating, which is how a value trap looks." % (own_bit(k), peer_bit(k), fund_bit())})
    if rich and len(rich) >= need:
        k = max(rich, key=lambda k: abs(M[k].get("z") or 0))
        out.append({"kind": "STRETCHED", "metrics": rich,
                    "text": "%s, and %s - expensive against its own history and its peers at once (%s)." % (
                        own_bit(k), peer_bit(k), fund_bit())})
    if not out:
        # DISLOCATION: at least min_agree core metrics beyond the own-history bands on
        # the SAME side. A peer gap on its own is NOT a flag (Jake, 2026-11-04, "Ship
        # it" on the coordinator's defaults): it is often business mix, so it stays
        # in the multiples and peer tables (prem, "cheap/rich vs peers") as information.
        own_ok = [k for k in ev if M[k].get("pct") is not None]
        need_own = T["min_agree"]
        for side in ("cheap", "rich"):
            own_x = [k for k in own_ok if M[k].get("own") == side]
            if own_x and len(own_x) >= need_own:
                k = max(own_x, key=lambda k: abs(M[k].get("z") or 0))
                tail = (" It is also " + peer_bit(k) + ".") if M[k].get("prem") is not None and abs(M[k]["prem"]) >= 0.10 else ""
                out.append({"kind": "DISLOCATION", "metrics": own_x, "side": side,
                            "text": "%s - %s against itself on %d of %d multiples.%s" % (
                                own_bit(k), side, len(own_x), len(own_ok), tail)})
                break
    score = 0.0
    for k in ev:
        score = max(score, abs(M[k].get("z") or 0), abs(M[k].get("prem") or 0) * 3)
    return out, round(score, 2)


KIND_ORDER = {"OPPORTUNITY": 0, "VALUE-TRAP WATCH": 1, "STRETCHED": 2, "DISLOCATION": 3}
FLAG_KINDS = tuple(KIND_ORDER)   # the only kinds a flag line may carry


# ---- main ----------------------------------------------------------------------
def r3(v, nd=2):
    return None if v is None else round(v, nd)


def build(at=None, only=None):
    with open(os.path.join(DATA, "fundamentals.json"), encoding="utf-8") as f:
        FU = json.load(f)
    P = load_peers()
    overrides = P.get("overrides") or {}
    sections = {tk: hdr for tk, _, hdr in F.read_tickers()}
    con = sqlite3.connect(DB)
    have_px = {r[0] for r in con.execute("SELECT DISTINCT ticker FROM daily_prices")}
    fx_cache = {}
    names = FU.get("names", {})
    out = {"generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
           "source": "prices.db closes x SEC companyfacts (fundamentals.json); peers.json",
           "fundamentals_generated_at": FU.get("generated_at"),
           "thresholds": THRESHOLDS,
           "metrics": [{"key": k, "label": l, "lower_is_cheaper": lo, "core": c} for k, l, lo, c, _ in METRICS],
           "method": ("EV = market cap + debt - (cash + short-term marketable securities); operating leases, "
                      "minority interests and preferred are not included. Market cap = prices.db close x the latest "
                      "share count FILED on or before that session (split-adjusted) / ADR ratio. Statements apply from "
                      "the date their filing first made them public. Non-USD filers convert at the session's "
                      "<CCY>USD=X close. Own history = the trailing %d years of daily values." % THRESHOLDS["own_window_years"]),
           "names": {}, "na": dict(FU.get("na", {})), "dislocations": []}
    hist = {"generated_at": out["generated_at"], "metrics": HIST_METRICS, "names": {}}
    series = {}
    for tk, rec in names.items():
        if only and tk not in only and tk not in set(sum(([*(overrides.get(o, {}).get("peers") or [])] for o in only), [])):
            pass
        if rec.get("status") == "na":
            out["na"][tk] = rec.get("why")
            continue
        if tk not in have_px:
            out["na"][tk] = "price history pending - not in prices.db yet (added to tickers.txt for the backfill)"
            continue
        d, s, info = series_for(tk, rec, con, fx_cache)
        if info.get("why"):
            out["na"][tk] = info["why"]
            continue
        if not d:
            out["na"][tk] = "no session where both a close and a filed share count exist"
            continue
        series[tk] = (d, s)
    # current values (optionally at a quoted price)
    cur = {}
    for tk, (d, s) in series.items():
        rec = names[tk]
        q, qk, sh, sf = fund_index(rec)
        end = d[-1]
        row = latest_known(q, qk, end)
        now = {k: s[k][-1] for k in s}
        px = None
        dates, closes = load_closes(con, tk)
        px = closes[-1] if closes else None
        if at and tk in at:
            px = at[tk]
            mcap = px * sh[-1]["adj"] / (rec.get("adr_ratio") or 1.0)
            r = row
            if rec.get("currency", "USD") != "USD":
                fx = fx_cache[rec["currency"]][1][-1]
                r = {k: (v * fx if isinstance(v, (int, float)) and k != "eps" else v) for k, v in row.items()}
            now = multiples(r, mcap, rec.get("kind"))
        cur[tk] = {"now": now, "row": row, "end": end, "px": px, "shares": sh[-1] if sh else None}
    mcaps = {}
    for tk, c in cur.items():
        rec = names[tk]
        if c["px"] and c["shares"]:
            mcaps[tk] = c["px"] * c["shares"]["adj"] / (rec.get("adr_ratio") or 1.0)
    today = dt.date.today()
    for tk, (d, s) in series.items():
        rec, c = names[tk], cur[tk]
        row, kind = c["row"], rec.get("kind")
        stale = bool(row and (today - dt.date.fromisoformat(row["end"])).days > THRESHOLDS["stale_days"])
        ov = overrides.get(tk)
        if ov:
            pset = list(ov.get("peers") if isinstance(ov, dict) else ov)
            psrc = "override (peers.json): " + (ov.get("why", "") if isinstance(ov, dict) else "")
        else:
            pset = default_peers(tk, sections, mcaps)
            psrc = "default: tickers.txt section '%s', nearest by market cap" % (sections.get(tk) or "?")
        pending = [p for p in pset if p not in have_px]
        na_p = {p: out["na"].get(p) or names.get(p, {}).get("why") or "no fundamentals"
                for p in pset if p not in pending and p not in cur}
        live = [p for p in pset if p in cur]
        # Peer medians use only peers CURRENT on both legs: a close in the last 7 days
        # of this name's close, and statements inside the stale window.
        def is_current(p):
            pr = cur[p]["row"]
            if not pr or (today - dt.date.fromisoformat(pr["end"])).days > THRESHOLDS["stale_days"]:
                return False
            return (dt.date.fromisoformat(c["end"]) - dt.date.fromisoformat(cur[p]["end"])).days <= 7
        current_peers = [p for p in live if is_current(p)]
        stale_peers = [p for p in live if p not in current_peers]
        g = growth(rec, row)
        fx = None
        if rec.get("currency", "USD") != "USD":
            fx = fx_cache[rec["currency"]][1][-1]
        ev_now = None
        if c["px"] and c["shares"] and row and kind not in ("bank", "insurer") \
                and row.get("debt") is not None and row.get("cash") is not None:
            mc = mcaps.get(tk) if not (at and tk in at) else at[tk] * c["shares"]["adj"] / (rec.get("adr_ratio") or 1.0)
            k_fx = fx or 1.0
            ev_now = mc + (row["debt"] - row["cash"]) * k_fx
        part = partial_inputs(row)
        # No entity name: a registrant's legal name can carry a word the public repo's
        # rule-18 guard watches for, and the tab never shows it.
        rv = {"status": rec.get("status"), "why": rec.get("why"), "kind": kind,
              "px": r3(at[tk] if at and tk in at else c["px"]), "px_date": c["end"],
              "mcap": r3(mcaps.get(tk) if not (at and tk in at) else at[tk] * c["shares"]["adj"] / (rec.get("adr_ratio") or 1.0), 0),
              "ev": r3(ev_now, 0), "currency": rec.get("currency"), "fx": r3(fx, 6), "adr_ratio": rec.get("adr_ratio"),
              "shares": c["shares"], "stale": stale,
              "fund": {k: row.get(k) for k in ("end", "known", "rev", "gp", "oi", "da", "ebitda", "ni", "cfo", "capex",
                                               "fcf", "sbc", "cash", "debt", "equity", "da_partial", "oi_proxy",
                                               "debt_src", "debt_asof", "debt_why", "capex_incl_software",
                                               "cfo_advances")
                       if row and row.get(k) is not None} if row else None,
              "da_method": rec.get("da_method"),
              "splits": rec.get("splits"),
              "growth": {k: (r3(v, 4) if isinstance(v, float) else v) for k, v in g.items()},
              "peers": {"set": pset, "src": psrc, "live": current_peers, "stale": stale_peers,
                        "pending": pending, "na": na_p},
              "metrics": {}}
        for key, label, lower, core, _ in METRICS:
            if kind in ("bank", "insurer") and key not in BANK_METRICS and key != "pe_fwd":
                rv["metrics"][key] = {"now": None, "why": why_none(key, row, kind)}
                continue
            now = c["now"].get(key)
            m = {"now": r3(now)}
            if key == "pe_fwd":
                m["why"] = PE_FWD_WHY
                rv["metrics"][key] = m
                continue
            if now is None:
                m["why"] = why_none(key, row, kind)
            elif key in part:
                m["partial"] = part[key]
            st = own_stats(d, s[key], now, c["end"])
            m["n"], m["from"] = st["n"], st["from"]
            yrs = st["n"] / 252.0
            if st["n"] < THRESHOLDS["own_min_sessions"]:
                # under ~24 months: the value and the label only, never a percentile
                m["window"] = "short history (%d months)" % int(round(st["n"] / 21.0))
                m["short"] = True
            else:
                m["window"] = ("%d-year history" % THRESHOLDS["own_window_years"]) if yrs >= THRESHOLDS["own_window_years"] - 0.1 \
                    else "%.1f-year history" % yrs
                for k2 in ("med", "pct", "z", "lo", "hi"):
                    if k2 in st:
                        m[k2] = r3(st[k2])
            # a peer whose own inputs for this metric are partial stays out of the median
            vals = [cur[p]["now"].get(key) for p in current_peers if key not in partial_inputs(cur[p]["row"])]
            vals = [v for v in vals if v is not None and (key == "fcf_y" or v > 0)]
            if len(vals) >= THRESHOLDS["peer_min"]:
                m["peer_med"] = r3(statistics.median(vals))
                m["peer_n"] = len(vals)
                if now is not None and m["peer_med"]:
                    m["prem"] = r3(now / m["peer_med"] - 1.0, 3) if key != "fcf_y" else None
            if lower:
                metric_flags(m, kind)
            rv["metrics"][key] = m
        # growth-adjusted EV/EBITDA: the multiple per point of TTM revenue growth
        def ga(t):
            cc = cur.get(t)
            if not cc:
                return None
            e = cc["now"].get("ev_ebitda")
            gg = growth(names[t], cc["row"]).get("rev_g")
            return e / (gg * 100.0) if e and gg and gg > 0.02 else None
        mine = ga(tk)
        pv = [v for v in (ga(p) for p in current_peers) if v is not None]
        rv["growth_adj"] = {"label": "EV/EBITDA per point of TTM revenue growth", "now": r3(mine, 3),
                            "peer_med": r3(statistics.median(pv), 3) if len(pv) >= THRESHOLDS["peer_min"] else None}
        if mine is not None and rv["growth_adj"]["peer_med"]:
            rv["growth_adj"]["prem"] = r3(mine / rv["growth_adj"]["peer_med"] - 1.0, 3)
        rv["peer_table"] = []
        for p in [tk] + pset:
            if p in cur:
                pn, pr = cur[p]["now"], cur[p]["row"]
                pg = growth(names[p], pr)
                rv["peer_table"].append({"t": p, "self": p == tk, "pe": r3(pn.get("pe"), 1), "ev_s": r3(pn.get("ev_s"), 1),
                                         "ev_ebitda": r3(pn.get("ev_ebitda"), 1), "p_fcf": r3(pn.get("p_fcf"), 1),
                                         "pb": r3(pn.get("pb"), 1),
                                         "rev_g": r3(pg.get("rev_g"), 3), "om": r3(pg.get("om"), 3),
                                         "stmt": pr.get("end") if pr else None})
            else:
                rv["peer_table"].append({"t": p, "self": False,
                                         "na": "price history pending" if p in pending else na_p.get(p, "no data")})
        flags, score = name_flags(tk, rv, current_peers)
        rv["flags"], rv["score"] = flags, score
        if stale:
            rv["stale_why"] = ("latest statements in SEC XBRL end %s (%s) - shown, never flagged; a 20-F filer's "
                               "quarterly reports go on 6-K, which carries no XBRL" % (row["end"], rec.get("taxonomy")))
        out["names"][tk] = rv
        if flags:
            out["dislocations"].append({"t": tk, "kind": flags[0]["kind"], "score": score, "text": flags[0]["text"]})
        # chart history: month-ends for 10y + the last 260 sessions daily
        cut = (dt.date.fromisoformat(c["end"]) - dt.timedelta(days=int(365.25 * THRESHOLDS["history_years"]))).isoformat()
        idx = []
        for i, dd in enumerate(d):
            if dd < cut:
                continue
            last_of_month = i + 1 == len(d) or d[i + 1][:7] != dd[:7]
            if last_of_month or i >= len(d) - THRESHOLDS["history_daily_sessions"]:
                idx.append(i)
        hist["names"][tk] = {"d": [d[i] for i in idx],
                             **{k: [None if s[k][i] is None else round(s[k][i], 1) for i in idx] for k in HIST_METRICS}}
    out["dislocations"].sort(key=lambda x: (KIND_ORDER.get(x["kind"], 9), -x["score"]))
    con.close()
    return out, hist


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--at", action="append", default=[], help="TK=PRICE, value TK at a quoted price (checks only)")
    ap.add_argument("--show", default="", help="print these names' records instead of writing")
    a = ap.parse_args(argv)
    at = {}
    for x in a.at:
        k, v = x.split("=")
        at[k.upper()] = float(v)
    out, hist = build(at=at or None)
    if a.show:
        for tk in a.show.split(","):
            r = out["names"].get(tk.upper()) or {"na": out["na"].get(tk.upper())}
            print(json.dumps({tk: r}, indent=1, ensure_ascii=False))
        return 0
    if at:
        print("refusing to write with --at (a quoted price is a check, not a payload); use --show")
        return 2
    s = json.dumps(out, separators=(",", ":"), ensure_ascii=False)
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(s)
    h = json.dumps(hist, separators=(",", ":"), ensure_ascii=False)
    with open(OUT_HIST, "w", encoding="utf-8", newline="\n") as f:
        f.write(h)
    print("valuation.json: %d names, %d n/a, %d flagged, %d bytes; valuation_history.json %d bytes" % (
        len(out["names"]), len(out["na"]), len(out["dislocations"]), len(s), len(h)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
