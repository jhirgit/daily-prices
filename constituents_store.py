#!/usr/bin/env python3
"""
A closes-only store for the rotation ladder's constituents (B1 stage 3,
2026-11-20; design: board-refactor-2026-11 recon-3 section 3.4).

The ladder's rows hold some 2,500 listings, most of them not in prices.db, and
they do not belong there: prices.db also drives per-name technicals,
fundamentals and the "price history first" coverage rule, and GitHub caps a
file at 100 MB. rotation_members.py prices each constituent from Yahoo every
night; this store keeps what it fetched, so the next night asks Yahoo for the
last two weeks only and appends. Same number of requests, a fraction of the
data, and a night when Yahoo fails a symbol still has its stored history.

    where     data/cache/constituents/closes.json.gz -- under data/cache/, which
              is gitignored: NEVER committed. The workflow keeps it between runs
              in the GitHub Actions cache; a miss (evicted after 7 idle days, or
              a new runner image) simply rebuilds it from Yahoo with full requests.
    what      per Yahoo symbol: its trading currency, its dates and its adjusted
              closes, the last KEEP bars.
    re-bases  Yahoo re-bases adjusted closes backward after a dividend or a split
              (every earlier close times one factor). On the overlap between the
              stored tail and the new bars, one consistent ratio is applied to the
              stored history (exact for a re-base); an overlap whose ratios
              disagree by more than REBASE_SPREAD refetches the symbol in full.

rotation_members.json carries the store's state (`store`): hit or
miss_rebuilt or partial, how many symbols were appended, re-based, fetched in
full, new, served from the store because Yahoo failed them, or failed outright,
the file's size, and a warning past SIZE_WARN -- so a broken store shows
instead of silently costing history.

Tests: python test_constituents_store.py (offline).
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
STORE_PATH = os.path.join(HERE, "data", "cache", "constituents", "closes.json.gz")
KEEP = 520                 # bars kept per symbol: r252 + a 200-session average + room
TAIL_DAYS = 14             # calendar days asked for on a hit (a long weekend and a holiday)
FRESH_DAYS = 10            # a stored series whose last bar is older than this is fetched in full
REBASE_SPREAD = 0.002      # overlap ratios disagreeing by more than 0.2% = not a clean re-base
PARTIAL_SHARE = 0.20       # more than this share of symbols with no price at all = partial
SIZE_WARN = 25 * 1024 * 1024


def load(path=STORE_PATH):
    """(store, state): store = {"symbols": {sym: {"ccy", "d": [dates], "c": [closes]}}};
    state "hit", or "miss" when there is no file or it is unreadable (never raises)."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict) and isinstance(d.get("symbols"), dict):
            return d, "hit"
    except (OSError, ValueError, EOFError):
        pass
    return {"symbols": {}}, "miss"


