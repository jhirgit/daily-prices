#!/usr/bin/env python3
"""
Session reversal/continuation analysis.

Steelmans and statistically tests the viral "session script" claim:
  "If the previous session REVERSED, the next session CONTINUES.
   If no session reversed, New York is the reversal."

Two tracks:

  TRACK 1 (single instrument -- the claim's literal form):
    Does the S&P 500 OVERNIGHT move (prev close -> today open, i.e. the
    "pre-market candle that printed while you slept") predict the INTRADAY
    open->close move? This is the sharpest test of "one candle tells you
    the whole day's direction."

  TRACK 2 (cross-market sessions -- the user's framing):
    Chronological chain within a calendar day:
        Asia (Nikkei/HangSeng/KOSPI, local open->close)
        -> London (FTSE 100, local open->close)
        -> US (S&P 500, local open->close)
    A session "continues" if its open->close sign == the prior session's sign,
    "reverses" if opposite. Test the guru's scenario conditional probabilities
    against the correct BASE RATE (not just 50%), with Wilson CIs and z-tests.

Everything uses only past/contemporaneous session signs -- no lookahead beyond
what a trader would actually know at each session's open.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "prices.db")
OUT = os.path.join(HERE, "data", "session_rotation.json")

# Region -> preferred index ticker, with fallbacks (first present wins).
ASIA_CANDIDATES = ["^N225", "^HSI", "^KS11"]
LONDON = "^FTSE"
US = "^GSPC"

Z = 1.959963984540054  # 95% two-sided


def load_ohlc(conn, ticker):
    """date -> (open, close) for a ticker, sorted, dropping bad rows."""
    rows = conn.execute(
        "SELECT date, open, close FROM daily_prices "
        "WHERE ticker=? AND open IS NOT NULL AND close IS NOT NULL "
        "AND open>0 AND close>0 ORDER BY date",
        (ticker,),
    ).fetchall()
    return {d: (o, c) for d, o, c in rows}


def o2c_sign(oc):
    o, c = oc
    if c > o:
        return 1
    if c < o:
        return -1
    return 0


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def wilson(k, n, z=Z):
    """Wilson score interval for a binomial proportion."""
    if n == 0:
        return (float("nan"), float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / d
    return (p, centre - half, centre + half)


def prop_z_test(k, n, p0):
    """Two-sided z-test that the true proportion == p0. Returns (p_hat, z, pvalue)."""
    if n == 0:
        return (float("nan"), float("nan"), float("nan"))
    p = k / n
    se = math.sqrt(p0 * (1 - p0) / n)
    if se == 0:
        return (p, float("nan"), float("nan"))
    z = (p - p0) / se
    pval = 2 * (1 - norm_cdf(abs(z)))
    return (p, z, pval)


def summarize_prop(k, n, base):
    """Bundle Wilson CI + a z-test vs a base rate."""
    p, lo, hi = wilson(k, n)
    _, z, pval = prop_z_test(k, n, base)
    return {
        "k": k, "n": n, "rate": round(p, 4) if n else None,
        "ci95": [round(lo, 4), round(hi, 4)] if n else None,
        "base": round(base, 4),
        "excess_over_base": round(p - base, 4) if n else None,
        "z_vs_base": round(z, 2) if n else None,
        "p_value": round(pval, 4) if n else None,
    }


# --------------------------------------------------------------------------
# TRACK 1: single-instrument overnight -> intraday (S&P 500)
# --------------------------------------------------------------------------
def track1_overnight_predicts_intraday(conn):
    rows = conn.execute(
        "SELECT date, open, high, low, close FROM daily_prices "
        "WHERE ticker=? AND open>0 AND close>0 ORDER BY date", (US,),
    ).fetchall()
    # need previous close for the overnight leg
    series = [(d, o, c) for d, o, h, l, c in rows]
    recs = []
    for i in range(1, len(series)):
        d, o, c = series[i]
        prev_c = series[i - 1][2]
        overnight = o / prev_c - 1.0            # prev close -> today open
        intraday = c / o - 1.0                  # today open -> today close
        recs.append((d, overnight, intraday))
    n = len(recs)
    # sign agreement: does overnight sign predict intraday sign?
    both = [(og, it) for _, og, it in recs if og != 0 and it != 0]
    agree = sum(1 for og, it in both if (og > 0) == (it > 0))
    up_over = [it for og, it in both if og > 0]
    dn_over = [it for og, it in both if og < 0]
    # correlation
    xs = [og for _, og, _ in recs]
    ys = [it for _, _, it in recs]
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs) / n)
    sy = math.sqrt(sum((y - my) ** 2 for y in ys) / n)
    corr = cov / (sx * sy) if sx and sy else float("nan")

    def mean(v):
        return sum(v) / len(v) if v else float("nan")

    intraday_up_rate = sum(1 for _, _, it in recs if it > 0) / n
    # drift-neutral: P(intraday up | overnight up) vs P(intraday up | overnight down)
    up_over_k = sum(1 for og, it in both if og > 0 and it > 0)
    up_over_n = sum(1 for og, it in both if og > 0)
    dn_over_k = sum(1 for og, it in both if og < 0 and it > 0)
    dn_over_n = sum(1 for og, it in both if og < 0)
    p_up_given_up = up_over_k / up_over_n if up_over_n else float("nan")
    p_up_given_dn = dn_over_k / dn_over_n if dn_over_n else float("nan")
    return {
        "instrument": US,
        "n_days": n,
        "date_range": [recs[0][0], recs[-1][0]] if recs else None,
        "corr_overnight_intraday": round(corr, 4),
        "sign_agreement": summarize_prop(agree, len(both), 0.5),
        "intraday_up_rate_unconditional": round(intraday_up_rate, 4),
        # the honest conditional: how much does overnight direction move the odds
        "P_intraday_up_given_overnight_up": round(p_up_given_up, 4),
        "P_intraday_up_given_overnight_down": round(p_up_given_dn, 4),
        "conditional_spread": round(p_up_given_up - p_up_given_dn, 4),
        "mean_intraday_when_overnight_up_pct": round(100 * mean(up_over), 4),
        "mean_intraday_when_overnight_down_pct": round(100 * mean(dn_over), 4),
    }


# --------------------------------------------------------------------------
# TRACK 2: cross-market session chain
# --------------------------------------------------------------------------
def build_session_chain(conn):
    """Per calendar date, assemble (asia_sign, london_sign, us_sign) where all
    three regional indices traded that date. Returns list of dicts in date order
    plus the asia ticker actually used."""
    us = load_ohlc(conn, US)
    ldn = load_ohlc(conn, LONDON)
    asia_ticker = next((t for t in ASIA_CANDIDATES if load_ohlc(conn, t)), None)
    asia = load_ohlc(conn, asia_ticker) if asia_ticker else {}

    common = sorted(set(us) & set(ldn) & set(asia)) if asia else []
    chain = []
    for d in common:
        a = o2c_sign(asia[d]); l = o2c_sign(ldn[d]); u = o2c_sign(us[d])
        if 0 in (a, l, u):
            continue
        chain.append({"date": d, "asia": a, "london": l, "us": u})
    return chain, asia_ticker


def track2_scenarios(chain, asia_ticker):
    if not chain:
        return {"available": False,
                "reason": "international indices not yet loaded",
                "asia_ticker": asia_ticker}

    n = len(chain)
    # Need previous day's US sign to define an Asia "reversal" (Asia vs prior NY).
    for i, row in enumerate(chain):
        row["prev_us"] = chain[i - 1]["us"] if i > 0 else None

    def rate(cond_pairs):
        """cond_pairs: list of (condition_bool, event_bool). Returns k,n over rows where condition True."""
        sub = [ev for cond, ev in cond_pairs if cond]
        return sum(1 for ev in sub if ev), len(sub)

    # --- base rates (unconditional continuation) ---
    london_cont_all = [(True, r["london"] == r["asia"]) for r in chain]
    us_cont_all = [(True, r["us"] == r["london"]) for r in chain]
    k_lc, n_lc = rate(london_cont_all)
    k_uc, n_uc = rate(us_cont_all)
    base_london_cont = k_lc / n_lc
    base_us_cont = k_uc / n_uc

    # --- Scenario 1: London reversed Asia  =>  US continues London ---
    s1 = [(r["london"] != r["asia"], r["us"] == r["london"]) for r in chain]
    k1, n1 = rate(s1)

    # --- Scenario 1b: London CONTINUED Asia => US continues London (contrast) ---
    s1b = [(r["london"] == r["asia"], r["us"] == r["london"]) for r in chain]
    k1b, n1b = rate(s1b)

    # --- Scenario 2: neither Asia nor London reversed (Asia==London, a "run")
    #     => US reverses (US != London) ---
    rows2 = [r for r in chain if r["prev_us"] is not None]
    # "no session reversed": Asia continued prior US AND London continued Asia
    s2 = [((r["asia"] == r["prev_us"]) and (r["london"] == r["asia"]),
           r["us"] != r["london"]) for r in rows2]
    k2, n2 = rate(s2)
    base_us_rev = 1 - base_us_cont  # base rate of US reversing London

    # --- Scenario 3: Asia reversed prior US => London AND US continue Asia ---
    s3_london = [(r["asia"] != r["prev_us"], r["london"] == r["asia"]) for r in rows2]
    s3_us = [(r["asia"] != r["prev_us"], r["us"] == r["asia"]) for r in rows2]
    k3l, n3l = rate(s3_london)
    k3u, n3u = rate(s3_us)

    # --- Second-order Markov test (the claim's engine):
    #     after a reversal, is the next session a continuation? ---
    # Build a flat chronological sign sequence: asia,london,us,asia,london,us...
    seq = []
    for r in chain:
        seq += [r["asia"], r["london"], r["us"]]
    flips = []  # for t>=2: (was_prev_a_flip, is_curr_a_flip)
    for t in range(2, len(seq)):
        prev_flip = seq[t - 1] != seq[t - 2]
        curr_flip = seq[t] != seq[t - 1]
        flips.append((prev_flip, curr_flip))
    # after a reversal(flip), guru says CONTINUATION (not a flip)
    after_rev_cont_k = sum(1 for pf, cf in flips if pf and not cf)
    after_rev_n = sum(1 for pf, cf in flips if pf)
    # after a continuation(run), guru says REVERSAL (flip)
    after_cont_rev_k = sum(1 for pf, cf in flips if (not pf) and cf)
    after_cont_n = sum(1 for pf, cf in flips if not pf)
    overall_flip_rate = sum(1 for _, cf in flips if cf) / len(flips)

    # --- session up-rates (drift check) ---
    up = {k: round(sum(1 for r in chain if r[k] == 1) / n, 4)
          for k in ("asia", "london", "us")}

    return {
        "available": True,
        "asia_ticker": asia_ticker,
        "n_days": n,
        "date_range": [chain[0]["date"], chain[-1]["date"]],
        "session_up_rates": up,
        "base_rates": {
            "london_continues_asia": round(base_london_cont, 4),
            "us_continues_london": round(base_us_cont, 4),
            "us_reverses_london": round(base_us_rev, 4),
        },
        "scenario1_londonReversal_usContinues":
            summarize_prop(k1, n1, base_us_cont),
        "scenario1b_londonContinued_usContinues":
            summarize_prop(k1b, n1b, base_us_cont),
        "scenario2_noReversal_usReverses":
            summarize_prop(k2, n2, base_us_rev),
        "scenario3_asiaReversal_londonContinues":
            summarize_prop(k3l, n3l, base_london_cont),
        "scenario3_asiaReversal_usContinues":
            summarize_prop(k3u, n3u, base_us_cont),
        "markov_after_reversal_expect_continuation":
            summarize_prop(after_rev_cont_k, after_rev_n, 1 - overall_flip_rate),
        "markov_after_continuation_expect_reversal":
            summarize_prop(after_cont_rev_k, after_cont_n, overall_flip_rate),
        "overall_session_flip_rate": round(overall_flip_rate, 4),
    }


def main():
    conn = sqlite3.connect(DB)
    chain, asia_ticker = build_session_chain(conn)
    result = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "definitions": {
            "session_sign": "sign(close - open) of each index's local cash session",
            "continuation": "session sign == prior session sign",
            "reversal": "session sign != prior session sign",
            "chain_order": "Asia -> London -> US within each calendar date",
        },
        "track1_overnight_predicts_intraday": track1_overnight_predicts_intraday(conn),
        "track2_cross_market_sessions": track2_scenarios(chain, asia_ticker),
    }
    conn.close()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(result, f, indent=2)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
