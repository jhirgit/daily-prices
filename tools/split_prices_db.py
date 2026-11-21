#!/usr/bin/env python3
"""split_prices_db.py -- split prices.db into archive parts + a live file, or rejoin them.

    python tools/split_prices_db.py --dry-run        build + verify into temp files, replace nothing
    python tools/split_prices_db.py                  build, verify, move prices.db aside to
                                                     prices.db.orig, put the split set in place
    python tools/split_prices_db.py --rejoin --dry-run
    python tools/split_prices_db.py --rejoin         the ROLLBACK: one prices.db rebuilt from the
                                                     split set, verified, the
                                                     split files moved aside to *.split-orig

The layout (see pricedb.py): prices-archive-1.db holds rows dated before the first
part start (default 2021-01-01), prices-archive-2.db from there to the boundary
(default 2026-01-01), prices-live.db the boundary onward plus the meta
table. Spot quotes split on the date of captured_at.

Every build is verified BEFORE anything is replaced, against the source, both
opened through pricedb.connect:
  * per ticker and table: row count, min date, max date;
  * the full ordered checksum (pricedb.checksum: every column of every row, floats
    by their exact bits, storage class included) of daily_prices and spot_quotes;
  * PRAGMA integrity_check on every file written; the CREATE text of the tables and
    indexes and the pragmas (page_size, encoding, auto_vacuum, journal_mode) equal.
Idempotent: on an already-split (or already-joined) directory it re-verifies what
is there and exits 0 without writing. Each completed run writes a one-line
manifest, prices-split-manifest.txt, beside the database.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import pricedb  # noqa: E402

MANIFEST = "prices-split-manifest.txt"
PRAGMAS = ("page_size", "encoding", "auto_vacuum", "journal_mode")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def schema_rows(conn, schema="main"):
    """The two price tables' CREATE statements and their named indexes, as stored."""
    return [r[0] for r in conn.execute(
        f"SELECT sql FROM {schema}.sqlite_master "
        "WHERE tbl_name IN ('daily_prices', 'spot_quotes') AND sql IS NOT NULL "
        "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, name")]


def pragmas(path):
    c = sqlite3.connect(pricedb._uri(path, True), uri=True)
    try:
        return {p: c.execute(f"PRAGMA {p}").fetchone()[0] for p in PRAGMAS}
    finally:
        c.close()


def new_file(path, src_pragmas, ddl):
    if os.path.exists(path):
        os.remove(path)
    # uri=True so the build can ATTACH its source read-only by URI.
    c = sqlite3.connect(pricedb._uri(path, False).replace("?mode=rw", "?mode=rwc"), uri=True)
    c.execute(f"PRAGMA page_size={int(src_pragmas['page_size'])}")
    c.execute(f"PRAGMA auto_vacuum={int(src_pragmas['auto_vacuum'])}")
    c.execute(f"PRAGMA journal_mode={src_pragmas['journal_mode']}")
    for sql in ddl:
        c.execute(sql)
    c.commit()
    return c


def finish(c):
    c.commit()
    c.execute("VACUUM")
    ok = c.execute("PRAGMA integrity_check").fetchone()[0]
    c.close()
    return ok


def compare(src, dst, label, log):
    """Per-ticker counts / min / max and the full checksum; src and dst are connections."""
    probs = []
    pt_s, pt_d = pricedb.per_ticker(src), pricedb.per_ticker(dst)
    for t in pricedb.TABLES:
        if pt_s[t] != pt_d[t]:
            diff = sorted(k for k in set(pt_s[t]) | set(pt_d[t]) if pt_s[t].get(k) != pt_d[t].get(k))
            probs.append(f"{label}: {t} per-ticker count/min/max differ for {len(diff)} ticker(s): {diff[:10]}")
        else:
            log(f"  {t:13s} per-ticker count/min/max equal for {len(pt_s[t])} tickers")
    cs_s, cs_d = pricedb.checksum(src), pricedb.checksum(dst)
    for t in pricedb.TABLES:
        log(f"  {t:13s} source {cs_s[t][0]:>7} rows sha256 {cs_s[t][1]}")
        log(f"  {'':13s} {label:6s} {cs_d[t][0]:>7} rows sha256 {cs_d[t][1]}  "
            f"{'EQUAL' if cs_s[t] == cs_d[t] else 'DIFFERENT'}")
        if cs_s[t] != cs_d[t]:
            probs.append(f"{label}: {t} checksum differs")
    return probs, cs_s


def write_manifest(dbdir, line, log):
    p = os.path.join(dbdir, MANIFEST)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")
    log(f"manifest {p}:")
    log("  " + line)


def files_line(paths):
    return " ".join(f"{os.path.basename(p)}(sha256={sha256(p)[:16]},bytes={os.path.getsize(p)})" for p in paths)