def save(store, path=STORE_PATH):
    """Write-then-rename; returns the file's size in bytes."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(store, fh, separators=(",", ":"))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return os.path.getsize(path)


def _trim(e):
    if len(e["d"]) > KEEP:
        e["d"], e["c"] = e["d"][-KEEP:], e["c"][-KEEP:]
    return e


def merge(old, dates, closes, ccy):
    """(merged entry or None, how) for one symbol and today's bars. how:
    'new' (nothing stored), 'appended' (the overlap agrees), 'rebased' (the
    overlap is one consistent ratio: the stored history is scaled by it, then
    the new bars appended), 'refetch' (no overlap, or ratios that disagree:
    the caller fetches the symbol in full)."""
    if not old or not old.get("d"):
        return _trim({"ccy": ccy, "d": list(dates), "c": list(closes)}), "new"
    od = dict(zip(old["d"], old["c"]))
    ratios = [c / od[d] for d, c in zip(dates, closes) if od.get(d) and c]
    if not ratios:
        return None, "refetch"
    lo, hi = min(ratios), max(ratios)
    if lo <= 0 or hi / lo - 1.0 > REBASE_SPREAD:
        return None, "refetch"
    k = sum(ratios) / len(ratios)
    first = dates[0]
    keep = [(d, c * k) for d, c in zip(old["d"], old["c"]) if d < first]
    out = {"ccy": ccy or old.get("ccy"), "d": [d for d, _ in keep] + list(dates),
           "c": [c for _, c in keep] + list(closes)}
    if old.get("s0"):
        out["s0"] = old["s0"]
    return _trim(out), ("appended" if abs(k - 1.0) <= 1e-6 else "rebased")


def _days(a, b):
    return (dt.date.fromisoformat(b[:10]) - dt.date.fromisoformat(a[:10])).days


def fresh(e, start, today):
    """A stored series that reaches back to ``start`` and whose last bar is
    within FRESH_DAYS: only its tail is asked for."""
    # a listing younger than the window has no bar at `start`: its full fetch's own
    # start (s0) is what says the series is complete
    reach = min(e["d"][0], e.get("s0") or e["d"][0]) if (e and e.get("d")) else None
    return bool(reach and reach <= start and _days(e["d"][-1], today) <= FRESH_DAYS)


def stored_history(fetch, symbols, start, budget_s, store, today, stats=None):
    """The same answer as ``fetch(symbols, start, budget_s)`` -- {sym: (dates,
    closes, ccy)} -- through the store: a fresh symbol is asked for its last
    TAIL_DAYS and appended; every other symbol (new, stale, or a re-base that
    does not reconcile) is fetched from ``start``. A symbol Yahoo does not
    answer today is served from the store when it has a series (the caller's
    staleness rule then judges its last bar). ``stats`` (a dict) accumulates
    the counts across calls."""
    syms = store.setdefault("symbols", {})
    stats = {} if stats is None else stats
    for k in ("wanted", "appended", "rebased", "refetched", "new", "served_from_store", "failed"):
        stats.setdefault(k, 0)
    order = list(dict.fromkeys(symbols))
    stats["wanted"] += len(order)
    tail_start = (dt.date.fromisoformat(today) - dt.timedelta(days=TAIL_DAYS)).isoformat()
    short = [s for s in order if fresh(syms.get(s), start, today)]
    shorts = set(short)
    full = [s for s in order if s not in shorts]
    out, redo = {}, []
    got = fetch(short, tail_start, budget_s) if short else {}
    for s in short:
        h = got.get(s)
        if not h or not h[0]:
            e = syms[s]
            out[s] = (e["d"], e["c"], e.get("ccy"))
            stats["served_from_store"] += 1
            continue
        m, how = merge(syms[s], h[0], h[1], h[2])
        if how == "refetch":
            redo.append(s)
            continue
        syms[s] = m
        stats[how] += 1
        out[s] = (m["d"], m["c"], m.get("ccy"))
    redos = set(redo)
    want = full + redo
    got = fetch(want, start, budget_s) if want else {}
    for s in want:
        h = got.get(s)
        if not h or not h[0]:
            e = syms.get(s)
            if e and e.get("d") and s not in redos:
                out[s] = (e["d"], e["c"], e.get("ccy"))
                stats["served_from_store"] += 1
            else:
                stats["failed"] += 1
            continue
        stats["refetched" if s in syms else "new"] += 1
        syms[s] = _trim({"ccy": h[2], "d": list(h[0]), "c": list(h[1]), "s0": start})
        out[s] = (syms[s]["d"], syms[s]["c"], h[2])
    return out, stats


def status(stats, state, size):
    """The payload's `store` block."""
    st = "hit" if state == "hit" else "miss_rebuilt"
    if stats.get("failed", 0) > PARTIAL_SHARE * max(1, stats.get("wanted", 0)):
        st = "partial"
    out = dict(stats, store=st, bytes=size,
               note="constituent closes kept in the GitHub Actions cache, never committed; "
                    "a miss rebuilds it from Yahoo")
    if size > SIZE_WARN:
        out["warning"] = f"store is {size / 1048576:.1f} MB, past the {SIZE_WARN // 1048576} MB line"
    return out
