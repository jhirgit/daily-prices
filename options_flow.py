#!/usr/bin/env python3
"""options_flow.py — SPEC-62 phase 1: a daily options-chain snapshot, computed.

Emits data/options_flow.json (+ data/options_iv_hist.json) for every optionable
name in tickers.txt, from the three nearest monthly expiries:

    UNUSUAL(c)   vol/OI > 3  AND  volume > 500  AND  premium notional > $1M
                 All three ANDed. The ratio alone fires constantly on illiquid
                 far-OTM strikes where OI is single digits; the 500-contract
                 floor removes single prints and the $1M floor removes penny
                 lottery tickets that trivially clear a ratio test.
    pc_vol/pc_oi put/call volume and open-interest skew; flagged > 1.5 or < 0.40
                 once total volume clears 1,000.
    d_coi/d_poi  day-over-day open-interest change, plus per-contract deltas on
                 the twelve largest-OI contracts. The ONLY state this job keeps:
                 it reads yesterday's own artifact before overwriting it.
    iv30/iv60    ATM implied vol linearly interpolated to 30 and 60 days across
                 the bracketing expiries, in vol POINTS (41.2 == 41.2%).
    iv_rank      (iv30 - min) / (max - min) over the trailing IV history.
                 null below 60 sessions, `provisional` 60-251, `full` at >= 252.
                 A rank over three weeks of history is a number that means
                 nothing, and shipping it would be worse than shipping a blank.
    kink         iv30 - iv60; > +5 vol points means the front expiry is pricing
                 an event, cross-referenced against data/earnings_dates.json so
                 a known print is NAMED rather than flagged as a mystery.
    top          the five contracts with the largest premium notional, verbatim.

WHAT A SNAPSHOT CANNOT SAY (SPEC-62 s1). This is positioning arithmetic, not
flow. It cannot see the tape, the aggressor side, or whether a print opened or
closed a position — so no "$4M call sweep, bullish" claims. Confidence LOW to
MODERATE, display-only, alert-never-action, the same footing as the SPEC-36
crowd block.

DISCLOSURE (#15): this repo is PUBLIC. Every field here is a public fact about a
ticker already listed in tickers.txt — its own listed chain. No holding, weight,
basis, custodian or account data touches this file or its artifacts.

SOURCE: yfinance, not Schwab (SPEC-62 s3). Not because Schwab's numbers are
worse — they are the same OPRA end-of-day figures — but because this job runs in
GitHub Actions on a PUBLIC repo, a Schwab refresh token grants account and
position read, it dies every 7 days and renews only through an interactive
browser login, and that entitlement returns no per-contract IV at all.

    python options_flow.py                        # write both artifacts
    python options_flow.py --tickers NVDA,MU      # small live run
    python options_flow.py --dry-run              # compute, print, write nothing
    python options_flow.py --verify               # parity against the fixture
    python options_flow.py --freeze               # re-freeze the fixture's expectations

Stdlib + yfinance only (yfinance is imported lazily, so --verify and the unit
tests need no network and no third-party import at all).
"""

import argparse
import datetime as dt
import io
import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
DEFAULT_TICKERS = os.path.join(HERE, "tickers.txt")
DEFAULT_OUT = os.path.join(DATA, "options_flow.json")
DEFAULT_IV_OUT = os.path.join(DATA, "options_iv_hist.json")
EARNINGS = os.path.join(DATA, "earnings_dates.json")
LATEST = os.path.join(DATA, "latest.json")
FIXTURE = os.path.join(DATA, "fixtures", "options_flow_fixture.json")

