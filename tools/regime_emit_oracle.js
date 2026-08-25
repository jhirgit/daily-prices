// regime_emit_oracle.js — end-to-end CONFIG parity oracle.
//
//   node tools/regime_emit_oracle.js parity/panel.json parity/oracle_regime.json
//
// Reads a prebuilt panel {dates, series, emitted[], book_universe[]} and runs
// the EXACT client-side assembly (index.html renderRegimePanel + renderRotation
// + the rank-receipt block) on it via regime_core.js, emitting a `regime` object
// in the SAME shape as regime.py's build_regime(round_floats=False). The Python
// driver then diffs its own build_regime output against this, catching any
// error in the leg order / pair selection / pool filters / ladder config that
// the per-function fixture parity cannot see. The leg calls, REG_ETFS and the
// pool filters below are copied verbatim from index.html v52.

const fs = require("fs");
const RC = require("./regime_core.js");

const [, , panelPath, outPath] = process.argv;
const P = JSON.parse(fs.readFileSync(panelPath, "utf8"));
const series = P.series, dates = P.dates;

// REG_ETFS is passed in via panel.json (sourced from regime.py's REG_ETFS) so it
// can never drift from the port; the leg calls below stay verbatim from index.html.
const REG_ETFS = P.reg_etfs;
function firstPresent(list) { for (let i = 0; i < list.length; i++) if (series[list[i]]) return list[i]; return null; }

const etfSet = {}; REG_ETFS.forEach((e) => { etfSet[e.t] = 1; });
const book = P.book_universe.filter((t) => series[t] && !etfSet[t] && !/(-USD|=F)$/.test(t) && t.charAt(0) !== "^");

const legs = [], info = [];
const legKeys = [];  // keys assigned in the same order regime.py uses
function addRatio(key, name, numList, denList, macro) {
  const nm = firstPresent(numList), dn = firstPresent(denList); if (!nm || !dn) return;
  const cont = RC.ratio(series[nm], series[dn]);
  const s = RC.ratioLegSeries(cont);
  legs.push(s); legKeys.push(key);
  info.push({ key, label: name, type: "ratio", val: nm + "÷" + dn, macro: !!macro, series: s, last: s[s.length - 1], cont });
}
function addTrend(key, name, tickList, invert, macro) {
  const t = firstPresent(tickList); if (!t) return;
  const s = RC.trendLegSeries(series[t], invert);
  legs.push(s); legKeys.push(key);
  info.push({ key, label: name, type: "trend", val: (invert ? "↓" : "↑") + t, macro: !!macro, series: s, last: s[s.length - 1], cont: series[t] });
}
addRatio("credit_hy_ig", "Credit — HY vs IG", ["HYG"], ["LQD"], true);
addRatio("copper_gold", "Copper / gold", ["CPER"], ["GLD"], true);
addTrend("dollar_inv", "Dollar (inverse)", ["UUP"], true, true);
addRatio("offense_defense", "Offense vs defense", ["SMH", "SOXX"], ["GDX", "GDXJ", "RING"], false);
addRatio("beta_appetite", "Beta appetite", ["QQQ", "IWM", "SOXX"], ["SPY"], false);

let frac = book.length ? RC.breadthSeries(series, book) : null;
let breadthLeg = null;
if (frac) {
  breadthLeg = RC.breadthLegSeries(frac);
  legs.push(breadthLeg); legKeys.push("breadth_book_200d");
  info.push({ key: "breadth_book_200d", label: "Breadth (book >200d)", type: "breadth", val: null, macro: false, series: breadthLeg, last: breadthLeg[breadthLeg.length - 1], cont: frac });
}

const comp = RC.compositeSeries(legs);
const active = legs.length;
const macroCount = info.filter((x) => x.macro).length;
const last = comp.sum.length - 1;
const netLast = active ? comp.sum[last] / active : null;

// receipts pool = emitted (>=MIN_BARS) tickers minus sector ETFs (client's TECH.tickers filter)
const recPool = P.emitted.filter((t) => series[t] && !etfSet[t]);
const rr = RC.rankReceipts(series, recPool);
const receipts = {};
Object.keys(rr).forEach((t) => {
  const r = rr[t];
  receipts[t] = { rank: r.rank, tier: r.tier, streak: r.streak, sinceRet: r.sinceRet, entryIdx: r.entryIdx };
});

const lad = RC.sectorLadder(series, REG_ETFS);
const ladderRows = lad.rows.map((r) => ({ t: r.t, name: r.name, side: r.side, r63: r.r63, r126: r.r126, blend: r.blend, third: r.third, streak: r.streak }));

const br = RC.baseRates(series, book, comp.state, 21);
const baseRates = {};
["risk-on", "neutral", "defensive"].forEach((s) => { baseRates[s] = { n: br[s].n, median: br[s].median, hit: br[s].hit }; });

const out = {
  dates,
  asof: dates[dates.length - 1],
  axis: "equity-trading-day (SPY)",
  mode: macroCount >= 2 ? "cross-asset" : "equity-internal",
  composite: {
    sum: comp.sum, state: comp.state, net_last: netLast,
    state_last: comp.state[last], score_last: comp.sum[last],
    active_legs: active, macro_legs: macroCount,
  },
  legs: info.map((x) => ({ key: x.key, label: x.label, type: x.type, val: x.val, macro: x.macro, series: x.series, last: x.last, cont: x.cont })),
  breadth: frac == null ? null : { frac, leg: breadthLeg, pool_size: book.length, pool: "BOOK", pool_tickers: book },
  ladder: { rows: ladderRows, divergence: lad.divergence },
  receipts,
  base_rates: baseRates,
  flips: RC.flips(dates, comp.state),
};

fs.writeFileSync(outPath, JSON.stringify(out));
console.log("wrote oracle regime -> " + outPath + " (" + active + " legs, book " + book.length + ", receipts " + Object.keys(receipts).length + ")");
