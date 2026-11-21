#!/usr/bin/env python3
"""pricedb.py -- the ONE way to open the price database (stdlib sqlite3 only).

The price history used to be one SQLite file, prices.db (78.8 MB on 2026-11-20,
GitHub refuses files over 100 MB). Since the 2026-11 split it is a set of files
beside each other:

    prices-live.db        every row dated ON OR AFTER the boundary (meta
                          `archive_before`, a 1 January), plus pricedb_meta (the
                          boundary, the archive parts, their pairing tokens).
    prices-archive-N.db   the rows dated BEFORE the boundary, in date-range parts
                          (N = 1, 2, ...; each part's range is in the live meta),
                          each kept under GitHub's 50 MB warning.

Every file stores final values, exactly as the single file did. A plain date
split: nothing is derived at read time.

"prices.db" stays the LOGICAL name: every script still takes `--db .../prices.db`
and passes it here, and connect() resolves it:

    <dir>/prices.db exists                       -> single-file layout (the rollback,
                                                    and every test fixture)
    <dir>/prices-live.db + prices-archive-*.db   -> split layout
    both, or only one side of the split          -> PriceDBError (never guess)

On a split connection `daily_prices` and `spot_quotes` are TEMP VIEWS over all
the files (a temp name shadows the real tables of the same name, which keep
their names so each file stays a self-contained, valid database). They read as
the single table did: same names, columns, values and storage classes. A query
without ORDER BY gets the archive parts' rows first, then the live rows -- order
by what you need, as every reader already does.

Writes never go through the views (a write to one raises "cannot modify ...
because it is a view"): writers call upsert_daily / insert_spot / rebase /
set_adj here, which route each row by its date.

RE-BASES. fetch_prices.rebase_history carries every new dividend (and split)
back over a ticker's WHOLE history. rebase() runs the very same UPDATE, same
expression, on every file with `date < d0`; each row is multiplied once, by the
same k, so the result is bit-for-bit the single file's for any number of
re-bases. The cost is accepted: an archive part is re-committed whenever a
re-base reaches it (measured 2026-11-20: about 15-20 commits a month; git stores
each as a binary delta, ~0.1 MB pushed per routine commit).

PAIRING. The files are one database. Every write to an archive part stamps a
fresh token into that part (meta `generation`) and into the live file (meta
`generation:<part file>`) in the same transaction; connect() refuses a set whose
tokens differ (files from different commits -- e.g. a rebase that kept one
side's archive and the other side's live file), or whose parts differ from the
list the live file holds.

CLI (--db defaults to the prices.db beside this file):

    python pricedb.py info                     layout, boundary, parts, rows, sizes
    python pricedb.py verify [--roll-warn]     invariants; exit 1 on a problem (or a due roll)
    python pricedb.py checksum                 full ordered checksum of both tables
    python pricedb.py roll [--before 2027-01-01] [--dry-run]
                                               move completed years live -> archive
    python pricedb.py sql "SELECT ..."         one read-only query, tab-separated (the
                                               one-liner for skills, routines, research kits)
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import pathlib
import re
import shutil
import sqlite3
import sys
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "prices.db")

LAYOUT_VERSION = "1"
# The boundary the migration uses. After the split the ONLY authority is the live
# file's pricedb_meta `archive_before` row, which the roll advances.
INITIAL_ARCHIVE_BEFORE = "2026-01-01"
# Archive part boundaries the migration uses (rows before the first go to part 1).
INITIAL_PART_STARTS = ("2021-01-01",)
# A roll appends the completed year to the newest part unless that part is
# already past this size, in which case the year starts a new part. A year is
# ~13 MB at the 2026 universe, so a part tops out under GitHub's 50 MB warning.
PART_ROLL_MAX_BYTES = 35_000_000
# SQLite's ATTACH limit is 10 in the stock Python and Node builds.
MAX_PARTS = 9

TABLES = ("daily_prices", "spot_quotes")
DATE_COL = {"daily_prices": "date", "spot_quotes": "captured_at"}
KEY_COLS = {"daily_prices": ("ticker", "date"), "spot_quotes": ("ticker", "captured_at")}
COLS = {
    "daily_prices": ("ticker", "date", "open", "high", "low", "close", "adj_close",
                     "volume", "source", "updated_at"),
    "spot_quotes": ("ticker", "captured_at", "price", "previous_close", "currency", "source"),
}

# The single-file schema, verbatim from the pre-split fetch_prices.init_db (the
# CREATE text is what sqlite_master stores; the split files carry it unchanged).
SCHEMA = """
        CREATE TABLE IF NOT EXISTS daily_prices (
            ticker      TEXT NOT NULL,
            date        TEXT NOT NULL,      -- trading date, YYYY-MM-DD
            open        REAL,
            high        REAL,
            low         REAL,
            close       REAL,
            adj_close   REAL,               -- split/dividend-adjusted close
            volume      INTEGER,
            source      TEXT,
            updated_at  TEXT,               -- UTC time this row was last written
            PRIMARY KEY (ticker, date)
        );

        CREATE TABLE IF NOT EXISTS spot_quotes (
            ticker         TEXT NOT NULL,
            captured_at    TEXT NOT NULL,   -- UTC time of capture
            price          REAL,            -- delayed last trade price
            previous_close REAL,
            currency       TEXT,
            source         TEXT,
            PRIMARY KEY (ticker, captured_at)
        );

        CREATE INDEX IF NOT EXISTS idx_daily_ticker ON daily_prices (ticker);
        CREATE INDEX IF NOT EXISTS idx_spot_ticker  ON spot_quotes  (ticker);
        """

META_SCHEMA = """CREATE TABLE IF NOT EXISTS pricedb_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )"""

UPSERT_DAILY = """
            INSERT INTO {s}.daily_prices
                (ticker, date, open, high, low, close, adj_close, volume, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, date) DO UPDATE SET
                open=excluded.open, high=excluded.high, low=excluded.low,
                close=excluded.close, adj_close=excluded.adj_close,
                volume=excluded.volume, source=excluded.source,
                updated_at=excluded.updated_at
            """

INSERT_SPOT = ("INSERT OR REPLACE INTO {s}.spot_quotes "
               "(ticker, captured_at, price, previous_close, currency, source) "
               "VALUES (?, ?, ?, ?, ?, ?)")


class PriceDBError(RuntimeError):
    """The database is missing, half-present, ambiguous, or its files disagree."""


class PriceConnection(sqlite3.Connection):
    """sqlite3.Connection plus where it came from.

    layout          "single" or "split"
    archive_before  the boundary (None for single)
    parts           [(alias, path, from, before), ...] archive parts (split only)
    paths           every file opened
    """
    layout = "single"
    archive_before = None
    parts = ()
    paths = ()
    readonly = False


# --------------------------------------------------------------------------- paths

def _logical(db):
    db = os.fspath(db if db is not None else DEFAULT_DB)
    return os.path.join(db, "prices.db") if os.path.isdir(db) else db


def live_path(db=DEFAULT_DB):
    """prices.db -> prices-live.db (x.db -> x-live.db; tests use that)."""
    stem, ext = os.path.splitext(_logical(db))
    return stem + "-live" + (ext or ".db")


def part_path(db, n):
    stem, ext = os.path.splitext(_logical(db))
    return f"{stem}-archive-{n}{ext or '.db'}"


def part_paths(db=DEFAULT_DB):
    """The archive part files on disk, in part order."""
    stem, ext = os.path.splitext(_logical(db))
    ext = ext or ".db"
    pat = re.compile(re.escape(os.path.basename(stem)) + r"-archive-(\d+)" + re.escape(ext) + "$")
    found = []
    for p in glob.glob(glob.escape(stem) + "-archive-*" + ext):
        m = pat.match(os.path.basename(p))
        if m:
            found.append((int(m.group(1)), p))
    return [p for _, p in sorted(found)]


def files(db=DEFAULT_DB):
    """Every file that makes up the database now (for size checks and git add)."""
    lay = layout(db)
    if lay == "single":
        return [_logical(db)]
    if lay == "split":
        return part_paths(db) + [live_path(db)]
    return []


def layout(db=DEFAULT_DB):
    """'single', 'split' or 'missing'. Raises PriceDBError when it cannot tell."""
    db = _logical(db)
    if db == ":memory:":
        return "single"
    live, parts = live_path(db), part_paths(db)
    has_single = os.path.exists(db) and os.path.getsize(db) > 0
    has_live = os.path.exists(live)
    if has_live != bool(parts):
        raise PriceDBError(
            f"only one side of the split price database is present ({live if has_live else parts[0]}"
            f" without {'any prices-archive-N.db' if has_live else os.path.basename(live)}); "
            "the files are one database -- restore them all from the same commit")
    if has_live and has_single:
        raise PriceDBError(
            f"both layouts are present: {db} AND {os.path.basename(live)} + archive parts. Keep one: "
            "the split set (move the single file aside, e.g. to prices.db.orig) or the single file "
            "(tools/split_prices_db.py --rejoin)")
    if has_live:
        if os.path.exists(db):
            # A 0-byte prices.db is what a stray sqlite3.connect('prices.db') leaves behind.
            print(f"pricedb: ignoring empty {db} (a stray direct open made it; delete it)", file=sys.stderr)
        return "split"
    return "single" if os.path.exists(db) else "missing"


def exists(db=DEFAULT_DB):
    return layout(db) != "missing"


def _uri(path, readonly):
    return pathlib.Path(os.path.abspath(path)).as_uri() + ("?mode=ro" if readonly else "?mode=rw")


# --------------------------------------------------------------------------- open

def connect(db=DEFAULT_DB, readonly=False, timeout=5.0, **kw):
    """Open the price database; `daily_prices` and `spot_quotes` read as one table each.

    readonly=True opens every file mode=ro: no write can happen, and a missing
    database raises instead of being created. Extra keywords go to sqlite3.connect.
    """
    db = _logical(db)
    kw.setdefault("factory", PriceConnection)
    lay = layout(db)
    if lay == "missing":
        if readonly:
            raise PriceDBError(f"no price database at {db} (nor {os.path.basename(live_path(db))} "
                               "+ archive parts beside it)")
        conn = sqlite3.connect(db, timeout=timeout, **kw)      # a writer creating a fresh single file
        conn.layout, conn.paths, conn.readonly = "single", (db,), False
        return conn
    if lay == "single":
        if readonly and db != ":memory:":
            conn = sqlite3.connect(_uri(db, True), uri=True, timeout=timeout, **kw)
        else:
            conn = sqlite3.connect(db, timeout=timeout, **kw)
        conn.layout, conn.paths, conn.readonly = "single", (db,), readonly
        return conn
    return connect_split(live_path(db), part_paths(db), readonly=readonly, timeout=timeout, **kw)


def _parts_meta(meta):
    try:
        return json.loads(meta["parts"])
    except (KeyError, ValueError) as e:
        raise PriceDBError(f"live file has no readable 'parts' meta ({e})") from None


def view_sql(t, aliases):
    """The TEMP VIEW for table t over archive aliases + main (live)."""
    arms = [f"SELECT {', '.join(COLS[t])} FROM {a}.{t}" for a in list(aliases) + ["main"]]
    return f"CREATE TEMP VIEW {t} AS " + " UNION ALL ".join(arms)


def connect_split(live, parts, readonly=False, timeout=5.0, check=True, **kw):
    """Open an explicit split set (live file + archive part files, in part order) --
    what connect() does for the split layout; the migration uses it on temp names."""
    kw.setdefault("factory", PriceConnection)
    for p in [live, *parts]:
        if not os.path.exists(p):
            raise PriceDBError(f"missing {p}")
    if len(parts) > MAX_PARTS:
        raise PriceDBError(f"{len(parts)} archive parts; SQLite attaches at most {MAX_PARTS} beside the live file")
    conn = sqlite3.connect(_uri(live, readonly), uri=True, timeout=timeout, **kw)
    try:
        meta = dict(conn.execute("SELECT key, value FROM main.pricedb_meta"))
        listed = _parts_meta(meta)
        if check:
            if meta.get("role") != "live":
                raise PriceDBError(f"{live}: role meta is {meta.get('role')!r}, want 'live'")
            if meta.get("layout_version") != LAYOUT_VERSION:
                raise PriceDBError(f"{live}: layout_version {meta.get('layout_version')!r}, "
                                   f"this pricedb.py reads {LAYOUT_VERSION!r}")
            want = [p["file"] for p in listed]
            have = [os.path.basename(p) for p in parts]
            if want != have:
                raise PriceDBError(f"archive parts on disk {have} differ from the parts {live} lists {want}: "
                                   "take every prices-*.db file from one commit")
        out = []
        for i, (p, rec) in enumerate(zip(parts, listed), 1):
            alias = f"arc{i}"
            conn.execute(f"ATTACH DATABASE ? AS {alias}", (_uri(p, readonly),))
            if check:
                pm = dict(conn.execute(f"SELECT key, value FROM {alias}.pricedb_meta"))
                if pm.get("role") != "archive":
                    raise PriceDBError(f"{p}: role meta is {pm.get('role')!r}, want 'archive'")
                if pm.get("generation") != meta.get("generation:" + rec["file"]):
                    raise PriceDBError(
                        f"{rec['file']} and {os.path.basename(live)} are not a pair (part generation "
                        f"{pm.get('generation')!r}, live expects {meta.get('generation:' + rec['file'])!r}): "
                        "the files came from different versions. Take EVERY prices-*.db file from one "
                        "commit (git checkout <ref> -- 'prices-*.db'), then re-run whatever wrote the newer one.")
            out.append((alias, p, rec["from"], rec["before"]))
        for t in TABLES:
            conn.execute(view_sql(t, [a for a, *_ in out]))
    except Exception:
        conn.close()
        raise
    conn.layout, conn.readonly = "split", readonly
    conn.archive_before = meta.get("archive_before")
    conn.parts = tuple(out)
    conn.paths = tuple([p for _, p, _, _ in out] + [live])
    return conn


# --------------------------------------------------------------------------- writes

def init_db(conn):
    """Create the tables if missing (single layout; a split set already has them)."""
    if getattr(conn, "layout", "single") == "single":
        conn.executescript(SCHEMA)
        conn.commit()


def _split(conn):
    return getattr(conn, "layout", "single") == "split"


def _new_generation():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]


def touch_part(conn, alias, token=None):
    """Stamp a fresh pairing token into one archive part and the live file (call
    inside the writing transaction)."""
    token = token or _new_generation()
    name = next(os.path.basename(p) for a, p, _, _ in conn.parts if a == alias)
    conn.execute(f"INSERT OR REPLACE INTO {alias}.pricedb_meta (key, value) VALUES ('generation', ?)", (token,))
    conn.execute("INSERT OR REPLACE INTO main.pricedb_meta (key, value) VALUES (?, ?)",
                 ("generation:" + name, token))
    return token


def _schema_for(conn, day):
    """The alias a row dated `day` lives in: an archive part, or 'main' (live)."""
    if not _split(conn) or day >= conn.archive_before:
        return "main"
    for alias, _, lo, hi in conn.parts:
        if (not lo or day >= lo) and day < hi:
            return alias
    raise PriceDBError(f"no archive part holds {day}")


def upsert_daily(conn, rows):
    """Insert-or-update daily bars; each row is (ticker, date, open, high, low, close,
    adj_close, volume, source, updated_at). A row dated before the boundary lands in
    its archive part, every other row in live. Returns rows written. Does not commit."""
    n = 0
    touched = set()
    for row in rows:
        s = _schema_for(conn, row[1])
        if s != "main" and s not in touched:
            touch_part(conn, s)
            touched.add(s)
        conn.execute(UPSERT_DAILY.format(s=s), row)
        n += 1
    return n


def insert_spot(conn, row):
    """INSERT OR REPLACE one spot quote (ticker, captured_at, price, previous_close,
    currency, source), routed by its capture date. Does not commit."""
    s = _schema_for(conn, row[1][:10])
    if s != "main":
        touch_part(conn, s)
    conn.execute(INSERT_SPOT.format(s=s), row)


_REBASE_SPLIT = ("UPDATE {s}.daily_prices SET open=open*?, high=high*?, low=low*?, close=close*?, "
                 "adj_close=adj_close*?, volume=CAST(ROUND(volume/?) AS INTEGER) "
                 "WHERE ticker=? AND date<?")
_REBASE_ADJ = "UPDATE {s}.daily_prices SET adj_close=adj_close*? WHERE ticker=? AND date<?"


def rebase(conn, ticker, d0, k_adj, k_split=1.0):
    """Carry an adjustment change back over every stored row of `ticker` dated before
    d0: adj_close *= k_adj and, on a split, open/high/low/close *= k_split and volume
    /= k_split. fetch_prices.rebase_history's write: the SAME UPDATE, same expression
    and argument order, run on each file in turn (one file on the single layout), so
    every row is multiplied exactly as the single file multiplies it. Does not commit."""
    aliases = [a for a, *_ in conn.parts] if _split(conn) else []
    for s in aliases + ["main"]:
        if k_split != 1.0:
            cur = conn.execute(_REBASE_SPLIT.format(s=s),
                               (k_split, k_split, k_split, k_split, k_adj, k_split, ticker, d0))
        else:
            cur = conn.execute(_REBASE_ADJ.format(s=s), (k_adj, ticker, d0))
        if s != "main" and cur.rowcount:
            touch_part(conn, s)


def set_adj(conn, updates):
    """UPDATE adj_close on explicit rows: updates = [(adj_close, ticker, date), ...]
    (repair_adj.py's write), each routed to the file holding its date. Does not commit."""
    touched = set()
    for adj, ticker, day in updates:
        s = _schema_for(conn, day)
        if s != "main" and s not in touched:
            touch_part(conn, s)
            touched.add(s)
        conn.execute(f"UPDATE {s}.daily_prices SET adj_close=? WHERE ticker=? AND date=?", (adj, ticker, day))


# --------------------------------------------------------------------------- checks

def _cell(v):
    if isinstance(v, float):
        return "r" + v.hex()
    if isinstance(v, int):
        return "i" + str(v)
    if v is None:
        return "n"
    if isinstance(v, bytes):
        return "b" + v.hex()
    return "t" + v


def checksum(conn, tables=TABLES):
    """{table: (rows, sha256)} over EVERY column of EVERY row, ordered by the key.
    Floats hash by their exact bits (float.hex), and the storage class is part of each
    cell, so a last-bit change or an int-for-real swap shows."""
    out = {}
    for t in tables:
        h = hashlib.sha256()
        n = 0
        cur = conn.execute(f"SELECT {', '.join(COLS[t])} FROM {t} ORDER BY {', '.join(KEY_COLS[t])}")
        for row in cur:
            h.update("\x1f".join(_cell(v) for v in row).encode("utf-8"))
            h.update(b"\x1e")
            n += 1
        out[t] = (n, h.hexdigest())
    return out


def per_ticker(conn):
    """{table: {ticker: (rows, min date, max date)}}."""
    out = {}
    for t in TABLES:
        d = DATE_COL[t]
        out[t] = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
            f"SELECT ticker, COUNT(*), MIN({d}), MAX({d}) FROM {t} GROUP BY ticker ORDER BY ticker")}
    return out