# --- the constants, all of them, declared here beside the crowd.py precedent --
N_EXPIRIES = 3               # nearest monthly expiries per name
VOLOI_MIN = 3.0              # vol/OI strictly above this
VOL_MIN = 500                # contracts, strictly above
NOTIONAL_MIN = 1_000_000.0   # premium dollars, strictly above
PC_HI = 1.5                  # put/call skew flags
PC_LO = 0.40
PC_VOL_FLOOR = 1000          # total volume required before a skew flag means anything
KINK_PTS = 5.0               # iv30 - iv60, vol points
IV_TARGETS = (30, 60)
IV_MIN_SESSIONS = 60         # below this the rank is null
IV_FULL_SESSIONS = 252       # at/above this the rank is "full"
IV_HIST_CAP = 252            # sessions retained per name
IV_HIGH, IV_LOW = 0.80, 0.20
OI_TOP_N = 12                # per-contract OI deltas carried forward
TOP_N = 5                    # largest-notional contracts kept verbatim
SIZE_WARN = 200 * 1024
SIZE_FAIL = 300 * 1024
COVERAGE_MIN = 0.80          # of the optionable universe

# SIZE, MEASURED ON A FULL DRY RUN (2026-09-08, 195 optionable names):
#   summary 326 B + oi_top 339 B + top-5 647 B = ~1,270 B median per name,
#   payload 249,585 B. That is ABOVE the 200 KB warn line and below the 300 KB
#   hard cap, and it warns on every run. SPEC-62 s5 budgeted 850 B/name, but that
#   estimate is not reachable with the ten-field `top` entry the SAME section
#   prescribes: five entries at 129 B each is 647 B on their own, against the
#   ~156 B the 482 B "summary + top-5" figure implies. The schema is shipped as
#   specified rather than quietly trimmed — the warn is TRUE and the number it is
#   reporting is real. Headroom before the hard cap is ~236 names; tickers.txt
#   currently yields 195 optionable of 222 lines. Jake's call, not the job's:
#   raise the warn line, or drop `top` to three entries.
#
# The IV history is the reason it is a SEPARATE artifact: 252 floats per name
# projects to ~342 KB on its own, so carried inside options_flow.json the payload
# would reach ~590 KB and breach the hard cap outright.

DISCLOSURE = ("PUBLIC repo, chain data only: public listed-option facts about tickers already in "
              "tickers.txt. A once-a-day snapshot is positioning arithmetic, not flow — it cannot see "
              "the tape or the aggressor side. Display-only, alert-never-action (SPEC-62 s1).")


