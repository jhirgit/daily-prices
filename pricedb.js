// pricedb.js -- the Node twin of pricedb.py's READ side (node:sqlite, no npm deps).
//
// The price database is one prices.db, or since the 2026-11 split prices-live.db +
// prices-archive-N.db beside it (see pricedb.py for the why). open() takes the
// LOGICAL prices.db path and returns a read-only node:sqlite DatabaseSync on which
// `daily_prices` and `spot_quotes` read as one table each, exactly as pricedb.py's
// connect(readonly=True) does: the same TEMP VIEW text (viewSql), the same
// pairing-token and part-list checks. There is no write side here; every writer
// is Python and goes through pricedb.py.
//
//   const pricedb = require("<daily-prices>/pricedb.js");
//   const db = pricedb.open(path.join(dp, "prices.db"));   // throws if missing / unpaired
//   db.prepare("SELECT close FROM daily_prices WHERE ticker=? ORDER BY date DESC LIMIT 1").get("SPY");
//   db.close();
"use strict";
const fs = require("fs");
const path = require("path");

const LAYOUT_VERSION = "1";
const COLS = {
  daily_prices: ["ticker", "date", "open", "high", "low", "close", "adj_close", "volume", "source", "updated_at"],
  spot_quotes: ["ticker", "captured_at", "price", "previous_close", "currency", "source"],
};

function logical(db) {
  try { if (fs.statSync(db).isDirectory()) return path.join(db, "prices.db"); } catch (e) { /* not a dir */ }
  return db;
}
function livePath(db) {
  const p = path.parse(logical(db));
  return path.join(p.dir, p.name + "-live" + (p.ext || ".db"));
}
function partPaths(db) {
  const p = path.parse(logical(db));
  const ext = p.ext || ".db";
  const re = new RegExp("^" + p.name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "-archive-(\\d+)" +
                        ext.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "$");
  let names = [];
  try { names = fs.readdirSync(p.dir || "."); } catch (e) { return []; }
  return names.map((n) => [re.exec(n), n]).filter(([m]) => m)
    .sort((a, b) => Number(a[0][1]) - Number(b[0][1]))
    .map(([, n]) => path.join(p.dir, n));
}

// "single" | "split" | "missing"; throws when it cannot tell (pricedb.py's rules).
function layout(db) {
  db = logical(db);
  const live = livePath(db), parts = partPaths(db);
  const hasSingle = fs.existsSync(db) && fs.statSync(db).size > 0;
  const hasLive = fs.existsSync(live);
  if (hasLive !== (parts.length > 0)) {
    throw new Error("pricedb: only one side of the split price database is present beside " + db +
                    "; the files are one database -- restore them all from the same commit");
  }
  if (hasLive && hasSingle) {
    throw new Error("pricedb: both layouts are present beside " + db + " (prices.db AND prices-live.db); keep one");
  }
  if (hasLive) return "split";
  return fs.existsSync(db) ? "single" : "missing";
}
function exists(db) { return layout(db) !== "missing"; }

function viewSql(t, aliases) {
  const arms = aliases.concat(["main"]).map((a) => `SELECT ${COLS[t].join(", ")} FROM ${a}.${t}`);
  return `CREATE TEMP VIEW ${t} AS ` + arms.join(" UNION ALL ");
}

// A read-only DatabaseSync over the logical prices.db, either layout.
function open(db) {
  process.removeAllListeners("warning");               // node:sqlite's ExperimentalWarning is noise
  const { DatabaseSync } = require("node:sqlite");
  db = logical(db);
  const lay = layout(db);
  if (lay === "missing") throw new Error("pricedb: no price database at " + db + " (nor prices-live.db beside it)");
  if (lay === "single") return new DatabaseSync(db, { readOnly: true });
  const live = livePath(db), parts = partPaths(db);
  const conn = new DatabaseSync(live, { readOnly: true });
  try {
    const meta = Object.fromEntries(conn.prepare("SELECT key, value FROM main.pricedb_meta").all().map((r) => [r.key, r.value]));
    if (meta.role !== "live" || meta.layout_version !== LAYOUT_VERSION) {
      throw new Error("pricedb: " + live + " is not a layout-" + LAYOUT_VERSION + " live file");
    }
    const listed = JSON.parse(meta.parts).map((p) => p.file);
    const have = parts.map((p) => path.basename(p));
    if (JSON.stringify(listed) !== JSON.stringify(have)) {
      throw new Error("pricedb: archive parts on disk " + JSON.stringify(have) + " differ from the parts " + live +
                      " lists " + JSON.stringify(listed) + ": take every prices-*.db file from one commit");
    }
    const aliases = [];
    parts.forEach((p, i) => {
      const a = "arc" + (i + 1);
      conn.prepare(`ATTACH DATABASE ? AS ${a}`).run(p);   // read-only like the main file
      const pm = Object.fromEntries(conn.prepare(`SELECT key, value FROM ${a}.pricedb_meta`).all().map((r) => [r.key, r.value]));
      if (pm.role !== "archive" || pm.generation !== meta["generation:" + path.basename(p)]) {
        throw new Error("pricedb: " + path.basename(p) + " and " + path.basename(live) + " are not a pair (" +
                        pm.generation + " vs " + meta["generation:" + path.basename(p)] +
                        "): take every prices-*.db file from one commit");
      }
      aliases.push(a);
    });
    Object.keys(COLS).forEach((t) => conn.exec(viewSql(t, aliases)));
  } catch (e) {
    conn.close();
    throw e;
  }
  return conn;
}

// Every file that makes up the database now (sizes, git add).
function files(db) {
  const lay = layout(db);
  if (lay === "single") return [logical(db)];
  if (lay === "split") return partPaths(db).concat([livePath(db)]);
  return [];
}

module.exports = { open, layout, exists, files, viewSql, livePath, partPaths, LAYOUT_VERSION };