def verify(db=DEFAULT_DB):
    """Invariants of the database; a list of problems ([] = fine). connect() itself
    has already checked the pairing tokens and the part list."""
    if layout(db) == "missing":
        return [f"no price database at {_logical(db)}"]
    probs = []
    conn = connect(db, readonly=True)
    try:
        for alias in ["main"] + [a for a, *_ in conn.parts]:
            ok = conn.execute(f"PRAGMA {alias}.quick_check").fetchone()[0]
            if ok != "ok":
                probs.append(f"quick_check {alias}: {ok}")
        if conn.layout == "split":
            b = conn.archive_before
            prev_hi = None
            for alias, path, lo, hi in conn.parts:
                if prev_hi is not None and lo != prev_hi:
                    probs.append(f"{os.path.basename(path)} starts {lo}, the part before ends {prev_hi}")
                prev_hi = hi
                for t in TABLES:
                    d = DATE_COL[t]
                    mn, mx = conn.execute(f"SELECT MIN({d}), MAX({d}) FROM {alias}.{t}").fetchone()
                    if mx is not None and mx[:10] >= hi:
                        probs.append(f"{os.path.basename(path)} {t} holds {mx}, on/after its end {hi}")
                    if mn is not None and lo and mn[:10] < lo:
                        probs.append(f"{os.path.basename(path)} {t} holds {mn}, before its start {lo}")
            if prev_hi != b:
                probs.append(f"the last archive part ends {prev_hi}, the boundary is {b}")
            for t in TABLES:
                d = DATE_COL[t]
                lmin = conn.execute(f"SELECT MIN({d}) FROM main.{t}").fetchone()[0]
                if lmin is not None and lmin[:10] < b:
                    probs.append(f"live {t} holds {lmin}, before the boundary {b}")
    finally:
        conn.close()
    return probs


