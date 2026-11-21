#!/usr/bin/env python3
"""
Offline tests for pricedb.py (the one way to open the price database) and
tools/split_prices_db.py (the split, and --rejoin, the rollback).

  1. single-file and split layouts read identically (checksum + reader queries)
  2. new rows land in the live file; archive files stay byte-identical; old dates
     route to their archive part; a re-base reaches every file; a write through
     the view is refused
  3. the production fetch loop (fetch_prices.rebase_history + upsert_daily) on a
     single file and on a split set gives the same stored history, every bit:
     one dividend, two and twelve successive dividends, a re-base whose overlap
     bar is before the boundary, a dividend then a split
  4. roll moves a completed year, keeps the checksum, starts
     a new part past the size cap; refuses an open year and a non-1-January
  5. pairing: files from different versions, a missing part, both layouts present
  6. --rejoin rebuilds one file with the same checksum; the split is idempotent
  7. Node twin (pricedb.js) reads the same rows through the same view text
  8. no direct opens of the price database outside the helper (repo grep)

Each section carries a failing control: the check is shown to catch the fault.

Run:  python test_pricedb.py            (all; offline, temp files only)
      python test_pricedb.py --scan     (section 8 only, over the jr-dash checkout
                                         too when this repo sits inside one)
"""

import contextlib
import datetime as dt
import hashlib
import io
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tools"))
import pricedb as P  # noqa: E402
import split_prices_db as S  # noqa: E402

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


def raises(fn):
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        return str(e) or type(e).__name__
    return None


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def quiet(fn, *a, **kw):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **kw)


# =========================================================================== 8. the scanner
SCAN_PAT = re.compile(r"sqlite3\.connect\(|_sq\.connect\(|DatabaseSync\(|new Database\(|better-sqlite3"
                      r"|require\(\s*['\"]sqlite3['\"]\s*\)")
DOC_PAT = re.compile(r"connect\([^)]*prices\.db|sqlite3 [^|\n]*prices(-live|-archive[^ ]*)?\.db")
# Every open that is NOT the price database, or that builds one, with the reason.
# None = the whole file is allowed; a list = only lines containing one of these.
ALLOW = {
    "daily-prices/pricedb.py": None,                          # the helper
    "daily-prices/pricedb.js": None,                          # its Node twin
    "daily-prices/tools/split_prices_db.py": None,            # builds / rejoins the files
    "daily-prices/test_pricedb.py": None,                     # this file: fixtures + controls
    "daily-prices/test_thesis_gauge.py": [":memory:"],        # in-memory fixture
    "daily-prices/test_technicals.py": [":memory:", "_sq.connect(_dbp)"],  # fixtures: memory + temp copy
    "daily-prices/test_fetch_prices.py": [":memory:"],        # in-memory fixture
    "daily-prices/test_pipeline_e2e.py": ["sqlite3.connect(DB)"],        # builds its temp fixture
    "daily-prices/test_crowd.py": ["sqlite3.connect(DB)"],               # builds its temp fixture
    "daily-prices/test_mcp_server.py": ["sqlite3.connect(db)"],          # builds its temp fixture
    "daily-prices/test_fundamentals.py": ["sqlite3.connect(self.db)"],   # builds its temp fixture
    "tools/test_metrics_history.py": ["sqlite3.connect(path)"],          # builds its temp fixture
    "tools/test_coverage_check.js": ["con=sqlite3.connect(sys.argv[1])"],  # builds its temp fixture
    "filing-delta/fd/db.py": ["sqlite3.connect(DB_PATH)"],               # its own pair_scores db
}
CODE_EXT = (".py", ".js", ".mjs", ".cjs", ".sh", ".yml", ".yaml", ".ps1", ".cmd")
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "data", "archive", "tmp", "out",
             "worktrees", "parity", "fixtures"}


def scan_file(path, rel, code):
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return []
    allow = ALLOW.get(rel, [])
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        if code and SCAN_PAT.search(line):
            if allow is None or any(a in line for a in allow):
                continue
            out.append((rel, i, line.strip()))
        elif not code and DOC_PAT.search(line) and "pricedb" not in line:
            out.append((rel, i, line.strip()))
    return out