# ---------------------------------------------------------------- small helpers
def _f(x):
    """float or None — NaN, None and non-numerics all collapse to None."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) or math.isinf(v) else v


def _i(x):
    v = _f(x)
    return None if v is None else int(v)


def _r(x, nd):
    return None if x is None else round(x, nd)


def load_tickers(path=DEFAULT_TICKERS):
    out = []
    with io.open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            t = line.split("#", 1)[0].strip()
            if t:
                out.append(t)
    return out


def structural_skip(ticker):
    """Instruments that structurally cannot have a listed US option chain.
    Everything else is probed and lands in `skipped` on its own if Yahoo returns
    no expiries — so a name that GAINS options is picked up the next session and
    a name that loses them drops out, with no hand-maintained exclusion list."""
    if ticker.startswith("^"):
        return "index"
    if ticker.endswith("=F"):
        return "futures"
    if ticker.endswith("-USD"):
        return "crypto"
    return None


def third_friday(year, month):
    d = dt.date(year, month, 1)
    return d + dt.timedelta(days=(4 - d.weekday()) % 7 + 14)


def is_monthly(iso):
    d = dt.date.fromisoformat(iso)
    return d == third_friday(d.year, d.month)


def pick_expiries(expiries, n=N_EXPIRIES, today=None):
    """The n nearest MONTHLY expiries, or the n nearest of any kind if the name
    lists fewer than n monthlies."""
    cut = today.isoformat() if today else None
    fut = sorted(e for e in expiries if cut is None or e >= cut)
    monthlies = [e for e in fut if is_monthly(e)]
    return monthlies[:n] if len(monthlies) >= n else fut[:n]


# ------------------------------------------------------------ contract signals
def mid_or_last(c):
    """Mid when a two-sided quote exists, else the last print. SPEC-62 s4 prices
    premium at `mid or last`; a one-sided or crossed book falls back to last."""
    b, a = _f(c.get("bid")), _f(c.get("ask"))
    if b is not None and a is not None and b > 0 and a > 0 and a >= b:
        return (b + a) / 2.0
    px = _f(c.get("px"))
    return px if px and px > 0 else None


def notional(c):
    px = mid_or_last(c)
    v = _f(c.get("v")) or 0.0
    return None if px is None else v * 100.0 * px


def vol_oi(c):
    """None (never 0.0, never inf) when open interest is zero or absent."""
    oi = _f(c.get("oi")) or 0.0
    if oi <= 0:
        return None
    return (_f(c.get("v")) or 0.0) / oi


def is_unusual(c):
    r = vol_oi(c)
    n = notional(c)
    v = _f(c.get("v")) or 0.0
    return bool(r is not None and r > VOLOI_MIN
                and v > VOL_MIN
                and n is not None and n > NOTIONAL_MIN)


# ----------------------------------------------------------------- term / IV
def atm_iv(calls, puts, spot):
    """ATM implied vol for one expiry: the call and the put whose strike is
    nearest spot, averaged. Returns a FRACTION (0.412), or None."""
    if not spot:
        return None
    picks = []
    for leg in (calls, puts):
        best = None
        for c in leg or ():
            iv, k = _f(c.get("iv")), _f(c.get("k"))
            if iv is None or iv <= 0 or k is None:
                continue
            d = abs(k - spot)
            if best is None or d < best[0]:
                best = (d, iv)
        if best:
            picks.append(best[1])
    return sum(picks) / len(picks) if picks else None


def interp_term(points, target_days):
    """Linear interpolation of ATM IV to `target_days` across the bracketing
    expiries. Flat outside the term the name actually lists — never extrapolated
    off the end of a three-expiry curve, which produces nonsense."""
    pts = sorted((d, v) for d, v in points if v is not None and d is not None and d > 0)
    if not pts:
        return None
    if target_days <= pts[0][0]:
        return pts[0][1]
    if target_days >= pts[-1][0]:
        return pts[-1][1]
    for (d0, v0), (d1, v1) in zip(pts, pts[1:]):
        if d0 <= target_days <= d1:
            w = (target_days - d0) / float(d1 - d0)
            return v0 + w * (v1 - v0)
    return None


def iv_rank(hist, iv30):
    """(rank, pct, n, state) over the trailing history PLUS today's reading.
    Below IV_MIN_SESSIONS the rank is null — deliberately blank, never 0."""
    vals = [v for v in (hist or ()) if v is not None]
    if iv30 is None:
        return None, None, len(vals), "null"
    vals = vals + [iv30]
    n = len(vals)
    if n < IV_MIN_SESSIONS:
        return None, None, n, "null"
    state = "full" if n >= IV_FULL_SESSIONS else "provisional"
    lo, hi = min(vals), max(vals)
    rank = None if hi <= lo else (iv30 - lo) / (hi - lo)
    pct = sum(1 for v in vals if v < iv30) / float(n)
    return rank, pct, n, state


# ------------------------------------------------------------------ aggregation
def _flat(chain):
    """[(expiry, 'C'|'P', contract)] across every expiry in the chain."""
    out = []
    for e in sorted((chain.get("expiries") or {})):
        leg = chain["expiries"][e] or {}
        for kind, key in (("C", "calls"), ("P", "puts")):
            for c in leg.get(key) or ():
                out.append((e, kind, c))
    return out


def earnings_in_window(earnings, ticker, as_of, until):
    """The expected print date for `ticker` strictly after as_of and on/before
    `until`, or None. Accepts both the SPEC-71 {date, estimated} shape and the
    older bare-string feed."""
    v = (earnings or {}).get(ticker)
    if isinstance(v, dict):
        d, est = v.get("date"), v.get("estimated")
    else:
        d, est = v, None
    if not d or not (as_of < d <= until):
        return None
    return {"date": d, "estimated": est}


def summarize_name(ticker, chain, prior=None, hist=None, earnings=None):
    """One row of the artifact's `names` map. Pure: no network, no clock."""
    as_of = chain.get("as_of")
    spot = _f(chain.get("spot"))
    rows = _flat(chain)
    cv = pv = 0.0
    coi = poi = 0.0
    n_unusual = 0
    voloi_max, voloi_max_c = None, None
    scored = []

    for e, kind, c in rows:
        v = _f(c.get("v")) or 0.0
        oi = _f(c.get("oi")) or 0.0
        if kind == "C":
            cv += v
            coi += oi
        else:
            pv += v
            poi += oi
        r = vol_oi(c)
        if r is not None and (voloi_max is None or r > voloi_max):
            voloi_max, voloi_max_c = r, c.get("s")
        if is_unusual(c):
            n_unusual += 1
        scored.append((notional(c) or 0.0, oi, e, kind, c))

    tot_v = cv + pv
    pc_vol = round(pv / cv, 4) if cv > 0 else None
    pc_oi = round(poi / coi, 4) if coi > 0 else None

    # --- day-over-day open interest: yesterday's artifact is the only state ----
    p = prior or {}
    p_oi_top = p.get("oi_top") or {}
    d_coi = int(coi - p["coi"]) if _f(p.get("coi")) is not None else None
    d_poi = int(poi - p["poi"]) if _f(p.get("poi")) is not None else None

    def contract_d_oi(c, oi):
        was = _f(p_oi_top.get(c.get("s")))
        return None if was is None else int(oi - was)

    by_oi = sorted(scored, key=lambda r: (-(r[1] or 0.0), r[4].get("s") or ""))[:OI_TOP_N]
    oi_top = {r[4].get("s"): int(r[1] or 0) for r in by_oi}

    by_notional = sorted(scored, key=lambda r: (-(r[0] or 0.0), r[4].get("s") or ""))[:TOP_N]
    top = []
    for n, oi, e, kind, c in by_notional:
        top.append({
            "s": c.get("s"), "t": kind, "e": e, "k": _r(_f(c.get("k")), 4),
            "v": _i(c.get("v")) or 0, "oi": int(oi or 0),
            "iv": _r(_f(c.get("iv")), 4), "px": _r(mid_or_last(c), 4),
            "n": int(round(n or 0.0)), "d_oi": contract_d_oi(c, oi),
        })

    # --- term structure -------------------------------------------------------
    pts = []
    base = dt.date.fromisoformat(as_of) if as_of else None
    for e in sorted((chain.get("expiries") or {})):
        leg = chain["expiries"][e] or {}
        iv = atm_iv(leg.get("calls"), leg.get("puts"), spot)
        dte = (dt.date.fromisoformat(e) - base).days if base else None
        pts.append((dte, iv))
    iv30 = interp_term(pts, IV_TARGETS[0])
    iv60 = interp_term(pts, IV_TARGETS[1])
    iv30p = _r(iv30 * 100.0, 2) if iv30 is not None else None
    iv60p = _r(iv60 * 100.0, 2) if iv60 is not None else None
    kink = _r(iv30p - iv60p, 2) if (iv30p is not None and iv60p is not None) else None

    rank, pct, rank_n, state = iv_rank(hist, iv30p)

    # --- flags ----------------------------------------------------------------
    flags = []
    if n_unusual:
        flags.append("UNUSUAL")
    if pc_vol is not None and tot_v > PC_VOL_FLOOR and (pc_vol > PC_HI or pc_vol < PC_LO):
        flags.append("SKEW")
    kink_event = None
    if kink is not None and kink > KINK_PTS:
        flags.append("EVENT")
        exps = sorted((chain.get("expiries") or {}))
        until = exps[0] if exps else as_of
        if base:
            until = max(until, (base + dt.timedelta(days=IV_TARGETS[0])).isoformat())
        kink_event = earnings_in_window(earnings, ticker, as_of or "", until or "")
    if state == "full" and rank is not None:
        if rank > IV_HIGH:
            flags.append("IV-HIGH")
        elif rank < IV_LOW:
            flags.append("IV-LOW")

    return {
        "spot": _r(spot, 4), "n": len(rows),
        "cv": int(cv), "pv": int(pv), "pc_vol": pc_vol,
        "coi": int(coi), "poi": int(poi), "pc_oi": pc_oi,
        "d_coi": d_coi, "d_poi": d_poi,
        "voloi_max": _r(voloi_max, 4), "voloi_max_c": voloi_max_c,
        "iv30": iv30p, "iv60": iv60p,
        "iv_rank": _r(rank, 4), "iv_pct": _r(pct, 4),
        "iv_rank_n": rank_n, "iv_state": state,
        "kink": kink, "kink_event": kink_event,
        "n_unusual": n_unusual, "flags": flags,
        "top": top, "oi_top": oi_top,
    }