def roll_due(db=DEFAULT_DB, today=None):
    """The boundary the next roll moves to, or None: a roll is due once the live file
    holds a year that has ended."""
    if layout(db) != "split":
        return None
    today = today or dt.datetime.now(dt.timezone.utc).date()
    target = f"{today.year:04d}-01-01"
    conn = connect(db, readonly=True)
    try:
        return target if conn.archive_before < target else None
    finally:
        conn.close()


def _new_part_file(path, ddl_conn, name):
    """An empty archive part with the live file's price tables and indexes."""
    ddl = [r[0] for r in ddl_conn.execute(
        "SELECT sql FROM main.sqlite_master WHERE tbl_name IN ('daily_prices','spot_quotes') "
        "AND sql IS NOT NULL ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, name")]
    c = sqlite3.connect(path)
    for s in ddl + [META_SCHEMA]:
        c.execute(s)
    c.executemany("INSERT INTO pricedb_meta (key, value) VALUES (?, ?)",
                  [("role", "archive"), ("layout_version", LAYOUT_VERSION), ("part", name)])
    c.commit()
    c.close()


def roll(db=DEFAULT_DB, before=None, dry_run=False, today=None, log=print, part_max=None):
    """Move every row dated before `before` (a 1 January no later than this year's)
    from live into the newest archive part -- or a new part when the newest is past
    PART_ROLL_MAX_BYTES -- advance the boundary, VACUUM, and
    prove the full ordered checksum unchanged. Any failure restores every file."""
    db = _logical(db)
    if layout(db) != "split":
        raise PriceDBError("roll needs the split layout")
    today = today or dt.datetime.now(dt.timezone.utc).date()
    part_max = PART_ROLL_MAX_BYTES if part_max is None else part_max
    before = before or f"{today.year:04d}-01-01"
    if not re.fullmatch(r"\d{4}-01-01", before):
        raise PriceDBError(f"--before must be a 1 January (YYYY-01-01), got {before!r}")
    if before > f"{today.year:04d}-01-01":
        raise PriceDBError(f"{before} would archive a year that has not ended (today {today})")
    conn = connect(db)
    old = conn.archive_before
    if before <= old:
        conn.close()
        log(f"roll: nothing to do (boundary already {old})")
        return {"status": "noop", "archive_before": old}
    moving = {t: conn.execute(f"SELECT COUNT(*) FROM main.{t} WHERE {DATE_COL[t]} < ?", (before,)).fetchone()[0]
              for t in TABLES}
    last_alias, last_path, _, _ = conn.parts[-1]
    new_part = os.path.getsize(last_path) > part_max
    target_path = part_path(db, len(conn.parts) + 1) if new_part else last_path
    log(f"roll: boundary {old} -> {before}; {moving['daily_prices']} daily rows and {moving['spot_quotes']} "
        f"spot quotes into {os.path.basename(target_path)}{' (NEW part)' if new_part else ''}")
    if dry_run:
        conn.close()
        return {"status": "dry-run", "from": old, "to": before, "moving": moving,
                "target": os.path.basename(target_path), "new_part": new_part}
    if new_part and len(conn.parts) >= MAX_PARTS:
        conn.close()
        raise PriceDBError(f"a new part would make {len(conn.parts) + 1}; the limit is {MAX_PARTS}")
    before_sum = checksum(conn)
    conn.close()
    olds = files(db)
    bak = [(p, p + ".preroll") for p in olds]
    for p, b in bak:
        shutil.copy2(p, b)
    try:
        conn = connect(db)
        if new_part:
            name = os.path.basename(target_path)
            _new_part_file(target_path, conn, name)
            alias = f"arc{len(conn.parts) + 1}"
            conn.execute(f"ATTACH DATABASE ? AS {alias}", (_uri(target_path, False),))
            conn.parts = conn.parts + ((alias, target_path, old, before),)
        else:
            alias = last_alias
        conn.execute("BEGIN")
        for t in TABLES:
            cols = ", ".join(COLS[t])
            conn.execute(f"INSERT INTO {alias}.{t} ({cols}) SELECT {cols} FROM main.{t} "
                         f"WHERE {DATE_COL[t]} < ? ORDER BY rowid", (before,))
            conn.execute(f"DELETE FROM main.{t} WHERE {DATE_COL[t]} < ?", (before,))
        parts = [{"file": os.path.basename(p), "from": lo, "before": hi} for _, p, lo, hi in conn.parts]
        parts[-1]["before"] = before
        conn.execute("UPDATE main.pricedb_meta SET value=? WHERE key='parts'", (json.dumps(parts),))
        conn.execute("UPDATE main.pricedb_meta SET value=? WHERE key='archive_before'", (before,))
        touch_part(conn, alias)
        conn.commit()
        conn.close()
        for p in files(db):
            c = sqlite3.connect(p)
            c.execute("VACUUM")
            c.close()
        conn = connect(db, readonly=True)
        after_sum = checksum(conn)
        conn.close()
        if after_sum != before_sum:
            raise PriceDBError(f"checksum changed across the roll: {before_sum} -> {after_sum}")
        probs = verify(db)
        if probs:
            raise PriceDBError("verify after roll: " + "; ".join(probs))
    except Exception:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        for p, b in bak:
            shutil.copy2(b, p)
            os.remove(b)
        if new_part and os.path.exists(target_path):
            os.remove(target_path)
        log("roll: FAILED -- every file restored from its pre-roll copy")
        raise
    for p, b in bak:
        os.remove(b)
    log(f"roll: done, boundary {before}; full checksum unchanged ({after_sum['daily_prices'][0]} daily rows, "
        f"{after_sum['spot_quotes'][0]} spot quotes)")
    return {"status": "rolled", "from": old, "to": before, "moving": moving,
            "target": os.path.basename(target_path), "new_part": new_part, "checksum": after_sum}


