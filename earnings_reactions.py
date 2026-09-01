#!/usr/bin/env python3
"""earnings_reactions.py — post-print price reactions, COMPUTED, never authored.

Backlog #26 phase 3. The Earnings tab records a verdict for every print and then
never scores it. Every print on record already has a price reaction sitting in
prices.db, so the reaction is a DERIVED quantity: it is computed at emit from the
print date and the close series, and must never be typed into index.html. That is
the whole point — an authored number is an opinion with no error bar, and this
file exists so the scorecard cannot become one.

Lives beside technicals.py because it is the same kind of thing: a pure function
of prices.db emitted into a payload, gated by a parity fixture.

DISCLOSURE (#15). This repo is PUBLIC. Everything here is a public fact — a
ticker already in tickers.txt and a calendar date — and NOTHING book-revealing
enters it. Verdicts, calls, gates, position sizes and P&L stay on the jr-dash
side. This module takes (ticker, date) pairs in and returns price math out; it
does not know what any print meant. Keep it that way.

--------------------------------------------------------------------------
CONVENTION (derived, not assumed)
--------------------------------------------------------------------------
base   = the last settled session STRICTLY BEFORE the print date.
d(N)   = the close N sessions after `base`, so d1 is the first session ON OR
         AFTER the print date. Weekends and holidays fall out for free because
         the walk is over the session index, not the calendar.
return = (close(d(N)) / close(base) - 1) * 100, in percent.
excess = return - the SAME-WINDOW return of the benchmark, in percentage points,
         aligned by DATE (identical base_date and target date), not by offset.

This convention was not chosen a priori. Backlog #26 published four horizons for
five prints (AEHR, NBIS, DDOG, RDDT, AOSL) and this is the only convention that
reproduces all fourteen of those numbers; anchoring on the print-date close
instead reproduces none of them. Those fourteen values are frozen in
parity/earnings_reactions_expected.json and asserted by --verify, so the
convention cannot drift silently the way an undocumented one would.

--------------------------------------------------------------------------
KNOWN LIMIT — read before trusting d1
--------------------------------------------------------------------------
The EARN ledger does not record whether a print was AMC or BMO, and the prose
does not either: a scan of all 20 `chg` fields finds zero AMC/BMO markers. For an
AMC print the news lands after the print-date close, so d1 as defined here is the
session BEFORE the market could react, and it reads ~0 for reasons that have
nothing to do with the print. RDDT is the worked example: d1 = 0.0 exactly, then
d5 = -12.8. That 0.0 is not "the market shrugged", it is "the market had not
opened yet".

So: d5 and d10 are sound for every print; d1 is only interpretable once each
print carries a session marker. This module therefore emits `session: null` and
`d1_is_pre_reaction: null` rather than guessing, and REFUSES to infer AMC/BMO
from the price move itself — that would be inferring the timing from the very
reaction being measured, which is circular. Supply `session` in the input to
resolve it; the field is a verifiable public fact, not a judgement.

--------------------------------------------------------------------------
RUN
--------------------------------------------------------------------------
  python earnings_reactions.py --prints prints.json [--out reactions.json]
  python earnings_reactions.py --verify          # assert the #26 parity fixture
  python earnings_reactions.py --self-test       # convention + edge cases

`prints.json` is a list of objects, each {"ticker": "AOSL", "date": "2026-08-12"}
with an optional "session": "amc" | "bmo". Nothing else is read, so no verdict
can be smuggled in.
"""

import argparse
import json
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "prices.db")
DEFAULT_TICKERS = os.path.join(HERE, "tickers.txt")
PARITY = os.path.join(HERE, "parity", "earnings_reactions_expected.json")

HORIZONS = (1, 5, 10)

# Cohort benchmark, reusing the mapping technicals.py already derives from the
# "# --- ... ---" headers in tickers.txt so the two cannot disagree. SPY is
# carried alongside for every name as a common cross-cohort denominator: without
# it a semis print and a software print have no shared axis.
GROUP_PROXY = [
    (r"semiconduct|memory|storage|optic|network", "SMH"),
    (r"ai compute|datacenter", "SMH"),
    (r"power|energy", "SPY"),
    (r"miner|royalty|metals", "GDX"),
    (r"software", "IGV"),
    (r"macro|benchmark", None),
    (r"broad|thematic|crypto", "SPY"),
]
BASELINE = "SPY"