# --------------------------------------------------------------- IV history file
def hist_for(prev, ticker, session):
    """The name's trailing iv30 series, with a same-session tail dropped so a
    re-run on the same day cannot count today twice."""
    sessions = (prev or {}).get("sessions") or []
    arr = list(((prev or {}).get("names") or {}).get(ticker) or [])
    if sessions and session and sessions[-1] == session and arr:
        arr = arr[:-1]
    return arr


def update_iv_hist(prev, session, iv30_by_name, cap=IV_HIST_CAP):
    """Append one iv30 float per name against a shared session axis (null where a
    name had no reading that day), then trim to `cap` sessions. Self-capping —
    nothing to prune by hand."""
    prev = prev or {}
    sessions = list(prev.get("sessions") or [])
    names = {k: list(v) for k, v in (prev.get("names") or {}).items()}
    if sessions and sessions[-1] == session:
        sessions.pop()
        for k in names:
            names[k] = names[k][:-1]
    sessions.append(session)
    keep = len(sessions) - 1
    for k in set(list(names) + list(iv30_by_name)):
        arr = names.get(k, [])
        arr = arr[-keep:] if keep > 0 else []
        arr = [None] * (keep - len(arr)) + arr
        arr.append(iv30_by_name.get(k))
        names[k] = arr
    if len(sessions) > cap:
        sessions = sessions[-cap:]
        for k in names:
            names[k] = names[k][-cap:]
    # a name that has fallen entirely out of the window stops being carried
    names = {k: v for k, v in names.items() if any(x is not None for x in v)}
    return {"cap": cap, "sessions": sessions, "names": names}


