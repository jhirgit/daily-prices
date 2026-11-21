#!/usr/bin/env python3
"""macro_reads.py -- the eight computed reads and the seven inflation views that macro.py writes
into data/macro.json (recon-2 sections 5.1 and 5.2; build M1).

Pure functions over what macro.py already fetched: no network, no file writes. Every read lists its
inputs with their values and dates, carries its formula and limits as text, and is computed ONCE here
so the page only renders it (no browser twin). Every view's side comes from the fixed thresholds in
macro_catalogue.json (`views[].tests`, `side_rule`), never from free text.

Trend legs use the board's one definition, imported from regime.py (the port of regime_core.js).
"""
from __future__ import annotations

import datetime as dt
import statistics

PRICE_TICKERS = ["SPY", "IWM", "IJR", "IJH", "EEM", "VGK", "EMLC", "CEW"]
AXIS = "SPY"
Z_MONTHS = 120
Z_WEEKS = 520
SAHM_LINE = 0.5


# ------------------------------------------------------------------ helpers

def _r(x, n=3):
    return None if x is None else round(x, n)


def field_of(rec, field):
    """value / prior / change / yoy / ann3m, an `extra` key, or ann3m_minus_yoy."""
    if rec is None or rec.get("value") is None:
        return None
    if field == "ann3m_minus_yoy":
        a, b = rec.get("ann3m"), rec.get("yoy")
        return None if (a is None or b is None) else a - b
    if field in rec:
        return rec.get(field)
    return (rec.get("extra") or {}).get(field)


def inp(series, sid, field="value"):
    rec = series.get(sid)
    return {"id": sid, "name": (rec or {}).get("name"), "field": field, "value": _r(field_of(rec, field), 4),
            "asof": (rec or {}).get("asof"), "status": (rec or {}).get("status", "missing")}


def _status(inputs, value):
    bad = [i["id"] for i in inputs if i["status"] != "ok"]
    if value is None:
        return "error", "not enough inputs (%s)" % (", ".join(bad) or "a needed value is blank")
    if bad:
        return "partial", "computed, but %s %s not current" % (", ".join(bad), "is" if len(bad) == 1 else "are")
    return "ok", None


