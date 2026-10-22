#!/usr/bin/env python3
"""
AI-hardware thesis gauge: two dials and a quadrant, computed daily from market
prices in prices.db.

    python thesis_gauge.py                 # writes data/thesis_gauge.json
    python thesis_gauge.py --stdout        # print instead
    python thesis_gauge.py --db other.db   # read another copy of the database

THE FRAMEWORK
  Two dials.
    DEMAND    -- is AI-hardware demand accelerating / holding / fading?
    FINANCING -- is capital for the build open or tightening?
  Four quadrants from the two dial signs:
    A  demand up,      financing open       (build-out)
    B  demand up,      financing tightening (capital disciplines growth)
    C  demand fading,  financing tight      (bust)
    D  demand fading,  financing still open (overbuild)
  A dial inside the neutral band (|dial| < BAND) puts the day on an EDGE
  between two quadrants ("A/B" = demand up, financing neutral), or at the
  "center" when both dials are neutral. `quadrant` is then "neutral" and
  `zone` names the edge.

EVERY INPUT IS A MARKET PROXY
  Relative strength, credit and rates series and a breadth count -- what prices
  say about demand and financing, not the fundamentals themselves. Each input
  becomes a standardized score: z of the input against its OWN trailing 252
  sessions (>= 126 needed), sign-aligned so + = healthy for the thesis, clipped
  at +/-3. A dial is the plain mean of its available input scores (>= 3 needed).
  Every window is trailing; nothing here reads a bar after the day it scores.

THE EVENT HOOK (not market data, not computed here)
  Hard-data events -- hyperscaler capex guides raised or cut, memory contract
  prices -- belong in the dials too, but they live in a private earnings ledger
  this public repo never reads. ``combine(market_z, events)`` is the one place
  they enter: pass the payload's per-input scores plus a list of
      {"dial": "demand"|"financing", "key": str, "z": float, "asof": "YYYY-MM-DD",
       "source": str}
  and it returns the dials and the quadrant with the events averaged in as
  extra inputs (same clip, same band). The payload's ``event_inputs`` block
  documents the schema and is always empty here.

STATES, NOT FORECASTS
  The `evidence` block measures, over the backfilled history, how long each
  label lasts and what SMH did over the next 21 / 63 sessions in each, with N
  in days AND episodes. A label earns `edge` only with >= MIN_EP episodes and a
  calendar-block bootstrap interval that excludes zero; otherwise the payload
  says the labels are states, not forecasts. The history is ~2 years, so
  expect the latter.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import regime as RG

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "prices.db")
OUT = os.path.join(HERE, "data", "thesis_gauge.json")

Z_WIN = 252          # trailing window each input is standardized against
Z_MIN = 126          # sessions of the input needed before it scores
Z_CLIP = 3.0
MIN_INPUTS = 3       # input scores a dial needs on a day before it reads
BAND = 0.25          # neutral band on each dial (mean of z scores)
HISTORY = 504        # ~2 years of daily history in the payload
FWD = (21, 63)
MIN_EP = 20          # episodes a label needs before it can earn "edge"
BLOCK = 63           # calendar block for the bootstrap
REPS = 1000
SEED = 22

QUADS = ("A", "B", "C", "D", "neutral")
QUAD_TEXT = {
    "A": "demand up, financing open (build-out)",
    "B": "demand up, financing tightening (capital disciplines growth)",
    "C": "demand fading, financing tight (bust)",
    "D": "demand fading, financing still open (overbuild)",
    "neutral": "at least one dial inside the neutral band",
}
# (demand sign, financing sign) -> zone
ZONES = {
    (1, 1): "A", (1, -1): "B", (-1, -1): "C", (-1, 1): "D",
    (1, 0): "A/B", (0, -1): "B/C", (-1, 0): "C/D", (0, 1): "A/D", (0, 0): "center",
}

# A fixed AI-hardware list for the breadth input. Fixed on purpose: adding a
# name changes the denominator, so the list is edited deliberately, never
# grown by whatever happens to be in prices.db.
AI_HW = ["NVDA", "AVGO", "AMD", "TSM", "MU", "MRVL", "ASML", "LRCX",
         "LITE", "COHR", "CIEN", "FN", "GLW", "VRT", "ALAB", "MPWR"]
EQUIP = ["LRCX", "AMAT", "KLAC", "ASML"]      # those present in prices.db

INPUTS = [
    # ---- demand ----
    {"key": "smh_spy_21", "dial": "demand", "kind": "rs", "a": "SMH", "b": "SPY", "n": 21, "sign": 1,
     "label": "Semis (SMH) vs SPY, 21d relative log return"},
    {"key": "smh_spy_63", "dial": "demand", "kind": "rs", "a": "SMH", "b": "SPY", "n": 63, "sign": 1,
     "label": "Semis (SMH) vs SPY, 63d relative log return"},
    {"key": "soxx_spy_21", "dial": "demand", "kind": "rs", "a": "SOXX", "b": "SPY", "n": 21, "sign": 1,
     "label": "Semis (SOXX) vs SPY, 21d relative log return"},
    {"key": "soxx_spy_63", "dial": "demand", "kind": "rs", "a": "SOXX", "b": "SPY", "n": 63, "sign": 1,
     "label": "Semis (SOXX) vs SPY, 63d relative log return"},
    {"key": "optics_spy_63", "dial": "demand", "kind": "rs", "a": "OPTICS", "b": "SPY", "n": 63, "sign": 1,
     "label": "Optics basket vs SPY, 63d relative log return"},
    {"key": "powersemi_spy_63", "dial": "demand", "kind": "rs", "a": "POWERSEMI", "b": "SPY", "n": 63, "sign": 1,
     "label": "Power-semis basket vs SPY, 63d relative log return"},
    {"key": "mu_smh_63", "dial": "demand", "kind": "rs", "a": "MU", "b": "SMH", "n": 63, "sign": 1,
     "label": "Memory (MU) vs SMH, 63d relative log return"},
    {"key": "dram_smh_63", "dial": "demand", "kind": "rs", "a": "DRAM", "b": "SMH", "n": 63, "sign": 1,
     "label": "Memory ETF (DRAM) vs SMH, 63d relative log return"},
    {"key": "equip_smh_63", "dial": "demand", "kind": "rs", "a": "EQUIP", "b": "SMH", "n": 63, "sign": 1,
     "label": "Semi-equipment basket vs SMH, 63d relative log return"},
    {"key": "ai_breadth_50", "dial": "demand", "kind": "breadth", "names": AI_HW, "n": 50, "sign": 1,
     "label": "Share of a fixed AI-hardware list above its 50-day average"},
    # ---- financing ----
    {"key": "neo_hyper_63", "dial": "financing", "kind": "rs", "a": "NEOCLOUD", "b": "HYPERSCALE", "n": 63,
     "sign": 1, "label": "Neocloud basket vs hyperscaler basket, 63d relative log return"},
    {"key": "crwv_spy_63", "dial": "financing", "kind": "rs", "a": "CRWV", "b": "SPY", "n": 63, "sign": 1,
     "label": "CRWV vs SPY, 63d relative log return"},
    {"key": "orcl_spy_63", "dial": "financing", "kind": "rs", "a": "ORCL", "b": "SPY", "n": 63, "sign": 1,
     "label": "ORCL vs SPY, 63d relative log return"},
    {"key": "hyg_ief_63", "dial": "financing", "kind": "rs", "a": "HYG", "b": "IEF", "n": 63, "sign": 1,
     "label": "Credit: HYG vs IEF, 63d relative log return"},
    {"key": "tnx_level", "dial": "financing", "kind": "level", "a": "^TNX", "sign": -1,
     "label": "10-year yield level (higher = tighter)"},
    {"key": "tnx_chg_21", "dial": "financing", "kind": "chg", "a": "^TNX", "n": 21, "sign": -1,
     "label": "10-year yield, 21d change in percentage points (rising = tighter)"},
    {"key": "neocloud_dd_63", "dial": "financing", "kind": "dd", "a": "NEOCLOUD", "n": 63, "sign": 1,
     "label": "Neocloud basket drawdown from its 63d high"},
]
DIALS = ("demand", "financing")
BASKET_KEYS = ("OPTICS", "POWERSEMI", "NEOCLOUD", "HYPERSCALE")

NOTE = ("AI-hardware thesis gauge. Every input is a MARKET PROXY (relative strength, credit, "
        "rates, breadth), standardized against its own trailing 252 sessions and sign-aligned "
        "so + = healthy for the thesis. Hard-data events (capex guides, memory contract prices) "
        "are not read here; see event_inputs. The quadrant is a description of the tape, not a "
        "forecast; see evidence.")


# ===========================================================================
# legs and inputs
# ===========================================================================

def basket_members():
    out = {e["t"]: list(e["basket"]) for e in RG.REG_ETFS if e.get("basket") and e["t"] in BASKET_KEYS}
    out["EQUIP"] = list(EQUIP)
    return out


def legs(series):
    """Every price leg an input can name, on the panel axis: tickers as-is,
    baskets via regime.basket_series over the members present. Returns
    (legs, members_present)."""
    out = dict(series)
    present = {}
    for k, mem in basket_members().items():
        have = [m for m in mem if series.get(m)]
        present[k] = have
        b = RG.basket_series(series, have) if have else None
        if b is not None:
            # basket_series carries the prior level on a day with no member bar,
            # so a basket whose members have ALL gone stale would read as a flat
            # line. End it where the last member stopped.
            cur = [m for m in have if not RG._series_stale(series[m])]
            if not cur:
                last = max(max((i for i, v in enumerate(series[m]) if v is not None), default=-1)
                           for m in have)
                b = [v if i <= last else None for i, v in enumerate(b)]
            out[k] = b
    return out, present


def _f(arr):
    return pd.Series([np.nan if v is None else float(v) for v in arr], dtype=float)


def raw_series(inp, L):
    """The input's raw daily series (pd.Series on the axis) or None when a leg
    is absent from prices.db."""
    k = inp["kind"]
    if k == "breadth":
        cols = []
        for t in inp["names"]:
            if L.get(t):
                px = _f(L[t])
                sma = px.rolling(inp["n"], min_periods=inp["n"]).mean()
                ok = px.notna() & sma.notna()
                cols.append(((px > sma) & ok).astype(float).where(ok))
        if not cols:
            return None
        df = pd.concat(cols, axis=1)
        cnt = df.notna().sum(axis=1)
        return (df.sum(axis=1) / cnt).where(cnt >= 5)
    a = L.get(inp["a"])
    if not a:
        return None
    A = _f(a)
    if k == "level":
        return A
    if k == "chg":
        return A - A.shift(inp["n"])
    if k == "dd":
        return A / A.rolling(inp["n"], min_periods=21).max() - 1.0
    b = L.get(inp["b"])
    if not b:
        return None
    B = _f(b)
    n = inp["n"]
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.log(A).diff(n) - np.log(B).diff(n)


def zscore(raw, sign):
    """z of the raw series against its own trailing Z_WIN sessions (inclusive),
    sign-aligned, clipped. Trailing only: z[t] reads raw[t-Z_WIN+1 .. t]."""
    m = raw.rolling(Z_WIN, min_periods=Z_MIN).mean()
    s = raw.rolling(Z_WIN, min_periods=Z_MIN).std(ddof=0)
    z = (raw - m) / s.where(s > 0)
    return (sign * z).clip(-Z_CLIP, Z_CLIP)


def input_legs(inp):
    if inp["kind"] == "breadth":
        return list(inp["names"])
    return [x for x in (inp.get("a"), inp.get("b")) if x]


def leg_status(inp, series, present):
    """'missing' when a leg is not in prices.db at all; 'stale' when a leg
    printed bars and stopped (regime._series_stale, i.e. past the panel's
    MAX_STALE_SESSIONS cutoff); else 'ok'. Baskets are stale when fewer than
    half their present members still print."""
    if inp["kind"] == "breadth":
        have = [t for t in inp["names"] if series.get(t)]
        if not have:
            return "missing", []
        dead = [t for t in have if RG._series_stale(series[t])]
        return ("stale" if len(dead) * 2 > len(have) else "ok"), dead
    dead = []
    for leg in input_legs(inp):
        if leg in present:
            mem = present[leg]
            if not mem:
                return "missing", []
            gone = [m for m in mem if RG._series_stale(series[m])]
            dead += gone
            if len(gone) * 2 > len(mem):
                return "stale", dead
        else:
            if not series.get(leg):
                return "missing", []
            if RG._series_stale(series[leg]):
                return "stale", [leg]
    return "ok", dead


# ===========================================================================
# dials and quadrant
# ===========================================================================

def sgn(x, band=BAND):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    return 1 if x > band else (-1 if x < -band else 0)


def zone_of(demand, financing, band=BAND):
    sd, sf = sgn(demand, band), sgn(financing, band)
    if sd is None or sf is None:
        return None
    return ZONES[(sd, sf)]


def quadrant_of(zone):
    if zone is None:
        return None
    return zone if zone in ("A", "B", "C", "D") else "neutral"


def dial_mean(zs, min_inputs=MIN_INPUTS):
    v = [float(z) for z in zs if z is not None and not (isinstance(z, float) and math.isnan(z))]
    if len(v) < min_inputs:
        return None, len(v)
    return sum(v) / len(v), len(v)


def combine(market_z, events=None, band=BAND, min_inputs=MIN_INPUTS):
    """THE EVENT HOOK. `market_z` = {input key: z or None} (the payload's
    inputs[*].z); `events` = [{"dial", "key", "z", ...}] from a private ledger.
    Events are clipped like any input and averaged into their dial as extra
    inputs. Returns {"demand", "financing", "n", "zone", "quadrant"}."""
    by = {d: [] for d in DIALS}
    dial_of = {i["key"]: i["dial"] for i in INPUTS}
    for k, z in (market_z or {}).items():
        if k in dial_of:
            by[dial_of[k]].append(z)
    for e in events or []:
        d = e.get("dial")
        z = e.get("z")
        if d in by and isinstance(z, (int, float)) and not math.isnan(z):
            by[d].append(max(-Z_CLIP, min(Z_CLIP, float(z))))
    out = {"n": {}}
    for d in DIALS:
        out[d], out["n"][d] = dial_mean(by[d], min_inputs)
    out["zone"] = zone_of(out["demand"], out["financing"], band)
    out["quadrant"] = quadrant_of(out["zone"])
    return out


def compute(dates, series):
    """The whole gauge over the panel. Returns a dict of pandas objects:
    raw (DataFrame), z (DataFrame), dial frames, zone/quadrant Series and the
    per-input status for the last day."""
    L, present = legs(series)
    raw, z, status = {}, {}, {}
    for inp in INPUTS:
        st, dead = leg_status(inp, series, present)
        r = raw_series(inp, L) if st != "missing" else None
        if r is None:
            st = "missing"
            r = pd.Series(np.nan, index=range(len(dates)))
        raw[inp["key"]] = r
        z[inp["key"]] = zscore(r, inp["sign"])
        status[inp["key"]] = {"status": st, "stale_legs": dead}
    R = pd.DataFrame(raw)
    Z = pd.DataFrame(z)
    R.index = Z.index = pd.Index(dates)
    # A stale input scores nothing on the last day even inside the panel's
    # forward-fill grace (where its ratio is a flat line, not a reading).
    for inp in INPUTS:
        if status[inp["key"]]["status"] == "stale":
            Z.iloc[-1, Z.columns.get_loc(inp["key"])] = np.nan
    dial = {}
    nin = {}
    for d in DIALS:
        cols = [i["key"] for i in INPUTS if i["dial"] == d]
        cnt = Z[cols].notna().sum(axis=1)
        dial[d] = Z[cols].mean(axis=1).where(cnt >= MIN_INPUTS)
        nin[d] = cnt
    # plain lists: a pandas Series would turn None into NaN
    zone = [zone_of(a, b) for a, b in zip(dial["demand"], dial["financing"])]
    quad = [quadrant_of(x) for x in zone]
    return {"raw": R, "z": Z, "dial": dial, "n": nin, "zone": zone, "quad": quad,
            "status": status, "present": present, "legs": L}


# ===========================================================================
# evidence
# ===========================================================================

def episodes(labels):
    """Run ids: consecutive equal non-null labels share an id; None -> -1."""
    ids = []
    cur = -1
    prev = object()
    for x in labels:
        if x is None:
            ids.append(-1)
            prev = object()
            continue
        if x != prev:
            cur += 1
            prev = x
        ids.append(cur)
    return ids


def persistence(labels):
    lab = [x for x in labels]
    pairs1 = [(a, b) for a, b in zip(lab, lab[1:]) if a is not None and b is not None]
    out = {"next_day_same": _r(sum(a == b for a, b in pairs1) / len(pairs1), 3) if pairs1 else None}
    for h in FWD:
        p = [(a, b) for a, b in zip(lab, lab[h:]) if a is not None and b is not None]
        out[f"same_after_{h}"] = _r(sum(a == b for a, b in p) / len(p), 3) if p else None
    ids = episodes(lab)
    runs = {}
    for x, i in zip(lab, ids):
        if i >= 0:
            runs.setdefault(i, [x, 0])
            runs[i][1] += 1
    by = {}
    for x, n in runs.values():
        by.setdefault(x, []).append(n)
    out["episodes"] = len(runs)
    out["median_episode_sessions"] = {k: float(np.median(v)) for k, v in sorted(by.items())}
    return out


def _r(x, d=5):
    if x is None:
        return None
    x = float(x)
    return None if math.isnan(x) else round(x, d)


def block_boot_diff(fwd, mask, rng, reps=REPS, block=BLOCK):
    """90% interval of median(fwd | mask) - median(fwd) under a moving-block
    bootstrap of the session axis (blocks of `block` consecutive sessions, so
    the heavy overlap of forward windows is resampled together)."""
    n = len(fwd)
    if n < block or mask.sum() == 0:
        return None
    starts = np.arange(0, n - block + 1)
    k = int(math.ceil(n / block))
    diffs = []
    for _ in range(reps):
        idx = np.concatenate([np.arange(s, s + block) for s in rng.choice(starts, size=k)])[:n]
        f, m = fwd[idx], mask[idx]
        if m.sum() == 0:
            continue
        diffs.append(np.median(f[m]) - np.median(f))
    if len(diffs) < reps // 2:
        return None
    return [_r(np.percentile(diffs, 5)), _r(np.percentile(diffs, 95))]


def evidence(dates, quad, smh, spy, reps=REPS):
    """What followed each quadrant label on this history, with N."""
    lab = [None if (q is None or (isinstance(q, float) and math.isnan(q))) else q for q in quad]
    first = next((i for i, q in enumerate(lab) if q is not None), None)
    if first is None:
        return {"verdict": "no labelled history", "by_quadrant": {}}
    S = _f(smh).values
    P = _f(spy).values
    rng = np.random.default_rng(SEED)
    ids = episodes(lab)
    out = {}
    edges = []
    base = {}
    for h in FWD:
        n = len(S)
        fwd = np.full(n, np.nan)
        rel = np.full(n, np.nan)
        with np.errstate(divide="ignore", invalid="ignore"):
            fwd[:n - h] = np.log(S[h:] / S[:n - h])
            rel[:n - h] = fwd[:n - h] - np.log(P[h:] / P[:n - h])
        ok = np.array([lab[i] is not None for i in range(n)]) & ~np.isnan(fwd)
        okr = ok & ~np.isnan(rel)
        base[f"fwd{h}"] = {"days": int(ok.sum()),
                           "median_smh": _r(np.median(fwd[ok])) if ok.any() else None,
                           "median_smh_vs_spy": _r(np.median(rel[okr])) if okr.any() else None,
                           "share_pos": _r((fwd[ok] > 0).mean(), 3) if ok.any() else None}
        sel = np.where(ok)[0]
        f_ok = fwd[sel]
        for q in QUADS:
            m = np.array([lab[i] == q for i in sel])
            cell = out.setdefault(q, {"days_labelled": sum(1 for x in lab if x == q),
                                      "episodes": len({ids[i] for i in range(len(lab)) if lab[i] == q})})
            if not m.any():
                cell[f"fwd{h}"] = {"days": 0, "episodes": 0}
                continue
            ep = len({ids[i] for i in sel[m]})
            mr = m & ~np.isnan(rel[sel])
            c = {"days": int(m.sum()), "episodes": ep,
                 "median_smh": _r(np.median(f_ok[m])),
                 "median_smh_vs_spy": _r(np.median(rel[sel][mr])) if mr.any() else None,
                 "share_pos": _r((f_ok[m] > 0).mean(), 3),
                 "diff_vs_all": _r(np.median(f_ok[m]) - np.median(f_ok))}
            ci = block_boot_diff(f_ok, m, rng, reps=reps)
            c["ci90_diff"] = ci
            earned = ep >= MIN_EP and ci is not None and (ci[0] > 0 or ci[1] < 0)
            c["tag"] = "edge" if earned else "descriptive"
            if earned:
                edges.append({"quadrant": q, "h": h})
            cell[f"fwd{h}"] = c
    verdict = ("states, not forecasts: no quadrant label shows a forward SMH edge on this history "
               f"(edge needs >= {MIN_EP} episodes and a {BLOCK}-session block-bootstrap 90% interval "
               "excluding 0)") if not edges else \
        "some labels cleared the edge rule on a short history; read with care (see edges)"
    return {
        "window": {"labels_from": dates[first], "to": dates[-1], "sessions": len(dates) - first},
        "verdict": verdict,
        "edges": edges,
        "persistence": persistence(lab[first:]),
        "baseline": base,
        "by_quadrant": out,
        "method": {
            "outcome": "forward 21d / 63d SMH log return from each labelled day (and SMH minus SPY)",
            "episode": "consecutive sessions with the same quadrant label = one episode",
            "bootstrap": f"{reps} reps, seed {SEED}, moving {BLOCK}-session blocks of the axis; "
                         "interval on median(label) - median(all labelled days)",
            "edge_rule": f">= {MIN_EP} episodes AND the 90% interval excludes 0",
        },
        "notes": [
            "Labels are STATES, not forecasts. Every number here describes what followed them on "
            "this short history; none is a prediction.",
            "Overlap: consecutive days share almost all of their forward window, so days overstate "
            "the sample; episodes are the honest unit.",
            "The history is the prices.db backfill (about two years, and less for inputs whose "
            "tickers were added later), a single cycle in which semis mostly rose. Survivorship: "
            "the baskets and the breadth list were chosen in 2026 for what the names became.",
        ],
    }


# ===========================================================================
# payload
# ===========================================================================

def _since(labels):
    """(start index, sessions) of the run that ends on the last label."""
    if not labels or labels[-1] is None:
        return None, 0
    i = len(labels) - 1
    while i > 0 and labels[i - 1] == labels[-1]:
        i -= 1
    return i, len(labels) - i


def build(conn, reps=REPS):
    dates, series = RG.build_panel(conn)
    if not dates:
        raise SystemExit("prices.db has no reference axis (SPY)")
    G = compute(dates, series)
    Z, R = G["z"], G["raw"]
    last = len(dates) - 1

    inputs = []
    market_z = {}
    for inp in INPUTS:
        k = inp["key"]
        st = G["status"][k]
        zv = _r(Z[k].iloc[-1], 3)
        rv = _r(R[k].iloc[-1], 5)
        status = st["status"]
        if status == "ok" and zv is None:
            status = "warming" if R[k].notna().any() else "missing"
        market_z[k] = zv
        row = {"key": k, "dial": inp["dial"], "label": inp["label"],
               "legs": input_legs(inp) if inp["kind"] != "breadth" else ["AI_HW list"],
               "sign": inp["sign"], "raw": rv, "z": zv, "status": status}
        if inp.get("n"):
            row["window"] = inp["n"]
        if st["stale_legs"]:
            row["stale_legs"] = st["stale_legs"]
        inputs.append(row)

    today = combine(market_z)
    zones = list(G["zone"])
    i0, run = _since(zones)
    lo = max(0, len(dates) - HISTORY)

    def col(s, d=3):
        return [_r(v, d) for v in list(s)[lo:]]

    smh = G["legs"].get("SMH") or [None] * len(dates)
    hist = {
        "dates": dates[lo:],
        "demand": col(G["dial"]["demand"]),
        "financing": col(G["dial"]["financing"]),
        "n_demand": [int(v) for v in list(G["n"]["demand"])[lo:]],
        "n_financing": [int(v) for v in list(G["n"]["financing"])[lo:]],
        "zone": zones[lo:],
        "quadrant": list(G["quad"])[lo:],
        "smh": [_r(v, 4) for v in smh[lo:]],
        "inputs_z": {i["key"]: col(Z[i["key"]]) for i in INPUTS},
    }
    ev = evidence(dates[lo:], list(G["quad"])[lo:], smh[lo:],
                  (G["legs"].get("SPY") or [None] * len(dates))[lo:], reps=reps)

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "asof": dates[last],
        "note": NOTE,
        "quadrant": today["quadrant"],
        "zone": today["zone"],
        "zone_text": QUAD_TEXT.get(today["quadrant"]) if today["quadrant"] != "neutral"
        else ("on the %s edge" % today["zone"] if today["zone"] != "center" else "both dials neutral"),
        "zone_since": dates[i0] if i0 is not None else None,
        "zone_sessions": run,
        "dials": {
            "demand": _r(today["demand"], 3),
            "financing": _r(today["financing"], 3),
            "n_demand": today["n"]["demand"],
            "n_financing": today["n"]["financing"],
        },
        "method": {
            "z": f"each input vs its own trailing {Z_WIN} sessions (>= {Z_MIN}), sign-aligned so "
                 f"+ = healthy, clipped at +/-{Z_CLIP:g}",
            "dial": f"plain mean of the available input z scores; >= {MIN_INPUTS} needed",
            "band": f"neutral band |dial| < {BAND:g}: a neutral dial puts the day on an edge "
                    "(A/B, B/C, C/D, A/D) or the center",
            "quadrants": QUAD_TEXT,
            "stale": f"an input whose leg stopped printing (regime._series_stale, past the "
                     f"{RG.MAX_STALE_SESSIONS}-session cutoff) scores nothing; a basket is stale "
                     "when over half its members are",
            "baskets": {k: v for k, v in G["present"].items()},
            "ai_hw_list": AI_HW,
        },
        "inputs": inputs,
        "event_inputs": {
            "status": "hook only: nothing is read here",
            "schema": {"dial": "demand | financing", "key": "short id", "z": "score, + = healthy",
                       "asof": "YYYY-MM-DD", "source": "where the event came from"},
            "merge": "thesis_gauge.combine(market_z, events): events join their dial as extra inputs",
            "applied": [],
        },
        "history": hist,
        "evidence": ev,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="AI-hardware thesis gauge (two dials, one quadrant)")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--stdout", action="store_true")
    ap.add_argument("--reps", type=int, default=REPS)
    a = ap.parse_args(argv)
    import pathlib
    import sqlite3
    conn = sqlite3.connect(pathlib.Path(os.path.abspath(a.db)).as_uri() + "?mode=ro", uri=True)
    try:
        out = build(conn, reps=a.reps)
    finally:
        conn.close()
    # one field per line (indent=0): the repo's pre-commit guard reads line by line
    text = json.dumps(out, ensure_ascii=False, indent=0, separators=(",", ":"), allow_nan=False)
    if a.stdout:
        sys.stdout.write(text + "\n")
        return 0
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text + "\n")
    os.replace(tmp, a.out)
    d = out["dials"]
    print(f"thesis gauge {out['asof']}: demand {d['demand']} (n {d['n_demand']}), "
          f"financing {d['financing']} (n {d['n_financing']}) -> {out['zone']} "
          f"since {out['zone_since']}; evidence: {out['evidence']['verdict'][:60]}")
    print(f"Wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
