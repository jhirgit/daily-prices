#!/usr/bin/env python3
"""
SPEC-86 Part D, on demand: what followed the phase labels and the check
statuses the sector ladder shows, on 25+ years of history.

    python rotation_evidence.py            # reuse data/cache/rotation_history.csv.gz
    python rotation_evidence.py --refresh  # re-download every series first

Writes data/rotation_evidence.json (small, committed). The long price history
lives in data/cache/ (gitignored, reproducible by re-running); prices.db is
never extended.

ONE IMPLEMENTATION. Every state here comes from rotation_phase.py's
``phase_frame`` / ``labels`` / ``measure`` / ``status_codes`` /
``status_word_array`` / ``ext_frame`` -- the same functions the daily run
calls -- so the evidence describes exactly what the board shows. They read
trailing windows only; ``lookahead_check`` below re-proves it on a real row
every run (mutate every bar after d, states at <= d must not move, and a
positive control must).

WHAT IS MEASURED (spec s7)
  * outcomes: the row's forward 21d / 63d LOG return minus SPY's over the same
    sessions ("relative"), and persistence = the forward 63d ABSOLUTE return
    has the sign of the 126d trend (up / down days only)
  * phase base rates per label, pooled over the primaries (SPY excluded: its
    relative return is identically zero; twins excluded: near-duplicates) and
    per family; days, EPISODES (consecutive days = one), median forward 63d
    relative return, share positive, persistence
  * check evidence per kind x family, up / down / flat trends separately:
    forward 63d relative return when confirming vs diverging (flat: leaning up
    vs leaning down), the difference of medians, and TWO bootstrap 90%
    intervals -- blocks = episodes (the spec's), and blocks = 63-session
    calendar blocks shared by every row (rows move together: 2008 is one draw,
    not sixty). ``edge`` requires both to exclude 0 in the thesis' direction
    with >= 20 episodes a side; everything else is ``descriptive``
  * per row: episodes of each label and what followed, with n
  * holdings-based checks (participation, concentration, split) are NOT
    calibrated -- there is no holdings history -- and say so
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
import rotation_phase as RP
import rotation_theses as TH

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "data", "cache")
CACHE = os.path.join(CACHE_DIR, "rotation_history.csv.gz")
OUT = os.path.join(HERE, "data", "rotation_evidence.json")

START = "1998-01-01"
REPS = 1000
SEED = 86
BLOCK = 63            # calendar block length (sessions) for the time bootstrap
MIN_EP = 20           # episodes a side before a cell can earn "edge"
FWD = (21, 63)
BASKET_MIN_MEMBERS = 4
CALIBRATED_KINDS = ("driver", "ratio", "curve", "level")


# ===========================================================================
# history
# ===========================================================================

def universe():
    syms = {"SPY"}
    for e in RG.REG_ETFS:
        if e.get("basket"):
            syms.update(e["basket"])
        else:
            syms.add(e["t"])
    for s in TH.price_inputs():
        if s in TH.BASKETS:
            syms.update(TH.BASKETS[s])
        else:
            syms.add(s)
    return syms


def load_history(refresh=False, chunk=40):
    """Wide DataFrame (date index, one column per symbol) of adjusted closes
    from START. Missing symbols are fetched and merged into the cache."""
    want = universe()
    have = pd.DataFrame()
    if os.path.exists(CACHE) and not refresh:
        long = pd.read_csv(CACHE, parse_dates=["date"])
        have = long.pivot(index="date", columns="ticker", values="close")
    missing = sorted(want - set(have.columns))
    errs = {}
    if missing:
        frames = []
        for i in range(0, len(missing), chunk):
            got, e = RP.fetch_yf(missing[i:i + chunk], start=START)
            errs.update(e)
            for s, ser in got.items():
                ser = ser.copy()
                ser.index = pd.to_datetime(ser.index).tz_localize(None) \
                    if getattr(ser.index, "tz", None) else pd.to_datetime(ser.index)
                frames.append(ser.rename(s))
        if frames:
            new = pd.concat(frames, axis=1)
            fresh = new[[c for c in new.columns if c not in have.columns]]
            have = new if have.empty else pd.concat([have, fresh], axis=1)
        os.makedirs(CACHE_DIR, exist_ok=True)
        long = have.stack().rename("close").reset_index()
        long.columns = ["date", "ticker", "close"]
        long.to_csv(CACHE, index=False, compression="gzip", float_format="%.6g")
    return have.sort_index(), errs


def align_all(wide):
    spy = wide["SPY"].dropna()
    dates = [d.strftime("%Y-%m-%d") for d in spy.index]
    out = {}
    for s in wide.columns:
        out[s] = RP.align_to_axis(dates, wide[s].copy())
    return dates, out


def basket_hist(aligned, members):
    """regime.basket_series over the members, from the first session at least
    BASKET_MIN_MEMBERS of them have a price (spec s7.1)."""
    arrs = [aligned[m] for m in members if m in aligned]
    if len(arrs) < BASKET_MIN_MEMBERS:
        return None, None
    cnt = np.sum([~np.isnan(a) for a in arrs], axis=0)
    ok = np.where(cnt >= BASKET_MIN_MEMBERS)[0]
    if not len(ok):
        return None, None
    start = int(ok[0])
    ser = {}
    for m, a in zip([m for m in members if m in aligned], arrs):
        b = a.copy()
        b[:start] = np.nan
        ser[m] = [None if math.isnan(v) else float(v) for v in b]
    bs = RG.basket_series(ser, list(ser.keys()))
    return (RP._arr(bs) if bs else None), start


class Resolver:
    def __init__(self, aligned):
        self.a = aligned
        self.cache = {}
        self.basket_start = {}

    def get(self, sym):
        if sym in self.cache:
            return self.cache[sym]
        if sym in TH.BASKETS:
            v, st = basket_hist(self.a, TH.BASKETS[sym])
            self.basket_start[sym] = st
        else:
            v = self.a.get(sym)
        self.cache[sym] = v
        return v


def check_arrays(c, res):
    xs = []
    for s in c["inputs"]:
        a = res.get(s)
        if a is None:
            return None
        xs.append(a)
    if c.get("fx"):
        fx = res.get(c["fx"])
        if fx is None:
            return None
        xs[0] = xs[0] * fx
    return xs


# ===========================================================================
# statistics
# ===========================================================================

def run_ids(keys, valid):
    """Episode ids: a new id whenever the key changes or validity breaks."""
    n = len(keys)
    ids = np.full(n, -1, dtype=np.int64)
    cur = -1
    prev = None
    for i in range(n):
        if not valid[i]:
            prev = None
            continue
        k = keys[i]
        if prev is None or k != prev:
            cur += 1
        ids[i] = cur
        prev = k
    return ids


def _offset(ids, base):
    """Make per-series episode ids globally unique: valid ids shift by `base`;
    returns (ids, next base). Invalid (-1) stays -1 and is never selected."""
    out = np.where(ids >= 0, ids + base, -1)
    nxt = base + int(ids.max()) + 1 if len(ids) and ids.max() >= 0 else base
    return out, nxt


def _wmed_setup(vals, groups):
    order = np.argsort(vals, kind="stable")
    uniq, inv = np.unique(groups, return_inverse=True)
    return vals[order], inv[order], len(uniq)


def _wmed(v, g, counts):
    w = counts[g]
    cw = np.cumsum(w)
    tot = cw[-1] if len(cw) else 0
    if tot <= 0:
        return np.nan
    return v[min(np.searchsorted(cw, tot / 2.0), len(v) - 1)]


def boot_median(vals, eps, blocks, rng, reps=None):
    """90% intervals for the median of `vals`: resampling episodes, and
    resampling calendar blocks."""
    reps = reps or REPS
    v, g, k = _wmed_setup(vals, eps)
    ve, gb, kb = _wmed_setup(vals, blocks)
    a = np.empty(reps)
    b = np.empty(reps)
    for r in range(reps):
        a[r] = _wmed(v, g, np.bincount(rng.integers(0, k, k), minlength=k))
        b[r] = _wmed(ve, gb, np.bincount(rng.integers(0, kb, kb), minlength=kb))
    return _ci(a), _ci(b)


def boot_diff(v1, e1, b1, v2, e2, b2, rng, reps=None):
    """90% intervals for median(v1) - median(v2): (i) episodes resampled within
    each group independently; (ii) calendar blocks resampled once per rep and
    applied to both groups (they share the calendar)."""
    reps = reps or REPS
    s1 = _wmed_setup(v1, e1)
    s2 = _wmed_setup(v2, e2)
    allb, inv = np.unique(np.concatenate([b1, b2]), return_inverse=True)
    kb = len(allb)
    bi1, bi2 = inv[:len(b1)], inv[len(b1):]
    o1 = np.argsort(v1, kind="stable")
    o2 = np.argsort(v2, kind="stable")
    t1v, t1g = v1[o1], bi1[o1]
    t2v, t2g = v2[o2], bi2[o2]
    ep = np.empty(reps)
    tm = np.empty(reps)
    for r in range(reps):
        c1 = np.bincount(rng.integers(0, s1[2], s1[2]), minlength=s1[2])
        c2 = np.bincount(rng.integers(0, s2[2], s2[2]), minlength=s2[2])
        ep[r] = _wmed(s1[0], s1[1], c1) - _wmed(s2[0], s2[1], c2)
        cb = np.bincount(rng.integers(0, kb, kb), minlength=kb)
        tm[r] = _wmed(t1v, t1g, cb) - _wmed(t2v, t2g, cb)
    return _ci(ep), _ci(tm)


def _ci(x):
    x = x[~np.isnan(x)]
    if len(x) < 10:
        return None
    return [round(float(np.percentile(x, 5)), 5), round(float(np.percentile(x, 95)), 5)]


def _r(x, d=5):
    return RP._num(x, d)


def label_cell(rel63, rel21, abs63, trend, eps, blocks, rng, boot=True):
    ok = ~np.isnan(rel63)
    if not np.any(ok):
        return None
    v = rel63[ok]
    e = eps[ok]
    cell = {"days": int(ok.sum()), "episodes": int(len(np.unique(e))),
            "med_rel63": _r(np.median(v)), "share_pos": _r(np.mean(v > 0), 3),
            "med_rel21": _r(np.nanmedian(rel21[ok])) if np.any(~np.isnan(rel21[ok])) else None}
    tr = trend[ok]
    ab = abs63[ok]
    pm = (tr != 0) & ~np.isnan(tr) & ~np.isnan(ab)
    cell["persist"] = _r(np.mean(np.sign(ab[pm]) == tr[pm]), 3) if np.any(pm) else None
    cell["persist_days"] = int(pm.sum())
    # context for persistence: how often ANY forward 63d window in this cell was
    # up in absolute terms (equities drift up, so 'persisted' in an up-trend is
    # only informative against this)
    okab = ~np.isnan(ab)
    cell["share_abs_pos"] = _r(np.mean(ab[okab] > 0), 3) if np.any(okab) else None
    if boot and cell["episodes"] >= 5:
        ci_e, ci_t = boot_median(v, e, blocks[ok], rng)
        cell["ci90"], cell["ci90_time"] = ci_e, ci_t
    return cell


def check_cell(v1, e1, b1, v2, e2, b2, expect, rng, kind=None):
    """expect: +1 (thesis: group 1 followed by better relative returns), -1
    (worse), 0 (no expected sign -- extension)."""
    n1, n2 = len(np.unique(e1)), len(np.unique(e2))
    cell = {"n_conf": int(n1), "n_div": int(n2), "days_conf": int(len(v1)), "days_div": int(len(v2)),
            "med_conf": _r(np.median(v1)) if len(v1) else None,
            "med_div": _r(np.median(v2)) if len(v2) else None}
    if not len(v1) or not len(v2):
        cell.update({"diff": None, "ci": None, "ci_time": None, "tag": "descriptive"})
        return cell
    cell["diff"] = _r(np.median(v1) - np.median(v2))
    ci, cit = boot_diff(v1, e1, b1, v2, e2, b2, rng)
    cell["ci"], cell["ci_time"] = ci, cit
    tag = "descriptive"
    if n1 >= MIN_EP and n2 >= MIN_EP and ci and cit:
        if expect > 0 and ci[0] > 0 and cit[0] > 0:
            tag = "edge"
        elif expect < 0 and ci[1] < 0 and cit[1] < 0:
            tag = "edge"
        elif expect == 0 and ((ci[0] > 0 and cit[0] > 0) or (ci[1] < 0 and cit[1] < 0)):
            tag = "edge"
        elif expect != 0 and ((expect > 0 and ci[1] < 0 and cit[1] < 0) or
                              (expect < 0 and ci[0] > 0 and cit[0] > 0)):
            cell["note"] = "ran OPPOSITE to the thesis: the divergence was followed by better " \
                           "relative returns than confirmation"
            cell["reversed"] = True
    cell["tag"] = tag
    return cell


# ===========================================================================
# lookahead proof on real data
# ===========================================================================

def lookahead_check(close, check_xs, check_cfg, d):
    """States at <= d must be identical after every bar after d is replaced;
    a positive control (a state after d) must change."""
    rng = np.random.default_rng(7)
    f0 = RP.phase_frame(close)
    c0 = RP.labels(f0)
    m0, s0 = RP.measure(check_cfg["kind"], check_xs, check_cfg["window"], check_cfg.get("unit"))
    st0 = RP.status_word_array(RP.status_codes(m0, s0, check_cfg["s"]), f0["trend"])
    e0, p0 = RP.ext_frame(close)
    mut = close.copy()
    n = len(mut)
    shock = np.exp(np.cumsum(rng.normal(-0.004, 0.03, n - d - 1)))
    mut[d + 1:] = mut[d] * shock
    xs_mut = [x.copy() for x in check_xs]
    xs_mut[0][d + 1:] = xs_mut[0][d] * np.exp(np.cumsum(rng.normal(0.004, 0.03, n - d - 1)))
    f1 = RP.phase_frame(mut)
    c1 = RP.labels(f1)
    m1, s1 = RP.measure(check_cfg["kind"], xs_mut, check_cfg["window"], check_cfg.get("unit"))
    st1 = RP.status_word_array(RP.status_codes(m1, s1, check_cfg["s"]), f1["trend"])
    e1, p1 = RP.ext_frame(mut)
    same = (np.array_equal(c0[:d + 1], c1[:d + 1]) and
            np.array_equal(st0[:d + 1], st1[:d + 1], equal_nan=True) and
            np.array_equal(p0[:d + 1], p1[:d + 1], equal_nan=True))
    changed = (not np.array_equal(c0[d + 1:], c1[d + 1:])) and \
              (not np.array_equal(st0[d + 1:], st1[d + 1:], equal_nan=True))
    return same, changed


# ===========================================================================
# main
# ===========================================================================

def main(argv=None):
    global REPS
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--reps", type=int, default=REPS)
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args(argv)
    REPS = a.reps
    rng = np.random.default_rng(SEED)

    wide, fetch_errs = load_history(a.refresh)
    dates, aligned = align_all(wide)
    n = len(dates)
    res = Resolver(aligned)
    spy = aligned["SPY"]
    lspy = RP._log(spy)
    blocks_all = np.arange(n) // BLOCK

    def fwd(lp, h):
        out = np.full(n, np.nan)
        out[:n - h] = lp[h:] - lp[:n - h]
        return out

    spy_f = {h: fwd(lspy, h) for h in FWD}
    prims = TH.primaries()
    fam_of = {t: TH.THESES[TH.primary_of(t)]["family"] for t in [e["t"] for e in RG.REG_ETFS]}

    rows_out = {}
    pool = {"code": [], "rel63": [], "rel21": [], "abs63": [], "trend": [], "ep": [],
            "blk": [], "fam": []}
    chk = []        # (kind, family, trend, group, rel63, ep, blk)
    ep_base = 0
    first_label = None
    last_fwd = None

    for e in RG.REG_ETFS:
        t = e["t"]
        close = res.get(t)
        fam = fam_of[t]
        info = {"family": fam, "primary": TH.primary_of(t)}
        if close is None or not np.any(~np.isnan(close)):
            info["note"] = "no history"
            rows_out[t] = info
            continue
        first = int(np.argmax(~np.isnan(close)))
        info["first"] = dates[first]
        info["sessions"] = int(np.sum(~np.isnan(close)))
        if t in TH.BASKETS and res.basket_start.get(t) is not None:
            info["basket_from"] = dates[res.basket_start[t]]
        f = RP.phase_frame(close)
        codes = RP.labels(f)
        lp = RP._log(close)
        rel63 = fwd(lp, 63) - spy_f[63]
        rel21 = fwd(lp, 21) - spy_f[21]
        abs63 = fwd(lp, 63)
        trend = f["trend"]
        ext, pct = RP.ext_frame(close)
        info["ext_q"] = RP.ext_quantiles(ext)
        info["ext_n"] = int(np.sum(~np.isnan(ext[-RP.EXT_LB:])))
        valid = codes >= 0
        eps = run_ids(codes, valid)
        # per-row label stats (absolute and relative)
        lab = {}
        for ci_, key in enumerate(RP.LABEL_KEYS):
            m = (codes == ci_) & ~np.isnan(abs63)
            if not np.any(m):
                continue
            st = {"days": int(m.sum()), "episodes": int(len(np.unique(eps[m]))),
                  "med_abs63": _r(np.median(abs63[m]))}
            if t != "SPY":
                st["med_rel63"] = _r(np.median(rel63[m]))
                st["share_pos"] = _r(np.mean(rel63[m] > 0), 3)
            pm = m & (trend != 0) & ~np.isnan(trend)
            st["persist"] = _r(np.mean(np.sign(abs63[pm]) == trend[pm]), 3) if np.any(pm) else None
            lab[key] = st
        info["labels"] = lab
        if np.any(valid):
            fl = int(np.argmax(valid))
            info["labels_from"] = dates[fl]
        is_primary = (TH.primary_of(t) == t)
        if is_primary and t != "SPY" and np.any(valid):
            fl = int(np.argmax(valid))
            first_label = fl if first_label is None else min(first_label, fl)
            okf = ~np.isnan(rel63)
            if np.any(okf):
                lf = int(np.max(np.where(okf)[0]))
                last_fwd = lf if last_fwd is None else max(last_fwd, lf)
            m = valid
            pool["code"].append(codes[m])
            pool["rel63"].append(rel63[m])
            pool["rel21"].append(rel21[m])
            pool["abs63"].append(abs63[m])
            pool["trend"].append(trend[m])
            pe, ep_base = _offset(eps, ep_base)
            pool["ep"].append(pe[m])
            pool["blk"].append(blocks_all[m])
            pool["fam"].append(np.array([fam] * int(m.sum()), dtype=object))

        # checks (primaries only; twins inherit the same measures)
        if is_primary:
            rc = {}
            for c in TH.checks_for(t):
                k = c["kind"]
                if k in CALIBRATED_KINDS and c["s"] != 0:
                    xs = check_arrays(c, res)
                    if xs is None:
                        rc[c["id"]] = {"note": "inputs unavailable"}
                        continue
                    mm, sd = RP.measure(k, xs, c["window"], c.get("unit"), c.get("threshold", 0.0))
                    codes_c = RP.status_codes(mm, sd, c["s"])
                    word = RP.status_word_array(codes_c, trend)
                    okw = ~np.isnan(word)
                    key = np.where(okw, word * 10 + np.nan_to_num(trend), np.nan)
                    ceps, ep_base = _offset(run_ids(key, okw), ep_base)
                    per = {}
                    for tw, tv, g1, g2 in (("up", 1, 1, -1), ("down", -1, 1, -1), ("flat", 0, 2, -2)):
                        sel = (trend == tv) & ~np.isnan(rel63)
                        a1 = sel & (word == g1)
                        a2 = sel & (word == g2)
                        per[tw] = {"n_conf": int(len(np.unique(ceps[a1]))),
                                   "n_div": int(len(np.unique(ceps[a2]))),
                                   "med_conf": _r(np.median(rel63[a1])) if np.any(a1) else None,
                                   "med_div": _r(np.median(rel63[a2])) if np.any(a2) else None}
                        if t != "SPY":
                            for grp, msk in (("conf", a1), ("div", a2)):
                                if np.any(msk):
                                    chk.append((k, fam, tw, grp, rel63[msk], ceps[msk], blocks_all[msk]))
                    rc[c["id"]] = per
                elif k == "extension" and t != "SPY":
                    okp = ~np.isnan(pct) & ~np.isnan(rel63)
                    for tw, tv, hot in (("up", 1, pct >= RP.EXT_HI), ("down", -1, pct <= RP.EXT_LO)):
                        sel = okp & (trend == tv)
                        flag = np.where(hot, 1.0, 0.0)
                        key = np.where(sel, flag, np.nan)
                        ceps, ep_base = _offset(run_ids(key, sel), ep_base)
                        for grp, msk in (("conf", sel & hot), ("div", sel & ~hot)):
                            if np.any(msk):
                                chk.append(("extension", fam, tw, grp, rel63[msk], ceps[msk],
                                            blocks_all[msk]))
            info["checks"] = rc
        rows_out[t] = info

    # ---------------- pooled phase base rates ----------------
    P = {k: np.concatenate(v) for k, v in pool.items()}
    phase_all = {}
    phase_fam = {f_: {} for f_ in TH.FAMILIES}
    for ci_, key in enumerate(RP.LABEL_KEYS):
        m = P["code"] == ci_
        cell = label_cell(P["rel63"][m], P["rel21"][m], P["abs63"][m], P["trend"][m],
                          P["ep"][m], P["blk"][m], rng)
        if cell:
            phase_all[key] = cell
        for f_ in TH.FAMILIES:
            mf = m & (P["fam"] == f_)
            cell = label_cell(P["rel63"][mf], P["rel21"][mf], P["abs63"][mf], P["trend"][mf],
                              P["ep"][mf], P["blk"][mf], rng)
            if cell:
                phase_fam[f_][key] = cell
    base = {}
    for name, m in (("all_labelled_days", np.ones(len(P["code"]), bool)),
                    ("up_trend_days", P["trend"] == 1),
                    ("down_trend_days", P["trend"] == -1),
                    ("flat_trend_days", P["trend"] == 0)):
        base[name] = label_cell(P["rel63"][m], P["rel21"][m], P["abs63"][m], P["trend"][m],
                                P["ep"][m], P["blk"][m], rng, boot=False)
    freq = {key: int(np.sum(P["code"] == ci_)) for ci_, key in enumerate(RP.LABEL_KEYS)}

    # ---------------- pooled check evidence ----------------
    kinds = list(CALIBRATED_KINDS) + ["extension"]
    checks_all = {}
    checks_fam = {f_: {} for f_ in TH.FAMILIES}

    def gather(kind, fam, tw, grp):
        sel = [x for x in chk if x[0] == kind and x[2] == tw and x[3] == grp and (fam is None or x[1] == fam)]
        if not sel:
            return np.array([]), np.array([], dtype=np.int64), np.array([], dtype=np.int64)
        return (np.concatenate([x[4] for x in sel]), np.concatenate([x[5] for x in sel]),
                np.concatenate([x[6] for x in sel]))

    tested = 0
    for kind in kinds:
        for fam in [None] + list(TH.FAMILIES):
            dest = checks_all if fam is None else checks_fam[fam]
            for tw in ("up", "down", "flat"):
                if kind == "extension" and tw == "flat":
                    continue
                v1, e1, b1 = gather(kind, fam, tw, "conf")
                v2, e2, b2 = gather(kind, fam, tw, "div")
                if not len(v1) and not len(v2):
                    continue
                expect = 0 if kind == "extension" else (1 if tw in ("up", "flat") else -1)
                cell = check_cell(v1, e1, b1, v2, e2, b2, expect, rng, kind)
                if kind == "extension":
                    cell["note"] = (("stretched (>= 90th pct of its 3y range) vs other up-trend days"
                                     if tw == "up" else
                                     "washed out (<= 10th pct of its 3y range) vs other down-trend days")
                                    + ("; " + cell["note"] if cell.get("note") else ""))
                if cell["n_conf"] >= MIN_EP and cell["n_div"] >= MIN_EP:
                    tested += 1
                dest.setdefault(kind, {})[tw] = cell

    # ---------------- lookahead proof on a real row ----------------
    la = {}
    gd = res.get("GDX")
    gc = res.get("GC=F")
    if gd is not None and gc is not None:
        d = n - 400
        same, changed = lookahead_check(gd.copy(), [gc.copy()],
                                        {"kind": "driver", "window": 63, "s": 1}, d)
        la = {"row": "GDX", "check": "GC=F driver", "d": dates[d], "states_at_or_before_d_unchanged": same,
              "positive_control_changed_after_d": changed}
        if not (same and changed):
            print("LOOKAHEAD CHECK FAILED", la, file=sys.stderr)
            return 1

    edges = sorted({(k, f_, tw) for f_, dct in [("all", checks_all)] + list(checks_fam.items())
                    for k, tws in dct.items() for tw, c in tws.items() if c.get("tag") == "edge"})
    out = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "asof": dates[-1],
        "window": {"history_requested_from": START, "axis_from": dates[0],
                   "labels_from": dates[first_label] if first_label is not None else None,
                   "outcomes_to": dates[last_fwd] if last_fwd is not None else None,
                   "sessions": n,
                   "primaries_pooled": len([p for p in prims if p != "SPY"])},
        "method": {
            "phase": "rotation_phase.phase_frame + labels (SPEC-86 s4), trailing windows only",
            "checks": "rotation_phase.measure + status_codes + status_word_array (SPEC-86 s5)",
            "outcome": "forward 63d (and 21d) log return of the row minus SPY's; persistence = sign of "
                       "the forward 63d absolute log return equals the 126d trend sign (up/down days only)",
            "episode": "consecutive sessions in the same state on the same row (checks: same status "
                       "word under the same trend) = one episode",
            "bootstrap": f"{REPS} reps, seed {SEED}; weighted medians; 'ci' resamples episodes (spec), "
                         f"'ci_time' resamples {BLOCK}-session calendar blocks shared across rows",
            "edge_rule": f">= {MIN_EP} episodes a side AND both 90% intervals exclude 0 in the thesis' "
                         "direction (diff = median(confirming) - median(diverging); the thesis expects "
                         "> 0 in up-trends and flat (leaning up vs down), < 0 in down-trends). Extension "
                         "has no thesis sign: edge = both intervals exclude 0 either way",
            "baskets": f"regime.basket_series over today's members, from the first session with >= "
                       f"{BASKET_MIN_MEMBERS} members priced",
            "dead_zones": f"checks: {RP.CHECK_DZ} x the measure's own window volatility (std of daily "
                          f"changes over {RP.SIGMA_LB} sessions x sqrt(window)); level checks: {RP.CHECK_DZ} "
                          "x the std of the level over the window",
        },
        "notes": [
            "Phases are STATES, not forecasts. Every number here describes what followed these "
            "states on these sleeves' own past; none is a prediction.",
            "Survivorship: the 80 rows were chosen in 2026. Baskets are rebuilt from TODAY's members "
            "(membership hindsight: NEOCLOUD and QUANTUM members were picked for what they became). "
            "Their back-cast bars describe other businesses: NEOCLOUD's members were bitcoin miners "
            "before their GPU-cloud pivot, and QUANTUM's early bars include pre-merger SPAC shells.",
            "Overlap: consecutive days share almost all of their 63d forward window -- episodes, not "
            "days, are the honest unit. Rows also move together (2008 hits every row at once), which "
            "is why 'edge' also requires the calendar-block interval to exclude 0.",
            f"Multiple comparisons: {tested} kind x family x trend cells had >= {MIN_EP} episodes a "
            f"side; at 90% one-sided-in-the-thesis-direction, about 5% of them could clear the "
            f"episode interval by chance alone.",
            "t126 is the plain OLS t-stat of a 126d log-price regression (as SPEC-86 s4 declares). "
            "Daily residuals are autocorrelated, so |t| is large for most drifting series and 'flat' "
            "is a narrow state; see label_frequency.",
            "Futures are continuous front-month series with roll jumps; CL=F printed negative in "
            "April 2020, so windows containing that print are n/a. Yields are in percent; their "
            "measures are changes in basis points.",
            "Participation, concentration and split checks are NOT calibrated: there is no holdings "
            "history. Informational ratio checks (s = 0) have no expected sign and are not calibrated.",
            "SPY is excluded from the pools (its return relative to itself is zero); twins are "
            "excluded (near-duplicates of their primary) but keep their own per-row statistics.",
            "DRAM has history from 2026-04 only: no evidence of its own.",
        ],
        "label_frequency": freq,
        "edges": [{"kind": k, "pool": f_, "trend": tw} for k, f_, tw in edges],
        # how many cells could have earned "edge" at all -- the board shows edges as
        # "N of M tested" so a chance-level count is never read as a finding
        "cells_tested": tested,
        "phase": {"all": phase_all, "by_family": phase_fam, "baseline": base},
        "checks": {"all": checks_all, "by_family": checks_fam,
                   "uncalibrated": {"participation": "no holdings history",
                                    "concentration": "no holdings history",
                                    "split": "no holdings history"}},
        "lookahead_check": la,
        "rows": rows_out,
        "fetch_errors": fetch_errs,
    }
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=0, separators=(",", ":"), allow_nan=False)  # one field per line (indent=0): the repo's pre-commit guard reads line by line, and a one-line payload puts every word beside every number
        fh.write("\n")
    os.replace(tmp, a.out)

    # ---------------- console summary ----------------
    print(f"window {out['window']}")
    print("label        days  episodes  med_rel63  share+  persist  ci90(ep)            ci90(time)")
    for key in RP.LABEL_KEYS:
        c = phase_all.get(key)
        if not c:
            continue
        print(f"{key:18s} {c['days']:7d} {c['episodes']:6d} {c['med_rel63']:+.4f} {c['share_pos']:.3f} "
              f"{(c['persist'] if c['persist'] is not None else float('nan')):.3f}  {c.get('ci90')}  {c.get('ci90_time')}")
    for name, c in base.items():
        print(f"baseline {name}: {c}")
    print("check kinds (all families):")
    for kind, tws in checks_all.items():
        for tw, c in tws.items():
            print(f"  {kind:10s} {tw:5s} n {c['n_conf']:5d}/{c['n_div']:5d}  med {c['med_conf']}/{c['med_div']}"
                  f"  diff {c['diff']}  ci {c['ci']}  ci_t {c['ci_time']}  {c['tag']}"
                  f"{'  REVERSED' if c.get('reversed') else ''}")
    print("edges:", out["edges"])
    print("lookahead:", la)
    print(f"Wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