def scan_tree(root, prefix):
    """[(relpath, lineno, line)] of every direct database open not in ALLOW, and of
    every doc line that tells a reader to open the price file directly."""
    bad = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            code = fn.endswith(CODE_EXT) or fn == "Dockerfile"
            doc = fn.endswith(".md") and fn != "CHANGELOG.md"
            if code or doc:
                p = os.path.join(dirpath, fn)
                bad += scan_file(p, (prefix + os.path.relpath(p, root)).replace(os.sep, "/"), code)
    return bad


def section_scan(scan_parent):
    print("\n# 8. no direct opens of the price database outside pricedb")
    bad = scan_tree(HERE, "daily-prices/")
    parent = os.path.dirname(HERE)
    scanned = ["daily-prices"]
    if scan_parent and os.path.exists(os.path.join(parent, "tools", "test_all.sh")):
        for sub in ("tools", "position-sync", "functions", "cron-dispatch", "dcf", "filing-delta",
                    ".claude/skills"):
            d = os.path.join(parent, *sub.split("/"))
            if os.path.isdir(d):
                bad += scan_tree(d, sub + "/")
                scanned.append(sub)
        for f in ("CLAUDE.md", "ARCHITECTURE.md", "README.md"):
            bad += scan_file(os.path.join(parent, f), f, False)
        scanned.append("jr-dash *.md")
    for rel, i, line in bad:
        print(f"    {rel}:{i}: {line[:140]}")
    check(f"direct opens outside pricedb ({', '.join(scanned)})", len(bad), 0)
    tmp = tempfile.mkdtemp(prefix="pricedb_scan_")
    try:
        with open(os.path.join(tmp, "reader.py"), "w") as f:
            f.write("import sqlite3\nconn = sqlite3.connect('prices.db')\n")
        with open(os.path.join(tmp, "reader.js"), "w") as f:
            f.write("const db = new DatabaseSync(DB, { readOnly: true });\n")
        with open(os.path.join(tmp, "HOWTO.md"), "w") as f:
            f.write("Run `python -c \"import sqlite3;sqlite3.connect('daily-prices/prices.db')\"`\n")
        check("control: a planted python open, node open and doc one-liner are all flagged",
              len(scan_tree(tmp, "x/")), 3)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if "--scan" in sys.argv:
    section_scan(scan_parent=True)
    print("\nFAILED: " + ", ".join(FAILS) if FAILS else "\nall pricedb scan checks passed")
    sys.exit(1 if FAILS else 0)


# =========================================================================== fixtures

def weekdays(a, b):
    out, d = [], a
    while d <= b:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def build_single(path):
    """A small but awkward single-file database: two tickers over 2019-2026, an
    archive-only ticker, a live-only ticker, NULLs, integer-valued reals, spot
    quotes on both sides of the boundary, rows inserted out of date order."""
    conn = sqlite3.connect(path)
    conn.executescript(P.SCHEMA)
    rows = []
    for tk, a, b in (("AAA", dt.date(2019, 6, 3), dt.date(2026, 3, 31)),
                     ("OLD", dt.date(2019, 6, 3), dt.date(2022, 6, 30)),
                     ("NEW", dt.date(2026, 1, 5), dt.date(2026, 3, 31))):
        for i, d in enumerate(weekdays(a, b)):
            px = 50.0 + (i * 37 % 101) / 7.0 + (0.1 if tk == "AAA" else 0.0)
            adj = None if i % 97 == 5 else px * (0.97 + (i % 13) / 1000.0)
            vol = None if i % 89 == 3 else 1000 + i
            rows.append((tk, d, px * 0.99, px * 1.01, px * 0.98, px if i % 50 else float(int(px)), adj,
                         vol, "yfinance", f"{d}T22:00:00Z"))
    rows.sort(key=lambda r: (r[1][5:], r[0]))            # rowid order != key order
    conn.executemany("INSERT INTO daily_prices VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    spots = [(tk, f"{d}T21:00:0{j}Z", 10.0 + j, 9.5, "USD", "yfinance")
             for tk in ("AAA", "NEW") for j, d in enumerate(("2025-12-30", "2025-12-31", "2026-01-02", "2026-02-03"))]
    conn.executemany("INSERT INTO spot_quotes VALUES (?,?,?,?,?,?)", spots)
    conn.commit()
    conn.close()