# ------------------------------------------------------------------ the fetcher
class NoOptions(Exception):
    pass


def fetch_chain(ticker, n_exp=N_EXPIRIES, today=None):
    """Normalise one name's chain into the shape every signal function above
    consumes — which is exactly the shape of the parity fixture, so the fixture
    tests the shipped code and not a copy of it."""
    import yfinance as yf
    from zoneinfo import ZoneInfo

    t = yf.Ticker(ticker)
    exps = list(t.options or ())
    if not exps:
        raise NoOptions("no listed options")
    chosen = pick_expiries(exps, n_exp, today)
    if not chosen:
        raise NoOptions("no expiries at or after today")

    out = {"spot": None, "as_of": None, "expiries": {}}
    for e in chosen:
        ch = t.option_chain(e)
        u = getattr(ch, "underlying", None) or {}
        if out["spot"] is None:
            out["spot"] = _f(u.get("regularMarketPrice"))
        if out["as_of"] is None:
            ts = _f(u.get("regularMarketTime"))
            tzname = u.get("exchangeTimezoneName") or "America/New_York"
            if ts:
                try:
                    out["as_of"] = dt.datetime.fromtimestamp(ts, ZoneInfo(tzname)).date().isoformat()
                except Exception:
                    out["as_of"] = dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat()
        leg = {}
        for kind, df in (("calls", ch.calls), ("puts", ch.puts)):
            rows = []
            for rec in df.to_dict("records"):
                rows.append({
                    "s": rec.get("contractSymbol"),
                    "k": _f(rec.get("strike")),
                    "v": _i(rec.get("volume")) or 0,
                    "oi": _i(rec.get("openInterest")) or 0,
                    "iv": _r(_f(rec.get("impliedVolatility")), 6),
                    "px": _f(rec.get("lastPrice")),
                    "bid": _f(rec.get("bid")),
                    "ask": _f(rec.get("ask")),
                })
            leg[kind] = rows
        out["expiries"][e] = leg
    if out["as_of"] is None:
        out["as_of"] = (today or dt.date.today()).isoformat()
    return out