def shift_month(p, n):
    k = int(p[:4]) * 12 + int(p[5:7]) - 1 + n
    return "%04d-%02d" % (k // 12, k % 12 + 1)


def robust_z(values, latest):
    """(latest - median) / (1.4826 * median absolute deviation) over `values` (non-null)."""
    v = [x for x in values if x is not None]
    if len(v) < 24 or latest is None:
        return None
    med = statistics.median(v)
    mad = statistics.median(abs(x - med) for x in v)
    if mad == 0:
        return None
    return (latest - med) / (1.4826 * mad)


def monthly(raw, sid):
    return {p: v for p, v in (raw.get(sid) or [])}


def mean_or_none(xs, need):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if len(xs) >= need else None


# ------------------------------------------------------------------ prices and trend legs

def aligned_prices(loader, tickers=PRICE_TICKERS, n=320):
    axis = [d for d, _ in loader(AXIS)][-n:]
    out = {"dates": axis}
    for t in tickers:
        m = dict(loader(t))
        out[t] = [m.get(d) for d in axis]
    return out


def _leg_label(x):
    return {1: "up", -1: "down", 0: "flat"}.get(x)


def _common_tail(prices, *ts):
    """Cut every series at the last session where all of them have a close (a fund whose newest bar has not
    settled yet must not read as 'flat' because its last value is blank)."""
    last = max((i for i in range(len(prices["dates"])) if all(prices[t][i] is not None for t in ts)), default=None)
    if last is None:
        return None, [prices[t] for t in ts]
    return prices["dates"][last], [prices[t][:last + 1] for t in ts]


def ratio_leg(prices, num, den):
    if not prices or num not in prices or den not in prices:
        return None
    import regime
    asof, (a, b) = _common_tail(prices, num, den)
    if sum(1 for x in a if x is not None) < 80 or sum(1 for x in b if x is not None) < 80:
        return {"pair": "%s / %s" % (num, den), "leg": None, "label": None, "reason": "fewer than 80 sessions of history"}
    rat = regime.ratio(a, b)
    leg = regime.ratio_leg_series(rat)[-1]
    last = next((x for x in reversed(rat) if x is not None), None)
    back = rat[-64] if len(rat) > 64 else None
    return {"pair": "%s / %s" % (num, den), "leg": leg, "label": _leg_label(leg),
            "chg_63": _r(None if (last is None or back is None) else (last / back - 1) * 100), "asof": asof}


def price_leg(prices, t):
    if not prices or t not in prices:
        return None
    import regime
    asof, (px,) = _common_tail(prices, t)
    if sum(1 for x in px if x is not None) < 80:
        return {"pair": t, "leg": None, "label": None, "reason": "fewer than 80 sessions of history"}
    leg = regime.trend_leg_series(px, False)[-1]
    last = next((x for x in reversed(px) if x is not None), None)
    back = px[-64] if len(px) > 64 else None
    return {"pair": t, "leg": leg, "label": _leg_label(leg),
            "chg_63": _r(None if (last is None or back is None) else (last / back - 1) * 100), "asof": asof}


# ------------------------------------------------------------------ the eight reads

def read_labour(series, raw):
    u, pay, q, jol, une = (monthly(raw, k) for k in ("unrate", "payems", "jtsqur", "jtsjol", "unemploy"))
    rows, zs = [], []
    p0 = max((p for p, v in u.items() if v is not None), default=None)

    def comp(label, derived, latest_p, sign, unit, window=Z_MONTHS):
        keys = sorted(k for k in derived if k <= latest_p)[-window:] if latest_p else []
        hist = [derived[k] for k in keys]
        latest = derived.get(latest_p) if latest_p else None
        z = robust_z(hist, latest)
        z = None if z is None else sign * z
        zs.append(z)
        rows.append({"label": label, "value": _r(latest), "unit": unit, "asof": latest_p, "z": _r(z, 2)})

    d_u = {p: (u[p] - u[shift_month(p, -3)]) if (u.get(p) is not None and u.get(shift_month(p, -3)) is not None) else None for p in u}
    comp("Unemployment rate, change over 3 months (sign flipped)", d_u, p0, -1, "pp")
    # claims: 4-week average against its own 52-week average, weekly
    cl = [(p, v) for p, v in (raw.get("ic4wsa") or []) if v is not None]
    dev = {}
    for i in range(52, len(cl)):
        base = sum(v for _, v in cl[i - 52:i]) / 52.0
        dev[cl[i][0]] = (cl[i][1] / base - 1) * 100
    comp("Initial claims, 4-week average against its 52-week average (sign flipped)", dev, max(dev) if dev else None, -1, "%", Z_WEEKS)
    pp = max((p for p, v in pay.items() if v is not None), default=None)
    d_pay = {p: (pay[p] - pay[shift_month(p, -3)]) / 3.0 if (pay.get(p) is not None and pay.get(shift_month(p, -3)) is not None) else None for p in pay}
    comp("Payrolls, 3-month average monthly change", d_pay, pp, 1, "thousands")
    pq = max((p for p, v in q.items() if v is not None), default=None)
    comp("Quits rate", q, pq, 1, "%")
    ratio = {p: jol[p] / une[p] for p in jol if jol.get(p) is not None and une.get(p)}
    comp("Job openings per unemployed person", ratio, max(ratio) if ratio else None, 1, "ratio")
    value = mean_or_none(zs, 3)
    # Sahm gap
    avg3 = {}
    for p in u:
        w = [u.get(shift_month(p, -k)) for k in range(3)]
        avg3[p] = sum(w) / 3 if None not in w else None
    sahm, sahm_note = None, None
    if p0 and avg3.get(p0) is not None:
        prior = [avg3.get(shift_month(p0, -k)) for k in range(1, 13)]
        avail = [x for x in prior if x is not None]
        if avail:
            sahm = avg3[p0] - min(avail)
            if len(avail) < 12:
                sahm_note = "%d of the prior 12 months have no 3-month average (the October 2025 household survey was not conducted)" % (12 - len(avail))
    inputs = [inp(series, k) for k in ("unrate", "ic4wsa", "payems", "jtsqur", "jtsjol", "unemploy")]
    st, why = _status(inputs, value)
    return {"value": _r(value, 2), "unit": "average z-score (positive = tighter than its own 10-year median)", "rows": rows,
            "sahm": {"gap": _r(sahm, 2), "line": SAHM_LINE, "over_line": None if sahm is None else sahm >= SAHM_LINE, "asof": p0, "note": sahm_note},
            "inputs": inputs, "status": st, "reason": why}


def read_wages(series):
    w, pr, pce, ulc = (field_of(series.get(k), "yoy") for k in ("eciwag", "ophnfb", "pcepilfe", "ulcnfb"))
    value = None if (w is None or pr is None) else w - pr
    rows = [{"label": "Wages (Employment Cost Index), year over year", "value": _r(w), "unit": "%", "asof": (series.get("eciwag") or {}).get("asof")},
            {"label": "Productivity, year over year", "value": _r(pr), "unit": "%", "asof": (series.get("ophnfb") or {}).get("asof")},
            {"label": "Wages minus productivity", "value": _r(value), "unit": "pp"},
            {"label": "Core PCE prices, year over year", "value": _r(pce), "unit": "%", "asof": (series.get("pcepilfe") or {}).get("asof")},
            {"label": "Measured unit labour costs, year over year", "value": _r(ulc), "unit": "%", "asof": (series.get("ulcnfb") or {}).get("asof")}]
    inputs = [inp(series, k, "yoy") for k in ("eciwag", "ophnfb", "pcepilfe", "ulcnfb")]
    st, why = _status(inputs[:2], value)
    return {"value": _r(value), "unit": "pp (wage growth minus productivity growth)", "rows": rows, "inputs": inputs, "status": st, "reason": why}


def read_breadth(series):
    head = field_of(series.get("cpiaucsl"), "ann3m")
    med = field_of(series.get("medcpim158sfrbcle"), "avg3")
    trm = field_of(series.get("trmmeancpim158sfrbcle"), "avg3")
    stk = field_of(series.get("corestickm159sfrbatl"), "value")
    flx = field_of(series.get("flexcpim159sfrbatl"), "value")
    g1 = None if (med is None or head is None) else med - head
    g2 = None if (trm is None or head is None) else trm - head
    g3 = None if (stk is None or flx is None) else stk - flx
    rows = [{"label": "Median CPI minus headline CPI (3-month, annual rates)", "value": _r(g1), "unit": "pp", "parts": [_r(med), _r(head)]},
            {"label": "Trimmed-mean CPI minus headline CPI (3-month, annual rates)", "value": _r(g2), "unit": "pp", "parts": [_r(trm), _r(head)]},
            {"label": "Sticky-price core minus flexible-price CPI, year over year", "value": _r(g3), "unit": "pp", "parts": [_r(stk), _r(flx)]}]
    inputs = [inp(series, "medcpim158sfrbcle", "avg3"), inp(series, "trmmeancpim158sfrbcle", "avg3"), inp(series, "cpiaucsl", "ann3m"),
              inp(series, "corestickm159sfrbatl"), inp(series, "flexcpim159sfrbatl")]
    value = g1
    st, why = _status(inputs[:3], value)
    return {"value": _r(value), "unit": "pp (median minus headline; positive = the middle of the basket runs hotter than the headline)",
            "rows": rows, "inputs": inputs, "status": st, "reason": why,
            "missing": "Share of CPI categories rising faster than 3% needs the BLS key."}


def read_fed(series, raw, path, today):
    top, bot = field_of(series.get("dfedtaru"), "value"), field_of(series.get("dfedtarl"), "value")
    mid = None if (top is None or bot is None) else (top + bot) / 2
    tay = field_of(series.get("atl_taylor"), "value")
    rows = [{"label": "Policy-range midpoint", "value": _r(mid), "unit": "%", "asof": (series.get("dfedtaru") or {}).get("asof")},
            {"label": "Taylor-rule prescription (Atlanta Fed, FOMCTaylor99UR)", "value": _r(tay), "unit": "%", "asof": (series.get("atl_taylor") or {}).get("asof")}]
    value = None
    c = (path or {}).get("contracts") or []
    if path and path.get("status") in ("ok", "stale") and c:
        for k, lab in [(0, "Futures, this month"), (min(6, len(c) - 1), "Futures, about 6 months out"), (min(12, len(c) - 1), "Futures, about 12 months out")]:
            rows.append({"label": lab + " (%s)" % c[k]["month"], "value": c[k]["implied"], "unit": "%", "asof": c[k]["date"]})
        if mid is not None:
            value = c[min(12, len(c) - 1)]["implied"] - mid
    sep = [(p, v) for p, v in (raw.get("fedtarmd") or []) if v is not None and p >= str(today.year)]
    for p, v in sep:
        rows.append({"label": "FOMC median projection, end of %s" % p, "value": _r(v), "unit": "%"})
    lr = field_of(series.get("fedtarmdlr"), "value")
    rows.append({"label": "FOMC median projection, longer run", "value": _r(lr), "unit": "%"})
    inputs = [inp(series, k) for k in ("dfedtaru", "dfedtarl", "atl_taylor", "fedtarmd", "fedtarmdlr")]
    inputs.append({"id": "@fed_path", "name": "Fed funds futures strip", "field": "implied", "value": None if not c else c[-1]["implied"],
                   "asof": (path or {}).get("asof"), "status": (path or {}).get("status", "missing")})
    st, why = _status([inputs[0], inputs[1], inputs[-1]], value)
    return {"value": _r(value), "unit": "pp (change priced over about 12 months against the policy-range midpoint)", "rows": rows,
            "inputs": inputs, "status": st, "reason": why}


def read_fiscal(series, raw):
    mts, intr, gdp = monthly(raw, "mtsds133fms"), monthly(raw, "interest_public"), dict(raw.get("gdp") or [])
    def sum12(m, p):
        w = [m.get(shift_month(p, -k)) for k in range(12)]
        return None if None in w else sum(w)

    def gdp_at(p):
        q = "%s-Q%d" % (p[:4], (int(p[5:7]) - 1) // 3 + 1)
        ks = sorted(k for k, v in gdp.items() if v is not None and k <= q)
        return gdp[ks[-1]] * 1000.0 if ks else None          # billions -> millions

    def share(p):
        d, i, g = sum12(mts, p), sum12(intr, p), gdp_at(p)
        if None in (d, i, g):
            return None, None, None
        deficit = -d
        return (deficit - i) / g * 100.0, deficit / g * 100.0, deficit - i
    cands = sorted(p for p in mts if mts.get(p) is not None and intr.get(p) is not None)
    p0 = cands[-1] if cands else None
    value, rows = None, []
    if p0:
        s0, t0, prim = share(p0)
        s1, t1, _ = share(shift_month(p0, -12))
        if s0 is not None and s1 is not None:
            value = s0 - s1
        rows = [{"label": "Primary deficit, 12 months to %s, share of GDP" % p0, "value": _r(s0), "unit": "% of GDP"},
                {"label": "Same, 12 months earlier", "value": _r(s1), "unit": "% of GDP"},
                {"label": "Total deficit, 12 months to %s, share of GDP" % p0, "value": _r(t0), "unit": "% of GDP"},
                {"label": "Primary deficit, 12 months to %s" % p0, "value": _r(None if prim is None else prim / 1000.0, 1), "unit": "billions of dollars"}]
    ref = series.get("customs_refunds") or {}
    rows.append({"label": "Tariff refunds paid to importers, 12 months (a one-off, shown apart from the impulse)",
                 "value": _r(None if field_of(ref, "sum12") is None else field_of(ref, "sum12") / 1000.0, 1), "unit": "billions of dollars", "asof": ref.get("asof")})
    inputs = [inp(series, "mtsds133fms"), inp(series, "interest_public"), inp(series, "gdp"), inp(series, "customs_refunds", "sum12")]
    st, why = _status(inputs[:3], value)
    return {"value": _r(value), "unit": "pp of GDP (positive = fiscal policy putting more into demand than a year earlier)", "asof": p0,
            "rows": rows, "inputs": inputs, "status": st, "reason": why}


def read_size(series, prices):
    a, b = field_of(series.get("drtscis"), "value"), field_of(series.get("drtscilm"), "value")
    sl = None if (a is None or b is None) else a - b
    rows = [{"label": "Banks tightening to small firms minus to large and mid firms", "value": _r(sl), "unit": "net %", "parts": [_r(a), _r(b)],
             "asof": (series.get("drtscis") or {}).get("asof")},
            {"label": "Business applications, year over year", "value": _r(field_of(series.get("babatotalsaus"), "yoy")), "unit": "%",
             "asof": (series.get("babatotalsaus") or {}).get("asof")},
            {"label": "Payrolls at firms with 1-19 employees minus 500+ (ADP)", "value": None, "unit": "pp",
             "reason": "ADP data are third-party copyright and are not published from this public repo"}]
    legs = [x for x in (ratio_leg(prices, "IWM", "SPY"), ratio_leg(prices, "IJR", "IJH")) if x]
    inputs = [inp(series, k) for k in ("drtscis", "drtscilm")] + [inp(series, "babatotalsaus", "yoy")]
    st, why = _status(inputs[:2], sl)
    return {"value": _r(sl), "unit": "net % (small minus large tightening; positive = credit tighter for small firms)", "rows": rows,
            "legs": legs, "inputs": inputs, "status": st, "reason": why}


def read_cycle(series, prices):
    d2, es = field_of(series.get("dgs2"), "value"), field_of(series.get("estr"), "value")
    diff = None if (d2 is None or es is None) else d2 - es
    rows = [{"label": "Broad dollar, year over year", "value": _r(field_of(series.get("dtwexbgs"), "yoy")), "unit": "%", "asof": (series.get("dtwexbgs") or {}).get("asof")},
            {"label": "Real effective dollar (BIS), year over year", "value": _r(field_of(series.get("bis_reer_us"), "yoy")), "unit": "%", "asof": (series.get("bis_reer_us") or {}).get("asof")},
            {"label": "2-year Treasury minus euro overnight rate", "value": _r(diff), "unit": "pp", "parts": [_r(d2), _r(es)]}]
    for k, nm in [("oecd_cli_usa", "US"), ("oecd_cli_deu", "Germany"), ("oecd_cli_chn", "China")]:
        rows.append({"label": "OECD leading indicator, %s (100 = trend)" % nm, "value": _r(field_of(series.get(k), "value")), "unit": "index",
                     "change": _r(field_of(series.get(k), "change")), "asof": (series.get(k) or {}).get("asof")})
    legs = [x for x in (ratio_leg(prices, "EEM", "SPY"), ratio_leg(prices, "VGK", "SPY"), price_leg(prices, "EMLC"), price_leg(prices, "CEW")) if x]
    inputs = [inp(series, "dtwexbgs", "yoy"), inp(series, "bis_reer_us", "yoy"), inp(series, "dgs2"), inp(series, "estr")] + \
             [inp(series, k) for k in ("oecd_cli_usa", "oecd_cli_deu", "oecd_cli_chn")]
    st, why = _status(inputs[2:4], diff)
    return {"value": _r(diff), "unit": "pp (US 2-year yield minus the euro overnight rate)", "rows": rows, "legs": legs,
            "inputs": inputs, "status": st, "reason": why}


def read_capex(series):
    spec = [("dc_construction", "ann3m", "Data-centre construction, 3-month annualised"), ("neworder", "ann3m", "Core capital-goods orders, 3-month annualised"),
            ("pcu335311335311", "ann3m", "Transformer prices, 3-month annualised"), ("cusr0000sehf01", "yoy", "Electricity prices, year over year"),
            ("ces2000000003", "yoy", "Construction wages, year over year")]
    rows = [{"label": lab, "value": _r(field_of(series.get(k), f)), "unit": "%", "asof": (series.get(k) or {}).get("asof")} for k, f, lab in spec]
    value = field_of(series.get("dc_construction"), "ann3m")
    inputs = [inp(series, k, f) for k, f, _ in spec]
    st, why = _status(inputs[:1], value)
    return {"value": _r(value), "unit": "% (data-centre construction, 3-month annualised)", "rows": rows, "inputs": inputs, "status": st, "reason": why}


def compute_reads(cat, series, raw, path, prices, today):
    fns = {"labour_temp": lambda: read_labour(series, raw), "wages_productivity": lambda: read_wages(series),
           "inflation_breadth": lambda: read_breadth(series), "fed_reaction": lambda: read_fed(series, raw, path, today),
           "fiscal_impulse": lambda: read_fiscal(series, raw), "size_divergence": lambda: read_size(series, prices),
           "relative_cycle": lambda: read_cycle(series, prices), "capex_input": lambda: read_capex(series)}
    out = {}
    for d in cat.get("reads") or []:
        try:
            body = fns[d["id"]]()
        except Exception as ex:  # noqa: BLE001 - one read failing must not take the payload down
            body = {"value": None, "rows": [], "inputs": [], "status": "error", "reason": "computation failed (%s)" % type(ex).__name__}
        out[d["id"]] = dict({"id": d["id"], "title": d["title"], "formula": d["formula"], "limits": d["limits"]}, **body)
    return out


# ------------------------------------------------------------------ the seven views

def sits(test, v):
    if v is None:
        return "no data"
    if test["op"] == ">":
        return "with" if v > test["confirm"] else ("against" if v < test["refute"] else "neither")
    return "with" if v < test["confirm"] else ("against" if v > test["refute"] else "neither")


def side_of(results):
    have = [r for r in results if r != "no data"]
    if len(have) * 2 < len(results) or not have:
        return "insufficient"
    c, r = have.count("with"), have.count("against")
    if c == 0 and r == 0:
        return "neutral"
    if c - r >= 2:
        return "with"
    if c - r == 1:
        return "lean_with"
    if r - c >= 2:
        return "against"
    if r - c == 1:
        return "lean_against"
    return "mixed"


def compute_views(cat, series, reads):
    out = []
    codes = (cat.get("side_rule") or {}).get("codes") or {}
    for v in cat.get("views") or []:
        chips = []
        for t in v["tests"]:
            if t["series"].startswith("@"):
                rd = reads.get(t["series"][1:]) or {}
                val, asof, st, name = rd.get("value"), None, rd.get("status", "missing"), rd.get("title")
            else:
                rec = series.get(t["series"])
                val, asof, st, name = field_of(rec, t["field"]), (rec or {}).get("asof"), (rec or {}).get("status", "missing"), (rec or {}).get("name")
            if st == "error":
                val = None                         # a kept value from an earlier run never places a view
            chips.append({"series": t["series"], "field": t["field"], "label": t["label"], "name": name, "value": _r(val),
                          "asof": asof, "status": st, "op": t["op"], "confirm": t["confirm"], "refute": t["refute"], "sits": sits(t, val)})
        res = [c["sits"] for c in chips]
        side = side_of(res)
        line = "%d of %d tests sit with this view, %d against, %d neither%s." % (
            res.count("with"), len(res), res.count("against"), res.count("neither"),
            (", %d without data" % res.count("no data")) if res.count("no data") else "")
        out.append({"id": v["id"], "title": v["title"], "claim": v["claim"], "side": side, "side_text": codes.get(side, side),
                    "line": line, "tests": chips, "note": v.get("note"), "missing": v.get("missing")})
    return out