def load_groups(path=DEFAULT_TICKERS):
    """ticker -> {group, proxy}, parsed from tickers.txt section headers."""
    groups, cur = {}, "Ungrouped"
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            hdr = re.match(r"^#\s*-{2,}\s*(.+?)\s*-{2,}\s*$", line.strip())
            if hdr:
                cur = hdr.group(1)
                continue
            sym = line.split("#", 1)[0].strip()
            if sym:
                proxy = "SPY"
                for pat, px in GROUP_PROXY:
                    if re.search(pat, cur, re.I):
                        proxy = px
                        break
                groups[sym.upper()] = {"group": cur, "proxy": proxy}
    return groups


class Series:
    """Close series for one ticker, indexed by session."""

    __slots__ = ("ticker", "dates", "closes", "_pos")

    def __init__(self, ticker, rows):
        self.ticker = ticker
        self.dates = [r[0] for r in rows]
        self.closes = [r[1] for r in rows]
        self._pos = {d: i for i, d in enumerate(self.dates)}

    def __len__(self):
        return len(self.dates)

    def index_on_or_after(self, date):
        for i, d in enumerate(self.dates):
            if d >= date:
                return i
        return None

    def index_of(self, date):
        return self._pos.get(date)


def load_series(conn, ticker):
    rows = conn.execute(
        "SELECT date, COALESCE(adj_close, close) FROM daily_prices "
        "WHERE ticker = ? AND COALESCE(adj_close, close) IS NOT NULL "
        "ORDER BY date",
        (ticker.upper(),),
    ).fetchall()
    return Series(ticker.upper(), rows)


def pct(new, old):
    if old in (None, 0) or new is None:
        return None
    return (new / old - 1.0) * 100.0


def window_return(series, base_idx, n):
    """Return (date, close, pct) n sessions after base_idx, or None if short."""
    if base_idx is None or base_idx < 0:
        return None
    tgt = base_idx + n
    if tgt >= len(series):
        return None
    return (series.dates[tgt], series.closes[tgt],
            pct(series.closes[tgt], series.closes[base_idx]))


def bench_return(series, base_date, target_date):
    """Benchmark return over the SAME dates. Aligned by date, not by offset."""
    if series is None or target_date is None:
        return None
    b = series.index_of(base_date)
    t = series.index_of(target_date)
    if b is None or t is None:
        return None
    return pct(series.closes[t], series.closes[b])


def compute_one(conn, cache, groups, ticker, date, session=None):
    ticker = ticker.upper()
    if ticker not in cache:
        cache[ticker] = load_series(conn, ticker)
    s = cache[ticker]

    out = {
        "ticker": ticker,
        "print_date": date,
        "session": session,           # "amc" | "bmo" | None — never inferred
        "base_date": None,
        "proxy": None,
        "baseline": BASELINE,
        "complete": False,
        "notes": [],
    }
    for n in HORIZONS:
        out["d%d" % n] = None

    if len(s) == 0:
        out["notes"].append("no price history for %s" % ticker)
        return out

    at = s.index_on_or_after(date)
    if at is None:
        out["notes"].append("print date %s is after the last settled session %s"
                            % (date, s.dates[-1]))
        return out
    base_idx = at - 1
    if base_idx < 0:
        out["notes"].append("no settled session before %s" % date)
        return out

    base_date = s.dates[base_idx]
    out["base_date"] = base_date
    out["base_close"] = round(s.closes[base_idx], 6)
    if s.dates[at] != date:
        out["notes"].append("print date %s is not a session; d1 anchors on %s"
                            % (date, s.dates[at]))

    proxy_sym = groups.get(ticker, {}).get("proxy")
    out["proxy"] = proxy_sym
    out["group"] = groups.get(ticker, {}).get("group")
    if proxy_sym and proxy_sym not in cache:
        cache[proxy_sym] = load_series(conn, proxy_sym)
    if BASELINE not in cache:
        cache[BASELINE] = load_series(conn, BASELINE)
    proxy_s = cache.get(proxy_sym) if proxy_sym else None
    base_s = cache.get(BASELINE)

    filled = 0
    for n in HORIZONS:
        w = window_return(s, base_idx, n)
        if w is None:
            # Honest null: the window has not closed yet. Never zero-fill.
            out["d%d" % n] = None
            continue
        tgt_date, tgt_close, r = w
        pr = bench_return(proxy_s, base_date, tgt_date)
        br = bench_return(base_s, base_date, tgt_date)
        out["d%d" % n] = {
            "date": tgt_date,
            "close": round(tgt_close, 6),
            "ret": round(r, 2) if r is not None else None,
            "proxy_ret": round(pr, 2) if pr is not None else None,
            "vs_proxy": round(r - pr, 2) if (r is not None and pr is not None) else None,
            "baseline_ret": round(br, 2) if br is not None else None,
            "vs_baseline": round(r - br, 2) if (r is not None and br is not None) else None,
        }
        filled += 1

    # Session-aware horizons. For an AMC print the news lands AFTER the
    # print-date close, so the base above is one session too early and d1 is a
    # pre-reaction day. These re-anchor on the print-date close and are the
    # numbers to READ when session is known.
    #
    # Emitted ALONGSIDE the primary fields rather than replacing them: the
    # primary convention is frozen by the #26 parity fixture, and silently
    # moving it would break the only oracle the day-0 convention was ever
    # derived from. Nothing here is inferred — `adj` appears only when `session`
    # was supplied from a documented source.
    if session == "amc" and s.dates[at] == date:
        adj_base = at
        out["adj_base_date"] = s.dates[adj_base]
        adj = {}
        for n in HORIZONS:
            w = window_return(s, adj_base, n)
            if w is None:
                adj["d%d" % n] = None
                continue
            tgt_date, tgt_close, r = w
            pr = bench_return(proxy_s, s.dates[adj_base], tgt_date)
            adj["d%d" % n] = {
                "date": tgt_date,
                "ret": round(r, 2) if r is not None else None,
                "vs_proxy": round(r - pr, 2) if (r is not None and pr is not None) else None,
            }
        out["adj"] = adj
        out["notes"].append(
            "AMC: primary d1 is the pre-reaction session; read `adj` instead")

    out["complete"] = filled == len(HORIZONS)
    if not out["complete"]:
        have = [n for n in HORIZONS if out["d%d" % n] is not None]
        out["notes"].append(
            "window incomplete — only d%s settled as of %s"
            % ("/d".join(str(n) for n in have) if have else "(none)", s.dates[-1]))
    if session is None:
        out["notes"].append(
            "session (amc/bmo) not recorded — d1 may be a pre-reaction session")
    else:
        out["d1_is_pre_reaction"] = (session == "amc")
    return out