def load_earnings(path=EARNINGS):
    if not os.path.exists(path):
        return {}
    with io.open(path, "r", encoding="utf-8") as fh:
        return (json.load(fh) or {}).get("dates") or {}


def last_settled(path=LATEST):
    """The most recent settled session the price feed knows about."""
    if not os.path.exists(path):
        return None
    with io.open(path, "r", encoding="utf-8") as fh:
        j = json.load(fh)
    ds = [r.get("date") for r in (j.get("tickers") or []) if r.get("date")]
    return max(ds) if ds else None


def load_json(path):
    if not os.path.exists(path):
        return {}
    with io.open(path, "r", encoding="utf-8") as fh:
        return json.load(fh) or {}


# ---------------------------------------------------------------------- build
def assemble(names, skipped, universe, as_of, elapsed=None):
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
                          .isoformat().replace("+00:00", "Z"),
        "as_of": as_of,
        "source": "yfinance",
        "spec": "SPEC-62 phase 1",
        "disclosure": DISCLOSURE,
        "expiries": N_EXPIRIES,
        "thresholds": {"voloi": VOLOI_MIN, "volume": VOL_MIN, "notional": int(NOTIONAL_MIN),
                       "pc_hi": PC_HI, "pc_lo": PC_LO, "pc_vol_floor": PC_VOL_FLOOR,
                       "kink": KINK_PTS, "iv_min_sessions": IV_MIN_SESSIONS,
                       "iv_full_sessions": IV_FULL_SESSIONS},
        "iv_history": os.path.basename(DEFAULT_IV_OUT),
        "universe_n": len(universe),
        "count": len(names),
        "elapsed_s": _r(elapsed, 1),
        "skipped": dict(sorted(skipped.items())),
        "names": dict(sorted(names.items())),
    }


def dump(payload):
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def size_verdict(nbytes):
    """"ok" / "warn" / "fail" against the payload caps. Extracted so the guard is
    testable without building a 300 KB artifact."""
    if nbytes > SIZE_FAIL:
        return "fail"
    if nbytes > SIZE_WARN:
        return "warn"
    return "ok"


