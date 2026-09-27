#!/usr/bin/env python3
"""
Offline tests for fetch_prices.rebase_history (review H1, 2026-09-26).

A synthetic "Yahoo" serves split-adjusted closes and dividend-adjusted closes
AS OF a given day, exactly the way Yahoo re-bases history after every ex-date
and split. The test replays the production loop -- a 7-session window fetched
every day, rebase_history then upsert_daily -- and asserts the stored history
equals what a fresh full fetch would return. A positive control replays the
same days WITHOUT the rebase (the pre-fix code) and must fail the same check.

No network: bars are a stand-in object with the two methods the code uses.

Run:  python test_fetch_prices.py
"""

import datetime as dt
import sqlite3
import sys

import fetch_prices as F

FAILS = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}" + ("" if ok else f", want {want!r}"))
    if not ok:
        FAILS.append(name)


def check_true(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{(' -- ' + detail) if detail else ''}")
    if not cond:
        FAILS.append(name)


# --------------------------------------------------------------------------
# a synthetic Yahoo
# --------------------------------------------------------------------------

class Row(dict):
    """pandas Series stand-in: .get(key) is all fetch_prices reads."""


class Stamp:
    def __init__(self, iso):
        self.iso = iso

    def strftime(self, fmt):
        assert fmt == "%Y-%m-%d"
        return self.iso


class Bars:
    """DataFrame stand-in: iterrows() and iloc[-1]."""

    def __init__(self, rows):
        self.rows = rows            # [(iso, Row)]
        self.empty = not rows

    def iterrows(self):
        for iso, r in self.rows:
            yield Stamp(iso), r

    @property
    def iloc(self):
        return [r for _, r in self.rows]


def sessions(n, start=dt.date(2026, 6, 1)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


class Yahoo:
    """Raw traded prices plus corporate actions; answers "what would Yahoo
    return for dates [a, b] if asked on day `asof`".

      Close(t)     = raw(t) / prod(split ratio S_e for split dates t < e <= asof)
      Adj Close(t) = Close(t) * prod(dividend factor f_e for ex-dates t < e <= asof)
      Volume(t)    = raw_vol(t) * prod(S_e ...)
    """

    def __init__(self, days, divs=None, splits=None):
        self.days = days
        self.divs = divs or {}        # ex-date -> factor f (e.g. 0.9975)
        self.splits = splits or {}    # split date -> ratio S (2.0 = 2-for-1)
        self.raw = {}
        px = 100.0
        for i, d in enumerate(days):
            if d in self.splits:
                px /= self.splits[d]
            px *= 1.0 + 0.003 * ((i * 7) % 5 - 2)      # deterministic wiggle
            self.raw[d] = px

    def view(self, a, b, asof, revise=None):
        rows = []
        for d in self.days:
            if d < a or d > b or d > asof:
                continue
            s_mult = 1.0
            f_mult = 1.0
            for e, S in self.splits.items():
                if d < e <= asof:
                    s_mult *= S
            for e, f in self.divs.items():
                if d < e <= asof:
                    f_mult *= f
            close = self.raw[d] / s_mult
            if revise and d in revise:
                close *= revise[d]
            rows.append((d, Row({
                "Open": close * 0.99, "High": close * 1.01, "Low": close * 0.98,
                "Close": close, "Adj Close": close * f_mult,
                "Volume": 1_000_000 * s_mult,
                "Dividends": 0.0,
                "Stock Splits": self.splits.get(d, 0.0) if d <= asof else 0.0,
            })))
        return Bars(rows)


def fresh_db():
    conn = sqlite3.connect(":memory:")
    F.init_db(conn)
    return conn


def stored(conn, tk="T"):
    return {d: (o, h, lo, c, a, v) for d, o, h, lo, c, a, v in conn.execute(
        "SELECT date, open, high, low, close, adj_close, volume FROM daily_prices "
        "WHERE ticker=? ORDER BY date", (tk,))}


def run_day(conn, y, asof, window=7, rebase=True, tk="T", revise=None):
    """One production run on day `asof`: fetch the last `window` sessions."""
    i = y.days.index(asof)
    bars = y.view(y.days[max(0, i - window + 1)], asof, asof, revise=revise)
    rb = F.rebase_history(conn, tk, bars) if rebase else None
    F.upsert_daily(conn, tk, bars)
    conn.commit()
    return rb


def worst_gap(conn, y, asof, tk="T"):
    """Largest relative error of stored close/adj/volume vs a fresh full fetch."""
    truth = {d: r for d, r in y.view(y.days[0], asof, asof).rows}
    worst = 0.0
    for d, (o, h, lo, c, a, v) in stored(conn, tk).items():
        t = truth[d]
        for got, want in ((c, t["Close"]), (a, t["Adj Close"]), (o, t["Open"]), (v, t["Volume"])):
            worst = max(worst, abs(got / want - 1.0))
    return worst


# --------------------------------------------------------------------------
print("\n# 1. a dividend inside the window re-bases every older row")
DAYS = sessions(60)
EX1 = DAYS[40]
y = Yahoo(DAYS, divs={EX1: 0.9975})
conn = fresh_db()
run_day(conn, y, DAYS[30], window=31)             # the initial backfill
check("backfill matches a fresh fetch", worst_gap(conn, y, DAYS[30]) < 1e-12, True)
results = [run_day(conn, y, d) for d in DAYS[31:]]
gap = worst_gap(conn, y, DAYS[-1])
check_true("after 29 daily runs across an ex-date, history == a fresh full fetch",
           gap < 1e-9, f"worst relative gap {gap:.2e}")
rebased = [r for r in results if r["status"] == "rebased"]
check("exactly one run re-based (the first run that saw the ex-date)", len(rebased), 1)
check_true("it applied the dividend factor", abs(rebased[0]["k_adj"] - 0.9975) < 1e-12,
           f"k_adj {rebased[0]['k_adj']!r}")
check("closes were not touched by a dividend", rebased[0]["k_split"], 1.0)
check_true("every other run was a no-op",
           all(r["status"] == "same" for r in results if r is not rebased[0]))
old = stored(conn)[DAYS[0]]
check_true("the oldest row carries the dividend", abs(old[4] / old[3] - 0.9975) < 1e-12)

print("\n# 1b. positive control: the pre-fix loop (no rebase) fails the same check")
ctrl = fresh_db()
run_day(ctrl, y, DAYS[30], window=31, rebase=False)
for d in DAYS[31:]:
    run_day(ctrl, y, d, rebase=False)
cgap = worst_gap(ctrl, y, DAYS[-1])
check_true("without the rebase, old rows keep the stale basis (the H1 bug)",
           abs(cgap - (1 / 0.9975 - 1)) < 1e-9, f"worst relative gap {cgap:.6f}")

print("\n# 2. two ex-dates inside one window compound into one factor")
y2 = Yahoo(DAYS, divs={DAYS[41]: 0.99, DAYS[43]: 0.995})
c2 = fresh_db()
run_day(c2, y2, DAYS[39], window=40)
rb = run_day(c2, y2, DAYS[45])                       # one late run after both
check("one re-base", rb["status"], "rebased")
check_true("k_adj is the product of both factors", abs(rb["k_adj"] - 0.99 * 0.995) < 1e-12,
           f"{rb['k_adj']!r}")
check_true("history == fresh fetch", worst_gap(c2, y2, DAYS[45]) < 1e-9)

print("\n# 3. a split: OHLC, adj and volume of every older row re-scale")
SPLIT = DAYS[50]
y3 = Yahoo(DAYS, divs={EX1: 0.9975}, splits={SPLIT: 4.0})
c3 = fresh_db()
run_day(c3, y3, DAYS[30], window=31)
res3 = [run_day(c3, y3, d) for d in DAYS[31:]]
g3 = worst_gap(c3, y3, DAYS[-1])
check_true("dividend then 4-for-1 split: history == fresh fetch", g3 < 1e-9, f"gap {g3:.2e}")
sp = [r for r in res3 if r["status"] == "rebased" and r["k_split"] != 1.0]
check("one split re-base", len(sp), 1)
check_true("k_split = 1/4", abs(sp[0]["k_split"] - 0.25) < 1e-15)
after = res3[DAYS.index(SPLIT) - 31 + 1:]
check_true("the week the split event stays in the window, it is not applied again",
           len(after) >= 7 and all(r["status"] == "same" for r in after),
           f"{[r['status'] for r in after]}")
v0 = stored(c3)[DAYS[0]][5]
check("volume of an old row x4 and stays an integer", (v0, type(v0).__name__), (4_000_000, "int"))

print("\n# 4. noise and bad bars never re-base")
c4 = fresh_db()
y4 = Yahoo(DAYS)
run_day(c4, y4, DAYS[30], window=31)
before = stored(c4)
rb = run_day(c4, y4, DAYS[32], revise={DAYS[26]: 1.0001})   # a one-cent revision at d0
check("a revised close at the overlap bar is not a dividend", rb["status"], "same")
check_true("older rows untouched", all(stored(c4)[d] == before[d] for d in DAYS[:26]))
before = stored(c4)
rb = run_day(c4, y4, DAYS[33], revise={DAYS[27]: 1.5})       # a bad bar, no split event
check("a 50% close jump with no split event is refused", rb["status"], "close-mismatch")
check_true("... and nothing older moved", all(stored(c4)[d] == before[d] for d in DAYS[:27]))
check_true("the warning names the fix", "repair_adj.py" in F.rebase_note(rb))

print("\n# 5. no overlap with stored history is reported, not guessed")
c5 = fresh_db()
run_day(c5, y, DAYS[20], window=21)
rb = run_day(c5, y, DAYS[45])                        # 25-session outage > 7-day window
check("a gap wider than the window", rb["status"], "no-overlap")
check_true("the warning names the fix", "repair_adj.py" in F.rebase_note(rb))
c6 = fresh_db()
check("a brand-new ticker is not a warning", run_day(c6, y, DAYS[10])["status"], "empty")
check("... and the quiet note is empty", F.rebase_note({"status": "empty"}), "")

print("\n# 6. repair_adj.plan puts a pre-fix history back on one basis")
import repair_adj as R  # noqa: E402

def as_triples(bars):
    return [(d, r["Close"], r["Adj Close"]) for d, r in bars.rows]

def apply_plan(conn, updates, tk="T"):
    conn.executemany("UPDATE daily_prices SET adj_close=? WHERE ticker=? AND date=?",
                     [(a, tk, d) for d, a in updates])

broken = fresh_db()                                  # the H1 shape, from 1b
run_day(broken, y, DAYS[30], window=31, rebase=False)
for d in DAYS[31:]:
    run_day(broken, y, d, rebase=False)
rows = broken.execute("SELECT date, close, adj_close FROM daily_prices WHERE ticker='T' "
                      "ORDER BY date").fetchall()
ups, st = R.plan(rows, as_triples(y.view(DAYS[0], DAYS[-1], DAYS[-1])))
# The run on the ex-date fetched DAYS[34..40]; everything older kept the stale basis.
check_true("exactly the rows older than the ex-date's window change",
           sorted(d for d, _ in ups) == DAYS[:DAYS.index(EX1) - 6], f"{len(ups)} rows")
apply_plan(broken, ups)
g6 = worst_gap(broken, y, DAYS[-1])
check_true("after the plan, history == a fresh full fetch", g6 < 1e-9, f"gap {g6:.2e}")
check("a second plan is a no-op", R.plan(
    broken.execute("SELECT date, close, adj_close FROM daily_prices ORDER BY date").fetchall(),
    as_triples(y.view(DAYS[0], DAYS[-1], DAYS[-1])))[1]["changed"], 0)

yv = as_triples(y.view(DAYS[0], DAYS[-1], DAYS[-1]))
gappy = [t for t in yv if t[0] != DAYS[5]]           # Yahoo dropped one date
st_rows = [(d, c, c) for d, c, _ in yv]             # stored adj == close (no dividend)
ups, st = R.plan(st_rows, gappy)
fac_next = dict((d, a / c) for d, c, a in gappy)[DAYS[6]]
check_true("a date Yahoo dropped takes the next date's factor",
           abs(dict(ups)[DAYS[5]] / st_rows[5][1] - fac_next) < 1e-12)
bad = list(st_rows)
bad[3] = (bad[3][0], bad[3][1] * 1.05, bad[3][2])     # one stored close off by 5%
ups, st = R.plan(bad, yv)
check("a close that disagrees with Yahoo is counted", st["mismatch"], 1)
check_true("... and its row is left alone", DAYS[3] not in dict(ups))
check("... but one bad row does not skip the ticker", st["skipped"], False)
worse = [(d, c * 2.0, a) if i < 10 else (d, c, a) for i, (d, c, a) in enumerate(st_rows)]
ups, st = R.plan(worse, yv)
check("an unabsorbed split (many mismatched rows) skips the ticker whole",
      (st["skipped"], ups), (True, []))
check("no Yahoo history: skipped, nothing planned", R.plan(st_rows, [])[1]["skipped"], True)

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s): {', '.join(FAILS)}")
    sys.exit(1)
print("all tests passed")