def compute(prints, db=DEFAULT_DB, tickers=DEFAULT_TICKERS):
    conn = sqlite3.connect(db)
    try:
        groups = load_groups(tickers)
        cache = {}
        return [compute_one(conn, cache, groups, p["ticker"], p["date"],
                            p.get("session"))
                for p in prints]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# parity — the #26 published numbers, frozen
# ---------------------------------------------------------------------------

def verify(db=DEFAULT_DB, tickers=DEFAULT_TICKERS, path=PARITY):
    with open(path, "r", encoding="utf-8") as fh:
        fixture = json.load(fh)
    rows = compute([{"ticker": e["ticker"], "date": e["date"]}
                    for e in fixture["cases"]], db, tickers)
    tol = fixture.get("tolerance_pp", 0.05)
    bad = 0
    checked = 0

    # SMH excess is asserted separately from the displayed proxy: #26 benchmarked
    # everything against SMH, the emitter now uses the per-name cohort proxy, and
    # this proves the difference is that decision and not an arithmetic slip.
    conn = sqlite3.connect(db)
    smh = load_series(conn, "SMH")
    conn.close()

    print("parity: %s" % fixture.get("source", path))
    for exp, got in zip(fixture["cases"], rows):
        cells = []
        for n in HORIZONS:
            want = exp.get("d%d" % n)
            if want is None:
                cells.append("d%-2d    --   " % n)
                continue
            checked += 1
            cell = got.get("d%d" % n)
            have = cell["ret"] if cell else None
            if have is None:
                cells.append("d%-2d %8s MISS" % (n, "None"))
                bad += 1
            elif abs(have - want) > tol:
                cells.append("d%-2d %+8.2f FAIL(exp %+.1f)" % (n, have, want))
                bad += 1
            else:
                cells.append("d%-2d %+8.2f ok" % (n, have))
        print("  %-6s %s  %s" % (exp["ticker"], exp["date"], " | ".join(cells)))

        want_smh = exp.get("d5_vs_smh")
        if want_smh is not None and got.get("d5"):
            checked += 1
            sr = bench_return(smh, got["base_date"], got["d5"]["date"])
            have_smh = (got["d5"]["ret"] - sr) if sr is not None else None
            if have_smh is None:
                print("         d5 vs SMH  MISS")
                bad += 1
            elif abs(have_smh - want_smh) > tol:
                print("         d5 vs SMH %+7.2fpp FAIL (exp %+.1f)"
                      % (have_smh, want_smh))
                bad += 1
            else:
                shown = got["d5"].get("vs_proxy")
                tag = ""
                if got.get("proxy") != "SMH":
                    tag = ("   [displayed vs %s = %+.2fpp, deliberate]"
                           % (got.get("proxy"), shown))
                print("         d5 vs SMH %+7.2fpp ok%s" % (have_smh, tag))

    print("\n%d/%d assertions match within %.2fpp" % (checked - bad, checked, tol))
    return bad == 0