def do_split(db, before, starts, dry_run, log):
    db = pricedb._logical(db)
    dbdir = os.path.dirname(os.path.abspath(db))
    lay = pricedb.layout(db)
    if lay == "split":
        log("already split; verifying what is there")
        probs = pricedb.verify(db)
        for p in probs:
            log("PROBLEM " + p)
        if not probs:
            log("OK - " + ", ".join(os.path.basename(p) for p in pricedb.files(db)))
        return 1 if probs else 0
    if lay != "single":
        log(f"no prices database at {db}")
        return 1
    bounds = list(starts) + [before]
    if any(len(b) != 10 or not b.endswith("-01-01") for b in bounds) or bounds != sorted(set(bounds)):
        log(f"part starts and boundary must be ascending 1 Januaries, got {bounds}")
        return 1
    live = pricedb.live_path(db)
    parts = [pricedb.part_path(db, i + 1) for i in range(len(bounds))]
    ranges = [("" if i == 0 else bounds[i - 1], bounds[i]) for i in range(len(bounds))]
    tmp = {p: p + ".tmp" for p in parts + [live]}
    src_pr = pragmas(db)
    src_sha = sha256(db)
    gen = "split-" + src_sha[:16]
    parts_meta = [{"file": os.path.basename(p), "from": lo, "before": hi} for p, (lo, hi) in zip(parts, ranges)]
    src = pricedb.connect(db, readonly=True)
    probs, cs = [], None
    try:
        ddl = schema_rows(src)
        log(f"source {db}: {os.path.getsize(db)} bytes, sha256 {src_sha[:16]}, pragmas {src_pr}")
        jobs = [(p, "archive", lo, hi) for p, (lo, hi) in zip(parts, ranges)] + [(live, "live", before, None)]
        for path, role, lo, hi in jobs:
            c = new_file(tmp[path], src_pr, ddl + [pricedb.META_SCHEMA])
            c.execute("ATTACH DATABASE ? AS src", (pricedb._uri(db, True),))
            for t in pricedb.TABLES:
                cols = ", ".join(pricedb.COLS[t])
                d = f"substr({pricedb.DATE_COL[t]}, 1, 10)"
                where = " AND ".join(x for x in (f"{d} >= ?" if lo else "", f"{d} < ?" if hi else "") if x)
                args = tuple(x for x in (lo, hi) if x)
                c.execute(f"INSERT INTO main.{t} ({cols}) SELECT {cols} FROM src.{t} WHERE {where} "
                          "ORDER BY rowid", args)
            if role == "live":
                meta = {"role": "live", "layout_version": pricedb.LAYOUT_VERSION, "archive_before": before,
                        "parts": json.dumps(parts_meta)}
                meta.update({"generation:" + os.path.basename(p): gen for p in parts})
            else:
                meta = {"role": "archive", "layout_version": pricedb.LAYOUT_VERSION,
                        "part": os.path.basename(path), "generation": gen}
            c.executemany("INSERT INTO main.pricedb_meta (key, value) VALUES (?, ?)", sorted(meta.items()))
            c.commit()
            c.execute("DETACH DATABASE src")
            ok = finish(c)
            log(f"built {os.path.basename(tmp[path])}: {os.path.getsize(tmp[path])} bytes "
                f"[{lo or 'start'} .. {hi or 'end'}), integrity {ok}")
            if ok != "ok":
                probs.append(f"integrity_check failed on {tmp[path]}: {ok}")
        for path in tmp.values():
            got = pragmas(path)
            if got != src_pr:
                probs.append(f"pragmas differ on {path}: {got} vs {src_pr}")
        if not probs:
            dst = pricedb.connect_split(tmp[live], [tmp[p] for p in parts], readonly=True, check=False)
            try:
                for alias in ["main"] + [a for a, *_ in dst.parts]:
                    if schema_rows(dst, alias) != ddl:
                        probs.append(f"schema differs in {alias}")
                log("verify split vs source (both through pricedb):")
                p2, cs = compare(src, dst, "split", log)
                probs += p2
                counts = {os.path.basename(path).replace(".tmp", ""): dst.execute(
                    f"SELECT COUNT(*) FROM {alias}.daily_prices").fetchone()[0]
                    for alias, path in [(a, p) for a, p, _, _ in dst.parts] + [("main", tmp[live])]}
            finally:
                dst.close()
    finally:
        src.close()
    if probs:
        for p in probs:
            log("PROBLEM " + p)
        for p in tmp.values():
            if os.path.exists(p):
                os.remove(p)
        log("nothing replaced")
        return 1
    log("daily rows per file: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    if dry_run:
        for p in tmp.values():
            os.remove(p)
        log("DRY RUN ok: temp files removed, nothing replaced")
        return 0
    orig = db + ".orig"
    if os.path.exists(orig):
        os.remove(orig)
    os.replace(db, orig)
    for final, t in tmp.items():
        os.replace(t, final)
    probs = pricedb.verify(db)
    if probs:
        for final in tmp:
            os.remove(final)
        os.replace(orig, db)
        for p in probs:
            log("PROBLEM " + p)
        log("verify after replace failed: prices.db put back, split files removed")
        return 1
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_manifest(dbdir, f"{stamp} split boundary={before} parts={','.join(starts)} "
                          f"source=prices.db(sha256={src_sha[:16]},bytes={os.path.getsize(orig)}) -> "
                          f"{files_line(parts + [live])} "
                          f"daily_prices={cs['daily_prices'][0]}:{cs['daily_prices'][1][:16]} "
                          f"spot_quotes={cs['spot_quotes'][0]}:{cs['spot_quotes'][1][:16]} generation={gen}", log)
    log(f"done: prices.db moved aside to {os.path.basename(orig)} (untracked; delete it once shipped)")
    return 0


def do_rejoin(db, dry_run, log):
    db = pricedb._logical(db)
    dbdir = os.path.dirname(os.path.abspath(db))
    lay = pricedb.layout(db)
    if lay == "single":
        log(f"already one file ({db}); verifying it")
        probs = pricedb.verify(db)
        for p in probs:
            log("PROBLEM " + p)
        return 1 if probs else 0
    if lay != "split":
        log(f"no prices database at {db}")
        return 1
    tmp = db + ".tmp"
    split_files = pricedb.files(db)
    src = pricedb.connect(db, readonly=True)
    src_pr = pragmas(pricedb.live_path(db))
    ddl = schema_rows(src, "main")
    probs, cs = [], None
    try:
        c = new_file(tmp, src_pr, ddl)
        # Each part's rows in their stored order, then the live rows: values as stored.
        for i, path in enumerate([p for _, p, _, _ in src.parts] + [pricedb.live_path(db)]):
            c.execute(f"ATTACH DATABASE ? AS p{i}", (pricedb._uri(path, True),))
            for t in pricedb.TABLES:
                cols = ", ".join(pricedb.COLS[t])
                c.execute(f"INSERT INTO main.{t} ({cols}) SELECT {cols} FROM p{i}.{t} ORDER BY rowid")
            c.commit()
            c.execute(f"DETACH DATABASE p{i}")
        ok = finish(c)
        log(f"built {os.path.basename(tmp)}: {os.path.getsize(tmp)} bytes, integrity {ok}")
        if ok != "ok":
            probs.append(f"integrity_check failed on {tmp}: {ok}")
        else:
            dst = sqlite3.connect(pricedb._uri(tmp, True), uri=True)
            try:
                if schema_rows(dst) != ddl:
                    probs.append("schema differs in the rejoined file")
                log("verify rejoined vs split (through pricedb):")
                p2, cs = compare(src, dst, "joined", log)
                probs += p2
            finally:
                dst.close()
    finally:
        src.close()
    if probs:
        for p in probs:
            log("PROBLEM " + p)
        os.remove(tmp)
        log("nothing replaced")
        return 1
    if dry_run:
        os.remove(tmp)
        log("DRY RUN ok: temp file removed, nothing replaced")
        return 0
    srcline = files_line(split_files)
    for p in split_files:
        aside = p + ".split-orig"
        if os.path.exists(aside):
            os.remove(aside)
        os.replace(p, aside)
    os.replace(tmp, db)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_manifest(dbdir, f"{stamp} rejoin {srcline} -> {files_line([db])} "
                          f"daily_prices={cs['daily_prices'][0]}:{cs['daily_prices'][1][:16]} "
                          f"spot_quotes={cs['spot_quotes'][0]}:{cs['spot_quotes'][1][:16]}", log)
    log("done: the split files moved aside to *.split-orig (delete once the rollback is shipped)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=pricedb.DEFAULT_DB, help="logical database path (default: prices.db)")
    ap.add_argument("--before", default=pricedb.INITIAL_ARCHIVE_BEFORE,
                    help="boundary: rows dated before it go to the archive (default %(default)s)")
    ap.add_argument("--parts", default=",".join(pricedb.INITIAL_PART_STARTS),
                    help="comma-separated archive part starts (default %(default)s)")
    ap.add_argument("--rejoin", action="store_true", help="rebuild one prices.db from the split set (rollback)")
    ap.add_argument("--dry-run", action="store_true", help="build and verify into temp files only")
    a = ap.parse_args(argv)
    log = lambda s: print(s, flush=True)  # noqa: E731
    try:
        if a.rejoin:
            return do_rejoin(a.db, a.dry_run, log)
        starts = [s for s in a.parts.split(",") if s.strip()]
        return do_split(a.db, a.before, starts, a.dry_run, log)
    except pricedb.PriceDBError as e:
        log(f"split_prices_db: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