def split_copy(single, dest_dir, parts="2021-01-01", before="2026-01-01"):
    os.makedirs(dest_dir, exist_ok=True)
    two = os.path.join(dest_dir, os.path.basename(single))
    shutil.copy(single, two)
    rc = quiet(S.main, ["--db", two, "--parts", parts, "--before", before])
    assert rc == 0, f"split rc {rc}"
    os.remove(two + ".orig")
    return two


def make_pair(tmp, **kw):
    os.makedirs(os.path.join(tmp, "one"), exist_ok=True)
    one = os.path.join(tmp, "one", "prices.db")
    build_single(one)
    return one, split_copy(one, os.path.join(tmp, "two"), **kw)


def cs(db):
    c = P.connect(db, readonly=True)
    try:
        return P.checksum(c)
    finally:
        c.close()


def rows_of(db, sql, args=()):
    c = P.connect(db, readonly=True)
    try:
        return [tuple(r) for r in c.execute(sql, args)]
    finally:
        c.close()


# A synthetic Yahoo (the stand-ins from test_fetch_prices.py, trimmed).
class Row(dict):
    pass


class Stamp:
    def __init__(self, iso):
        self.iso = iso

    def strftime(self, fmt):
        return self.iso


class Bars:
    def __init__(self, rows):
        self.rows = rows
        self.empty = not rows

    def iterrows(self):
        for iso, r in self.rows:
            yield Stamp(iso), r


class Yahoo:
    def __init__(self, days, divs=None, splits=None):
        self.days, self.divs, self.splits = days, divs or {}, splits or {}
        self.raw, px = {}, 100.0
        for i, d in enumerate(days):
            if d in self.splits:
                px /= self.splits[d]
            px *= 1.0 + 0.003 * ((i * 7) % 5 - 2)
            self.raw[d] = px

    def view(self, a, b, asof):
        rows = []
        for d in self.days:
            if d < a or d > b or d > asof:
                continue
            s_mult = f_mult = 1.0
            for e, s in self.splits.items():
                if d < e <= asof:
                    s_mult *= s
            for e, f in self.divs.items():
                if d < e <= asof:
                    f_mult *= f
            close = self.raw[d] / s_mult
            rows.append((d, Row({"Open": close * 0.99, "High": close * 1.01, "Low": close * 0.98,
                                 "Close": close, "Adj Close": close * f_mult, "Volume": 1_000_000 * s_mult,
                                 "Dividends": 0.0,
                                 "Stock Splits": self.splits.get(d, 0.0) if d <= asof else 0.0})))
        return Bars(rows)


def run_loop(db, y, days, start_i, tk="T", window=7):
    """fetch_prices' production loop for each day from days[start_i] on."""
    import fetch_prices as F
    F.utc_now_iso = lambda: "2026-01-01T00:00:00Z"   # updated_at equal on both runs
    conn = P.connect(db)
    statuses = []
    for i in range(start_i, len(days)):
        bars = y.view(days[max(0, i - window + 1)], days[i], days[i])
        rb = F.rebase_history(conn, tk, bars)
        F.upsert_daily(conn, tk, bars)
        conn.commit()
        statuses.append(rb["status"])
    conn.close()
    return statuses


def seed(db, y, days, upto_i, tk="T"):
    """The initial backfill straight into a single file."""
    import fetch_prices as F
    conn = sqlite3.connect(db)
    F.init_db(conn)
    F.upsert_daily(conn, tk, y.view(days[0], days[upto_i], days[upto_i]))
    conn.commit()
    conn.close()


def stored(db, tk="T"):
    return rows_of(db, "SELECT date, open, high, low, close, adj_close, volume FROM daily_prices "
                       "WHERE ticker=? ORDER BY date", (tk,))