def self_test(db=DEFAULT_DB, tickers=DEFAULT_TICKERS):
    """Edge cases the convention has to survive."""
    ok = True

    def check(name, cond):
        nonlocal ok
        print("  %-58s %s" % (name, "ok" if cond else "FAIL"))
        ok = ok and bool(cond)

    conn = sqlite3.connect(db)
    groups = load_groups(tickers)
    cache = {}

    # 1. base is strictly before the print date
    r = compute_one(conn, cache, groups, "AOSL", "2026-08-12")
    check("base_date strictly before print date", r["base_date"] < "2026-08-12")
    check("d1 lands on or after the print date", r["d1"]["date"] >= "2026-08-12")

    # 2. a weekend print date rolls forward without crashing
    r = compute_one(conn, cache, groups, "AOSL", "2026-08-15")  # Saturday
    check("weekend print date resolves", r["base_date"] is not None)

    # 3. an unsettled window yields null, never 0.0
    r = compute_one(conn, cache, groups, "NVDA", "2026-08-26")
    check("unsettled d10 is None, not 0.0", r["d10"] is None)
    check("incomplete window flagged", r["complete"] is False)

    # 4. proxy assignment matches the cohort map
    r = compute_one(conn, cache, groups, "DDOG", "2026-08-06")
    check("software name proxies to IGV", r["proxy"] == "IGV")
    r = compute_one(conn, cache, groups, "AOSL", "2026-08-12")
    check("semis name proxies to SMH", r["proxy"] == "SMH")

    # 5. excess is computed against the same dates
    d5 = r["d5"]
    check("vs_proxy == ret - proxy_ret",
          abs(d5["vs_proxy"] - (d5["ret"] - d5["proxy_ret"])) < 0.011)
    check("baseline carried for every name", d5["vs_baseline"] is not None)

    # 6. unknown ticker degrades instead of throwing
    r = compute_one(conn, cache, groups, "ZZZZ", "2026-08-12")
    check("unknown ticker returns a note, not an exception",
          r["d1"] is None and r["notes"])

    # 7. session is never inferred
    check("session stays None when not supplied", r["session"] is None)

    conn.close()
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--prints", help="JSON list of {ticker, date[, session]}")
    ap.add_argument("--out", help="write JSON here instead of stdout")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--tickers", default=DEFAULT_TICKERS)
    ap.add_argument("--verify", action="store_true", help="assert the #26 fixture")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--table", action="store_true", help="human-readable table")
    a = ap.parse_args()

    if a.verify:
        sys.exit(0 if verify(a.db, a.tickers) else 1)
    if a.self_test:
        print("self-test:")
        sys.exit(0 if self_test(a.db, a.tickers) else 1)

    if not a.prints:
        ap.error("--prints is required (or use --verify / --self-test)")
    with open(a.prints, "r", encoding="utf-8") as fh:
        prints = json.load(fh)
    rows = compute(prints, a.db, a.tickers)

    if a.table:
        print("%-6s %-10s %-5s %-6s %8s %8s %8s %9s %9s" % (
            "TKR", "PRINT", "PXY", "BASE", "d1", "d5", "d10", "d5vsPXY", "d5vsSPY"))
        for r in rows:
            def g(n, k="ret"):
                c = r.get("d%d" % n)
                return "%+8.1f" % c[k] if c and c.get(k) is not None else "       -"
            print("%-6s %-10s %-5s %-6s %s %s %s %s %s" % (
                r["ticker"], r["print_date"], r.get("proxy") or "-",
                (r.get("base_date") or "-")[5:], g(1), g(5), g(10),
                g(5, "vs_proxy"), g(5, "vs_baseline")))
        incomplete = [r["ticker"] for r in rows if not r["complete"]]
        if incomplete:
            print("\nincomplete windows (honest nulls, not zeros): %s"
                  % ", ".join(incomplete))
        return

    payload = json.dumps(rows, indent=1)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
        print("wrote %s (%d prints)" % (a.out, len(rows)))
    else:
        print(payload)


if __name__ == "__main__":
    main()