def per_name_bytes(payload):
    """Measured, not guessed: median and max serialized bytes of a name row."""
    sizes = sorted(len(json.dumps(v, separators=(",", ":"))) for v in payload["names"].values())
    if not sizes:
        return 0, 0, 0
    return sizes[len(sizes) // 2], sizes[-1], sum(sizes) // len(sizes)


# ------------------------------------------------------------------- live run
def run_live(args):
    today = dt.date.today()
    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = load_tickers(args.tickers_file)

    prior = load_json(args.out)
    prev_hist = load_json(args.iv_out)
    earnings = load_earnings(args.earnings)
    prior_names = prior.get("names") or {}

    names, skipped, universe, iv30_by_name = {}, {}, [], {}
    as_of_seen = set()
    t0 = time.time()
    for tk in tickers:
        why = structural_skip(tk)
        if why:
            skipped[tk] = why
            continue
        t1 = time.time()
        chain = None
        for attempt in (0, 1):
            try:
                chain = fetch_chain(tk, N_EXPIRIES, today)
                break
            except NoOptions as exc:
                skipped[tk] = str(exc)
                chain = None
                break
            except Exception as exc:
                if attempt:
                    skipped[tk] = "fetch error: %s" % type(exc).__name__
                    chain = None
                else:
                    time.sleep(1.0)
        if chain is None:
            continue
        universe.append(tk)
        session = chain.get("as_of")
        if session:
            as_of_seen.add(session)
        row = summarize_name(tk, chain, prior=prior_names.get(tk),
                             hist=hist_for(prev_hist, tk, session), earnings=earnings)
        names[tk] = row
        if row.get("iv30") is not None:
            iv30_by_name[tk] = row["iv30"]
        print("  %-10s n=%-4d cv=%-9d pv=%-9d pc_vol=%-8s unusual=%-3d flags=%-22s %.2fs"
              % (tk, row["n"], row["cv"], row["pv"], row["pc_vol"], row["n_unusual"],
                 ",".join(row["flags"]) or "-", time.time() - t1))
    elapsed = time.time() - t0

    as_of = max(as_of_seen) if as_of_seen else None
    payload = assemble(names, skipped, universe, as_of, elapsed)
    body = dump(payload)
    size = len(body.encode("utf-8"))
    med, mx, avg = per_name_bytes(payload)

    hist = update_iv_hist(prev_hist, as_of or today.isoformat(), iv30_by_name)
    hist_doc = {
        "generated_at": payload["generated_at"],
        "source": "options_flow.py — ATM iv30 (vol points) per name per session, SPEC-62 s4",
        "note": ("One float per name per session against a shared date axis; null where the name had no "
                 "reading. Self-capping at %d sessions. Kept OUT of options_flow.json because at steady "
                 "state it is the larger half of the payload and would breach the 300 KB cap."
                 % IV_HIST_CAP),
        "cap": hist["cap"], "sessions": hist["sessions"], "names": hist["names"],
    }
    hist_body = dump(hist_doc)
    hist_size = len(hist_body.encode("utf-8"))

    print("\noptions_flow: %d names, %d skipped, universe %d, as_of %s, %.1fs"
          % (len(names), len(skipped), len(universe), as_of, elapsed))
    print("  payload %s (%d B) — per name median %d B, mean %d B, max %d B"
          % (args.out, size, med, avg, mx))
    print("  iv history %s (%d B) — %d sessions x %d names, steady state ~%d B at %d sessions"
          % (args.iv_out, hist_size, len(hist["sessions"]), len(hist["names"]),
             _steady_state(hist_doc), IV_HIST_CAP))

    # ------------------------------------------------- loud, but only AFTER the write
    failures = []
    verdict = size_verdict(size)
    if verdict == "fail":
        failures.append("payload %d B exceeds the %d B hard cap" % (size, SIZE_FAIL))
    elif verdict == "warn":
        print("  WARN: payload %d B is above the %d B warn line" % (size, SIZE_WARN))
    cov = (len(names) / float(len(universe))) if universe else 0.0
    if universe and cov < COVERAGE_MIN:
        failures.append("coverage %.1f%% of the optionable universe is below %.0f%%"
                        % (cov * 100, COVERAGE_MIN * 100))
    prior_u = _f(prior.get("universe_n"))
    if prior_u and len(universe) < COVERAGE_MIN * prior_u:
        failures.append("optionable universe fell to %d from %d (below %.0f%%)"
                        % (len(universe), int(prior_u), COVERAGE_MIN * 100))
    settled = last_settled(args.latest)
    if settled and as_of and as_of < settled:
        failures.append("as_of %s trails the last settled session %s" % (as_of, settled))
    if not names:
        failures.append("no names produced a chain")

    if not args.dry_run:
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with io.open(args.out, "w", encoding="utf-8") as fh:
            fh.write(body)
            fh.write("\n")
        with io.open(args.iv_out, "w", encoding="utf-8") as fh:
            fh.write(hist_body)
            fh.write("\n")
    else:
        print("  (--dry-run: nothing written)")

    if failures:
        print("\nFAIL — the artifact was written and is committable, but:")
        for f in failures:
            print("  * %s" % f)
        return 1
    return 0


def _steady_state(hist_doc):
    """Projected bytes of the IV history once every carried name has a full window."""
    n_names = len(hist_doc["names"]) or 1
    n_sess = len(hist_doc["sessions"]) or 1
    per = sum(len(json.dumps(v, separators=(",", ":"))) for v in hist_doc["names"].values())
    per_cell = per / float(n_names * n_sess)
    return int(len(dump(hist_doc)) + n_names * per_cell * (IV_HIST_CAP - n_sess))


# ------------------------------------------------------------------- parity
def _fixture_rows(fx):
    """Recompute every fixture name from its synthetic chain — the same code path
    the live job takes, minus the network."""
    out = {}
    for tk in sorted(fx["chains"]):
        out[tk] = summarize_name(
            tk, fx["chains"][tk],
            prior=(fx.get("prior") or {}).get(tk),
            hist=hist_for(fx.get("iv_hist"), tk, fx["chains"][tk].get("as_of")),
            earnings=fx.get("earnings"))
    return out


def verify(path=FIXTURE):
    fx = load_json(path)
    if not fx:
        print("FAIL — fixture %s is missing" % path)
        return 1
    got = _fixture_rows(fx)
    exp = fx.get("expected") or {}
    bad = 0
    print("parity: %s" % fx.get("source", path))
    for tk in sorted(exp):
        want, have = exp[tk], got.get(tk) or {}
        for k in sorted(want):
            w, h = want[k], have.get(k)
            ok = (w == h)
            if not ok and isinstance(w, float) and isinstance(h, float):
                ok = abs(w - h) <= 1e-9 * max(1.0, abs(w), abs(h))
            if not ok:
                bad += 1
                print("  %-7s %-12s DRIFT want %r got %r" % (tk, k, w, h))
        if tk not in got:
            bad += 1
            print("  %-7s MISSING from the recomputation" % tk)
    for tk in sorted(set(got) - set(exp)):
        bad += 1
        print("  %-7s recomputed but absent from `expected`" % tk)
    # the invariants SPEC-62 s9 names explicitly
    for tk, row in got.items():
        if row["cv"] and row["pc_vol"] is not None and abs(row["pv"] / float(row["cv"]) - row["pc_vol"]) > 5e-5:
            bad += 1
            print("  %-7s pc_vol does not recompute from cv/pv" % tk)
        if row["coi"] and row["pc_oi"] is not None and abs(row["poi"] / float(row["coi"]) - row["pc_oi"]) > 5e-5:
            bad += 1
            print("  %-7s pc_oi does not recompute from coi/poi" % tk)
        for t in row["top"]:
            if t["px"] is not None and abs(t["n"] - t["v"] * 100.0 * t["px"]) > 1.0:
                bad += 1
                print("  %-7s top %s notional != v*100*px" % (tk, t["s"]))
    if bad:
        print("FAIL: %d value(s) drifted from the frozen fixture" % bad)
        return 1
    print("OK: parity holds across %d names" % len(exp))
    return 0


def freeze(path=FIXTURE):
    fx = load_json(path)
    if not fx:
        print("FAIL — fixture %s is missing; author its `chains` first" % path)
        return 1
    fx["expected"] = _fixture_rows(fx)
    with io.open(path, "w", encoding="utf-8") as fh:
        json.dump(fx, fh, indent=1, ensure_ascii=False, sort_keys=False)
        fh.write("\n")
    print("froze %d cases into %s" % (len(fx["expected"]), path))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="SPEC-62 phase 1 options-chain snapshot")
    ap.add_argument("--tickers", default=None, help="comma-separated subset for a small live run")
    ap.add_argument("--tickers-file", default=DEFAULT_TICKERS)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--iv-out", default=DEFAULT_IV_OUT)
    ap.add_argument("--earnings", default=EARNINGS)
    ap.add_argument("--latest", default=LATEST)
    ap.add_argument("--dry-run", action="store_true", help="compute and print, write nothing")
    ap.add_argument("--verify", action="store_true", help="recompute the parity fixture and compare")
    ap.add_argument("--freeze", action="store_true", help="re-freeze the fixture's expectations")
    a = ap.parse_args(argv)
    if a.verify:
        return verify()
    if a.freeze:
        return freeze()
    return run_live(a)


if __name__ == "__main__":
    sys.exit(main())