def loop_pair(tmp, divs=None, splits=None, start=dt.date(2025, 9, 1), n=140, seed_i=60):
    """Seed a single file, copy + split it, run the same loop on both."""
    days = weekdays(start, start + dt.timedelta(days=int(n * 1.5)))[:n]
    y = Yahoo(days, divs=divs, splits=splits)
    one = os.path.join(tmp, "one", "prices.db")
    os.makedirs(os.path.dirname(one), exist_ok=True)
    seed(one, y, days, seed_i)
    two = split_copy(one, os.path.join(tmp, "two"))
    s1 = run_loop(one, y, days, seed_i + 1)
    s2 = run_loop(two, y, days, seed_i + 1)
    return one, two, days, s1, s2


def ndiff(a, b):
    return sum(1 for ra, rb in zip(a, b) if ra != rb) + abs(len(a) - len(b))


TMP = tempfile.mkdtemp(prefix="pricedb_test_")
try:
    # ======================================================================= 1
    print("\n# 1. single-file and split layouts read identically")
    one, two = make_pair(os.path.join(TMP, "s1"))
    check("the single fixture opens as 'single'", P.layout(one), "single")
    check("the split fixture opens as 'split'", P.layout(two), "split")
    check("split files on disk", [os.path.basename(p) for p in P.files(two)],
          ["prices-archive-1.db", "prices-archive-2.db", "prices-live.db"])
    check_true("full ordered checksum (every column, exact float bits) equal", cs(one) == cs(two),
               f"{cs(two)['daily_prices'][0]} daily rows, {cs(two)['spot_quotes'][0]} spot quotes")
    queries = [
        ("SELECT * FROM daily_prices WHERE ticker=? ORDER BY date", ("AAA",)),
        ("SELECT ticker, COUNT(*), MIN(date), MAX(date), SUM(close), SUM(adj_close) FROM daily_prices "
         "GROUP BY ticker ORDER BY ticker", ()),
        ("SELECT DISTINCT ticker FROM daily_prices ORDER BY ticker", ()),
        ("SELECT d.ticker, d.date, d.close FROM daily_prices d JOIN (SELECT ticker, MAX(date) m FROM "
         "daily_prices GROUP BY ticker) t ON d.ticker=t.ticker AND d.date=t.m ORDER BY 1", ()),
        ("SELECT typeof(open), typeof(close), typeof(adj_close), typeof(volume), COUNT(*) FROM daily_prices "
         "GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4", ()),
        ("SELECT date, adj_close FROM daily_prices WHERE ticker=? AND date<=? ORDER BY date DESC LIMIT 3",
         ("AAA", "2026-01-02")),
        ("SELECT * FROM spot_quotes ORDER BY ticker, captured_at", ()),
        ("SELECT ticker, close, ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY date DESC) FROM daily_prices "
         "ORDER BY ticker, date", ()),
    ]
    check_true(f"{len(queries)} reader queries return identical rows",
               all(rows_of(one, q, a) == rows_of(two, q, a) for q, a in queries))
    c = P.connect(two, readonly=True)
    check("the split connection says so", (c.layout, c.archive_before), ("split", "2026-01-01"))
    check("the view's column names", [d[0] for d in c.execute("SELECT * FROM daily_prices LIMIT 1").description],
          list(P.COLS["daily_prices"]))
    c.close()
    ctl = os.path.join(TMP, "ctl1")
    shutil.copytree(os.path.dirname(two), ctl)
    x = sqlite3.connect(os.path.join(ctl, "prices-archive-2.db"))
    v = x.execute("SELECT adj_close FROM daily_prices WHERE adj_close IS NOT NULL LIMIT 1").fetchone()[0]
    x.execute("UPDATE daily_prices SET adj_close=? WHERE adj_close=?", (math.nextafter(v, 1e9), v))
    x.commit()
    x.close()
    check_true("control: a one-ulp change in one archive row changes the checksum",
               cs(os.path.join(ctl, "prices.db"))["daily_prices"] != cs(one)["daily_prices"])

    # ======================================================================= 2
    print("\n# 2. writes land in the live file")
    arc_sha = {p: sha(p) for p in P.part_paths(two)}
    c = P.connect(two)
    n = P.upsert_daily(c, [("AAA", "2026-04-01", 1.0, 2.0, 0.5, 1.5, 1.4, 10, "yfinance", "t"),
                           ("ZZZ", "2026-04-01", 1.0, 2.0, 0.5, 1.5, 1.4, 10, "yfinance", "t")])
    P.insert_spot(c, ("AAA", "2026-04-01T21:00:00Z", 1.5, 1.4, "USD", "yfinance"))
    c.commit()
    check("rows written", n, 2)
    check("both new rows are in the live file",
          c.execute("SELECT COUNT(*) FROM main.daily_prices WHERE date='2026-04-01'").fetchone()[0], 2)
    check("the spot quote is in the live file",
          c.execute("SELECT COUNT(*) FROM main.spot_quotes WHERE captured_at LIKE '2026-04-01%'").fetchone()[0], 1)
    c.close()
    check_true("archive files byte-identical after new rows and a spot quote",
               all(sha(p) == h for p, h in arc_sha.items()))
    c = P.connect(two)
    q_aaa = "SELECT date, adj_close FROM daily_prices WHERE ticker='AAA' AND date<'2026-03-02' ORDER BY date"
    before_rows = rows_of(one, q_aaa)
    P.rebase(c, "AAA", "2026-03-02", 0.995)           # a dividend reaches every file
    c.commit()
    c.close()
    x = sqlite3.connect(one)
    x.execute("UPDATE daily_prices SET adj_close=adj_close*? WHERE ticker=? AND date<?", (0.995, "AAA", "2026-03-02"))
    x.commit()
    x.close()
    check_true("a re-base updates rows in every part, bit-for-bit the single file's UPDATE",
               rows_of(one, q_aaa) == rows_of(two, q_aaa) != before_rows)
    check_true("...and both archive parts changed on disk", all(sha(p) != h for p, h in arc_sha.items()))
    c = P.connect(two)
    P.upsert_daily(c, [("AAA", "2020-02-03", 9.0, 9.0, 9.0, 9.0, 9.0, 9, "yfinance", "t"),
                       ("AAA", "2023-02-06", 8.0, 8.0, 8.0, 8.0, 8.0, 8, "yfinance", "t")])
    c.commit()
    check("a 2020 row goes to part 1",
          c.execute("SELECT close FROM arc1.daily_prices WHERE ticker='AAA' AND date='2020-02-03'").fetchone(), (9.0,))
    check("a 2023 row goes to part 2",
          c.execute("SELECT close FROM arc2.daily_prices WHERE ticker='AAA' AND date='2023-02-06'").fetchone(), (8.0,))
    c.close()
    check("the set still opens and verifies (new pairing tokens in both files)", P.verify(two), [])
    c = P.connect(two)
    err = raises(lambda: c.execute("INSERT INTO daily_prices (ticker, date) VALUES ('X', '2026-05-01')"))
    check_true("control: a write through the view is refused", err is not None and "view" in err, str(err))
    c.close()
    c = P.connect(two, readonly=True)
    err = raises(lambda: c.execute("DELETE FROM main.daily_prices"))
    check_true("control: a readonly connection refuses a write", err is not None and "readonly" in err, str(err))
    c.close()

    # ======================================================================= 3
    print("\n# 3. the production fetch loop: single file vs split set")
    import fetch_prices as F  # noqa: E402  (yfinance import only; no network)
    # Seed a single file (2025-09 ..), copy + split it, run the same daily loop on both.
    def same(label, tmpname, **kw):
        o, t, days, s1, s2 = loop_pair(os.path.join(TMP, tmpname), **kw)
        so, st = stored(o), stored(t)
        check_true(label + ": identical to the single file, every bit (checksum too)",
                   so == st and cs(o) == cs(t) and s1 == s2,
                   f"{s2.count('rebased')} re-base(s); {ndiff(so, st)} of {len(so)} rows differ")
        return o, t
    monthly = weekdays(dt.date(2026, 1, 12), dt.date(2026, 12, 31))[::21][:12]
    same("one dividend after the boundary", "l1", divs={"2026-01-20": 0.9975})
    same("two successive dividends", "l2", divs={"2026-01-20": 0.9975, "2026-02-10": 0.9971})
    same(f"twelve successive dividends ({monthly[0]} .. {monthly[-1]})", "l3",
         divs={d: 0.9975 - i * 0.0001 for i, d in enumerate(monthly)}, n=330)
    same("a dividend whose overlap bar is before the boundary (d0 in December)", "l4",
         divs={"2025-12-23": 0.99})
    same("a dividend then a 2-for-1 split", "l5", divs={"2026-01-20": 0.9975}, splits={"2026-02-10": 2.0})
    # failing control: a writer that re-bases only the live file (the bug the helper prevents)
    real_rebase = P.rebase

    def live_only(conn, ticker, d0, k_adj, k_split=1.0):
        conn.execute(P._REBASE_ADJ.format(s="main"), (k_adj, ticker, d0))
    P.rebase = live_only
    try:
        o, t, *_ = loop_pair(os.path.join(TMP, "l6"), divs={"2026-01-20": 0.9975})
    finally:
        P.rebase = real_rebase
    check_true("control: re-basing only the live file leaves the split history different",
               stored(o) != stored(t), f"{ndiff(stored(o), stored(t))} rows differ")

    # ======================================================================= 4
    print("\n# 4. roll: a completed year moves live -> archive")
    one, two = make_pair(os.path.join(TMP, "r1"), before="2025-01-01")
    c = P.connect(two)
    P.rebase(c, "AAA", "2025-06-02", 0.99)            # a re-base across both sides first
    c.commit()
    c.close()
    before_cs = cs(two)
    jan = dt.date(2026, 2, 1)
    err = raises(lambda: P.roll(two, before="2027-01-01", today=jan, log=lambda s: None))
    check_true("control: a roll into a year that has not ended is refused", err and "not ended" in err, err)
    err = raises(lambda: P.roll(two, before="2026-02-01", today=jan, log=lambda s: None))
    check_true("control: a boundary that is not 1 January is refused", err and "1 January" in err, err)
    check("roll_due says 2026-01-01", P.roll_due(two, today=jan), "2026-01-01")
    res = P.roll(two, today=jan, log=lambda s: None)
    check("roll status / target", (res["status"], res["to"], res["target"]),
          ("rolled", "2026-01-01", "prices-archive-2.db"))
    check_true("full checksum unchanged across the roll", cs(two) == before_cs)
    c = P.connect(two, readonly=True)
    check("live now starts in 2026",
          c.execute("SELECT MIN(date) FROM main.daily_prices").fetchone()[0][:4], "2026")
    check("boundary advanced", c.archive_before, "2026-01-01")
    c.close()
    check("verify after the roll", P.verify(two), [])
    check("roll_due is now clear", P.roll_due(two, today=jan), None)
    check("a second roll is a no-op", P.roll(two, today=jan, log=lambda s: None)["status"], "noop")
    one, two = make_pair(os.path.join(TMP, "r2"), before="2025-01-01")
    before_cs = cs(two)
    res = P.roll(two, today=jan, log=lambda s: None, part_max=1)   # every part is "past the cap"
    check("past the size cap the year starts a NEW part", (res["target"], res["new_part"]),
          ("prices-archive-3.db", True))
    check_true("...checksum unchanged, verify clean", cs(two) == before_cs and P.verify(two) == [])
    check("...and the set now has three parts", [os.path.basename(p) for p in P.part_paths(two)],
          ["prices-archive-1.db", "prices-archive-2.db", "prices-archive-3.db"])

    # ======================================================================= 5
    print("\n# 5. pairing: the files are one database")
    one, two = make_pair(os.path.join(TMP, "p1"))
    d2 = os.path.dirname(two)
    old_live = os.path.join(TMP, "p1-live-old.db")
    shutil.copy(os.path.join(d2, "prices-live.db"), old_live)
    c = P.connect(two)
    P.upsert_daily(c, [("AAA", "2020-03-02", 1.0, 1.0, 1.0, 1.0, 1.0, 1, "yfinance", "t")])  # archive write
    c.commit()
    c.close()
    check("after an archive write the set opens", P.verify(two), [])
    shutil.copy(old_live, os.path.join(d2, "prices-live.db"))    # the other side's live file
    err = raises(lambda: P.connect(two, readonly=True))
    check_true("control: a live file from another version is refused", err and "not a pair" in err, err)
    one, two = make_pair(os.path.join(TMP, "p2"))
    os.remove(P.part_path(two, 2))
    err = raises(lambda: P.connect(two, readonly=True))
    check_true("control: a missing archive part is refused", err and "differ from the parts" in err, err)
    one, two = make_pair(os.path.join(TMP, "p3"))
    shutil.copy(one, two)                                        # a stray full prices.db beside the set
    err = raises(lambda: P.layout(two))
    check_true("control: both layouts present is refused", err and "both layouts" in err, err)
    os.remove(two)
    open(two, "w").close()                                       # what a stray sqlite3.connect leaves
    with contextlib.redirect_stderr(io.StringIO()):
        check("a 0-byte prices.db beside the set is ignored (split still opens)", P.layout(two), "split")
    one, two = make_pair(os.path.join(TMP, "p4"))
    os.remove(P.live_path(two))
    err = raises(lambda: P.layout(two))
    check_true("control: archive parts without the live file are refused", err and "one side" in err, err)
    check_true("a missing database raises on a readonly open",
               raises(lambda: P.connect(os.path.join(TMP, "nowhere", "prices.db"), readonly=True)) is not None)

    # ======================================================================= 6
    print("\n# 6. --rejoin (the rollback) and idempotence")
    one, two = make_pair(os.path.join(TMP, "j1"))
    c = P.connect(two)
    P.rebase(c, "AAA", "2026-03-02", 0.995)                     # a re-base before the rejoin
    c.commit()
    c.close()
    want = cs(two)
    check("split again on a split directory: a no-op that re-verifies", quiet(S.main, ["--db", two]), 0)
    check("rejoin --dry-run", quiet(S.main, ["--db", two, "--rejoin", "--dry-run"]), 0)
    check("dry run left the split in place", P.layout(two), "split")
    check("rejoin", quiet(S.main, ["--db", two, "--rejoin"]), 0)
    check("one file again", P.layout(two), "single")
    check_true("rejoined checksum equals the split set's", cs(two) == want)
    x = sqlite3.connect(two)
    check("rejoined schema is the original DDL",
          [r[0] for r in x.execute("SELECT name FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name")],
          ["daily_prices", "idx_daily_ticker", "idx_spot_ticker", "spot_quotes"])
    x.close()
    check("rejoin again on one file: a no-op", quiet(S.main, ["--db", two, "--rejoin"]), 0)
    check("manifest is one line", len(open(os.path.join(os.path.dirname(two), S.MANIFEST)).read().splitlines()), 1)
    # control: a split whose verification fails replaces nothing
    one = os.path.join(TMP, "j2", "prices.db")
    os.makedirs(os.path.dirname(one))
    build_single(one)
    real = P.checksum
    P.checksum = lambda conn, tables=P.TABLES: {t: (0, str(id(conn))) for t in tables}   # never equal
    try:
        rc = quiet(S.main, ["--db", one])
    finally:
        P.checksum = real
    check_true("control: a split that fails verification exits 1 and replaces nothing",
               rc == 1 and P.layout(one) == "single" and not os.path.exists(one + ".orig")
               and not os.path.exists(P.live_path(one)))

    # ======================================================================= 7
    print("\n# 7. the Node twin (pricedb.js)")
    node = shutil.which("node")
    if node and subprocess.run([node, "-e", "require('node:sqlite')"], capture_output=True).returncode != 0:
        node = None                                     # Node < 22.5 has no node:sqlite (CI's default)
    one, two = make_pair(os.path.join(TMP, "n1"))
    c = P.connect(two)
    P.rebase(c, "AAA", "2026-03-02", 0.995)
    c.commit()
    c.close()
    if not node:
        print("  SKIP  no node with node:sqlite on PATH")
    else:
        js = ("const p=require(process.argv[1]);const db=p.open(process.argv[2]);"
              "const r=db.prepare('SELECT * FROM daily_prices WHERE ticker=? ORDER BY date').all('AAA');"
              "console.log(JSON.stringify({rows:r.map(o=>Object.values(o)),view:p.viewSql('daily_prices',['arc1','arc2']),"
              "layout:p.layout(process.argv[2])}));db.close();")
        out = subprocess.run([node, "-e", js, os.path.join(HERE, "pricedb.js"), two],
                             capture_output=True, text=True)
        try:
            got = json.loads(out.stdout)
        except ValueError:
            got = None
            print("    node said: " + (out.stderr or out.stdout)[-400:])
        if got:
            want_rows = rows_of(two, "SELECT * FROM daily_prices WHERE ticker=? ORDER BY date", ("AAA",))
            same = len(got["rows"]) == len(want_rows) and all(
                all((a == b) if not isinstance(b, float) else (float(a) == b) for a, b in zip(ra, rb))
                for ra, rb in zip(got["rows"], want_rows))
            check_true("node reads the same rows as python", same,
                       f"{len(got['rows'])} rows")
            check("node's view text is python's", got["view"], P.view_sql("daily_prices", ["arc1", "arc2"]))
            check("node sees the split layout", got["layout"], "split")
        else:
            check_true("node opened the split fixture", False)
        os.remove(P.part_path(two, 2))
        out = subprocess.run([node, "-e", "require(process.argv[1]).open(process.argv[2])",
                              os.path.join(HERE, "pricedb.js"), two], capture_output=True, text=True)
        check_true("control: node refuses a set with a missing part", out.returncode != 0 and "differ" in out.stderr,
                   out.stderr.strip().splitlines()[-1] if out.stderr.strip() else "")

    # ======================================================================= 8
    section_scan(scan_parent=False)