def info(db=DEFAULT_DB):
    lay = layout(db)
    out = {"layout": lay, "db": _logical(db)}
    if lay == "missing":
        return out
    conn = connect(db, readonly=True)
    try:
        out["archive_before"] = conn.archive_before
        out["files"] = {os.path.basename(p): os.path.getsize(p) for p in conn.paths}
        if lay == "split":
            out["parts"] = [(os.path.basename(p), lo or "(start)", hi) for _, p, lo, hi in conn.parts]
            out["roll_due"] = roll_due(db)
        for t in TABLES:
            out[t] = conn.execute(f"SELECT COUNT(*), MIN({DATE_COL[t]}), MAX({DATE_COL[t]}) FROM {t}").fetchone()
    finally:
        conn.close()
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Open, check and roll the price database.")
    ap.add_argument("--db", default=DEFAULT_DB, help="logical database path (default: prices.db here)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info")
    v = sub.add_parser("verify")
    v.add_argument("--roll-warn", action="store_true", help="also exit 1 when a roll is due")
    sub.add_parser("checksum")
    r = sub.add_parser("roll")
    r.add_argument("--before", help="new boundary, a 1 January (default: 1 January this year)")
    r.add_argument("--dry-run", action="store_true")
    q = sub.add_parser("sql")
    q.add_argument("query")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "info":
            for k, val in info(a.db).items():
                print(f"{k}: {val}")
            return 0
        if a.cmd == "verify":
            probs = verify(a.db)
            due = roll_due(a.db) if a.roll_warn else None
            for p in probs:
                print("PROBLEM " + p)
            if due:
                print(f"ROLL DUE - the live file holds a completed year: python pricedb.py roll --before {due}")
            if not probs:
                inf = info(a.db)
                print(f"OK - {inf['layout']} layout, boundary {inf.get('archive_before')}, "
                      f"{inf['daily_prices'][0]} daily rows, {inf['spot_quotes'][0]} spot quotes; "
                      + ", ".join(f"{k} {val / 1e6:.1f} MB" for k, val in inf["files"].items()))
            return 1 if (probs or due) else 0
        if a.cmd == "checksum":
            conn = connect(a.db, readonly=True)
            for t, (n, h) in checksum(conn).items():
                print(f"{t} {n} {h}")
            conn.close()
            return 0
        if a.cmd == "roll":
            roll(a.db, before=a.before, dry_run=a.dry_run)
            return 0
        if a.cmd == "sql":
            conn = connect(a.db, readonly=True)
            cur = conn.execute(a.query)
            if cur.description:
                print("\t".join(d[0] for d in cur.description))
                for row in cur:
                    print("\t".join("" if val is None else str(val) for val in row))
            conn.close()
            return 0
    except PriceDBError as e:
        print(f"pricedb: {e}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
