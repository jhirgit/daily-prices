#!/usr/bin/env python3
"""
Event study: on this book's own history, what actually followed the
momentum-structure states the dashboard displays?

For every name and every session, compute three lookahead-free flags --
  stacked   close > EMA20 > EMA50 > EMA200            (trend structure)
  coiled    ATR14% at/below its own trailing-year p20  (volatility state)
  buyers    50d up-day volume >= 1.2x down-day volume  (participation)
-- then record the forward 21- and 63-session return (adjusted closes) from
every day each state held, against a baseline of every day the flags were
computable at all. `full_setup` is the conjunction.

Writes data/momentum_evidence.json; technicals.py inlines it into
technicals.json so the dashboard can print base rates next to the states.

Honesty constraints, enforced in code and restated in the output:
  * Flags use trailing windows only. Swing structure (pivots) is EXCLUDED from
    the conditions because a pivot confirms SWING_K bars late -- using it would
    peek. It stays display-only on the dashboard.
  * Overlapping windows: consecutive qualifying days are one episode; both the
    day count and the episode count are reported.
  * Survivorship: this watchlist is names that earned their way onto the book.
    Base rates describe THESE names' past, not a tradable expectation.
"""

from __future__ import annotations

import json
import os
import sqlite3
import statistics
from bisect import bisect_right, insort
from collections import deque
from datetime import datetime, timezone

import technicals as T

FWD = (21, 63)
CONDITIONS = ("full_setup", "stacked_only", "coiled_only", "buyers_only", "baseline")


def day_flags(highs, lows, closes, vols):
    """Per-session flag arrays; None where a flag is not yet computable."""
    n = len(closes)
    e20, e50, e200 = (T.ema(closes, k) for k in T.STACK_EMAS)
    stacked = [None] * n
    for i in range(n):
        if e200[i] is not None:
            stacked[i] = closes[i] > e20[i] > e50[i] > e200[i]

    atrp = T.atr_pct_series(highs, lows, closes)
    coiled = [None] * n
    buf, q = [], deque()
    for i, v in enumerate(atrp):
        if v is None:
            continue
        insort(buf, v)
        q.append(v)
        if len(q) > T.ATR_RANK_WIN:
            buf.remove(q.popleft())
        if len(buf) == T.ATR_RANK_WIN:
            coiled[i] = (bisect_right(buf, v) / len(buf)) <= T.CONTRACT_MAX

    buyers = [None] * n
    up = dn = 0.0
    updn = deque()
    for i in range(1, n):
        v = float(vols[i] or 0)
        u = v if closes[i] > closes[i - 1] else 0.0
        d = v if closes[i] < closes[i - 1] else 0.0
        updn.append((u, d))
        up += u
        dn += d
        if len(updn) > 50:
            ou, od = updn.popleft()
            up -= ou
            dn -= od
        if len(updn) == 50 and (up + dn) > 0:
            buyers[i] = (up >= T.UDVOL_CONFIRM * dn) if dn > 0 else True
    return stacked, coiled, buyers


def main() -> int:
    conn = sqlite3.connect(T.DEFAULT_DB)
    syms = [r[0] for r in conn.execute(
        "SELECT DISTINCT ticker FROM daily_prices ORDER BY ticker")]

    pools = {c: {f: [] for f in FWD} for c in CONDITIONS}
    episodes = {c: 0 for c in CONDITIONS}
    names = {c: set() for c in CONDITIONS}
    span = [None, None]
    used = 0

    for sym in syms:
        rows = T.load_bars(conn, sym)
        if len(rows) < T.MIN_BARS:
            continue
        dates = [r[0] for r in rows]
        opens = [r[1] if r[1] is not None else r[4] for r in rows]  # noqa: F841
        highs = [r[2] if r[2] is not None else r[4] for r in rows]
        lows = [r[3] if r[3] is not None else r[4] for r in rows]
        closes = [r[4] for r in rows]
        vols = [r[5] or 0 for r in rows]
        adjs = [r[6] if r[6] is not None else r[4] for r in rows]
        n = len(closes)
        used += 1
        span[0] = min(span[0] or dates[0], dates[0])
        span[1] = max(span[1] or dates[-1], dates[-1])

        stacked, coiled, buyers = day_flags(highs, lows, closes, vols)
        prev = {c: False for c in CONDITIONS}
        for t in range(n):
            if stacked[t] is None or coiled[t] is None or buyers[t] is None:
                prev = {c: False for c in CONDITIONS}
                continue
            state = {
                "full_setup": stacked[t] and coiled[t] and buyers[t],
                "stacked_only": stacked[t],
                "coiled_only": coiled[t],
                "buyers_only": buyers[t],
                "baseline": True,
            }
            for c, on in state.items():
                if not on:
                    prev[c] = False
                    continue
                took = False
                for f in FWD:
                    if t + f < n and adjs[t] and adjs[t + f]:
                        pools[c][f].append(adjs[t + f] / adjs[t] - 1)
                        took = True
                if took:
                    names[c].add(sym)
                    if not prev[c]:
                        episodes[c] += 1
                prev[c] = on

    conn.close()

    def summarize(vals):
        if not vals:
            return {"n": 0, "median": None, "mean": None, "hit_rate": None}
        return {
            "n": len(vals),
            "median": round(statistics.median(vals), 4),
            "mean": round(statistics.fmean(vals), 4),
            "hit_rate": round(sum(1 for v in vals if v > 0) / len(vals), 3),
        }

    out = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "span": {"from": span[0], "to": span[1], "names_used": used},
        "params": {
            "stacked": "close > EMA20 > EMA50 > EMA200",
            "coiled": f"ATR{T.ATR_N}% <= p{int(T.CONTRACT_MAX * 100)} of own trailing {T.ATR_RANK_WIN} sessions",
            "buyers": f"50d up-day volume >= {T.UDVOL_CONFIRM}x down-day volume",
            "forward_horizons_sessions": list(FWD),
            "returns": "adjusted closes (dividends count)",
        },
        "conditions": {
            c: {
                "n_names": len(names[c]),
                "n_episodes": episodes[c],
                **{f"fwd{f}": summarize(pools[c][f]) for f in FWD},
            }
            for c in CONDITIONS
        },
        "notes": [
            "Flags are lookahead-free (trailing windows only); swing pivots are excluded "
            "from conditions because they confirm late.",
            "Consecutive qualifying days overlap heavily -- episodes is the honest unit.",
            "Survivorship-biased by construction: these are names that earned their way "
            "onto the watchlist. A description of their past, not an expectation.",
        ],
    }

    path = os.path.join(T.DEFAULT_OUT, "momentum_evidence.json")
    os.makedirs(T.DEFAULT_OUT, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
        fh.write("\n")

    print(f"{used} names, {span[0]} .. {span[1]}")
    hdr = f"{'condition':14s} {'names':>5} {'episodes':>8} " + \
          "".join(f"{'fwd' + str(f) + ' med':>10}{'hit':>6}{'n':>9}" for f in FWD)
    print(hdr)
    for c in CONDITIONS:
        row = out["conditions"][c]
        line = f"{c:14s} {row['n_names']:>5} {row['n_episodes']:>8} "
        for f in FWD:
            s = row[f"fwd{f}"]
            med = "—" if s["median"] is None else f"{s['median']:+.2%}"
            hit = "—" if s["hit_rate"] is None else f"{s['hit_rate']:.0%}"
            line += f"{med:>10}{hit:>6}{s['n']:>9,}"
        print(line)
    print(f"\nWrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