finally:
    shutil.rmtree(TMP, ignore_errors=True)


# -- a blank bar never overwrites a settled close (2026-11-21) -----------------
# An off-hours fetch once returned the last session with no close and blanked 281
# settled closes. The upsert now leaves an existing close alone when the incoming
# bar has none; a blank bar still lands where nothing was there, and a real bar
# still replaces a real bar.
print("\nblank bar guard")
_gd = tempfile.mkdtemp()
_gc = P.connect(os.path.join(_gd, "g.db"))
P.init_db(_gc)
P.upsert_daily(_gc, [("GGG", "2026-05-01", 1.0, 2.0, 0.5, 1.5, 1.5, 10, "yfinance", "t1")])
P.upsert_daily(_gc, [("GGG", "2026-05-01", None, None, None, None, None, None, "yfinance", "t2")])
check("a blank bar leaves the settled close", _gc.execute("select close, updated_at from daily_prices where ticker='GGG'").fetchone(), (1.5, "t1"))
P.upsert_daily(_gc, [("GGG", "2026-05-01", 1.0, 2.0, 0.5, 1.7, 1.7, 11, "yfinance", "t3")])
check("a real bar still replaces a real bar", _gc.execute("select close, updated_at from daily_prices where ticker='GGG'").fetchone(), (1.7, "t3"))
P.upsert_daily(_gc, [("GGG", "2026-05-04", None, None, None, None, None, None, "yfinance", "t4")])
check("a blank bar lands where nothing was there", _gc.execute("select close from daily_prices where ticker='GGG' and date='2026-05-04'").fetchone(), (None,))
P.upsert_daily(_gc, [("GGG", "2026-05-04", 1.0, 2.0, 0.5, 1.9, 1.9, 12, "yfinance", "t5")])
check("and a real bar then fills it", _gc.execute("select close from daily_prices where ticker='GGG' and date='2026-05-04'").fetchone(), (1.9,))
_gc.close()

print("\nFAILED: " + ", ".join(FAILS) if FAILS else "\nall pricedb checks passed")
sys.exit(1 if FAILS else 0)
