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
  info.push({ key, label: name, type: "ratio", val: nm + "÷" + dn, macro: !!macro, voting: true, series: s, last: s[s.length - 1], cont, first_active: RC.legFirstActive(cont) });
}
function addTrend(key, name, tickList, invert, macro) {
  const t = firstPresent(tickList); if (!t) return;
  const s = RC.trendLegSeries(series[t], invert);
  legs.push(s); legKeys.push(key);
  info.push({ key, label: name, type: "trend", val: (invert ? "↓" : "↑") + t, macro: !!macro, voting: true, series: s, last: s[s.length - 1], cont: series[t], first_active: RC.legFirstActive(series[t]) });
}
function addCorr(key, name, aList, bList, macro) {
  // v62: DISPLAY-ONLY tape-type read — not pushed into legs (no vote, no denominator seat)
  const ta = firstPresent(aList), tb = firstPresent(bList); if (!ta || !tb) return;
  const cont = RC.corrPairSeries(series[ta], series[tb], 63);
  const s = RC.corrVoteSeries(cont, 0.20);
  info.push({ key, label: name, type: "corr", val: ta + "↔" + tb + " 63d", macro: !!macro, voting: false, series: s, last: s[s.length - 1], cont, first_active: RC.firstNonNull(cont) });
}
function addVol(key, name, tickList, macro) {
  const t = firstPresent(tickList); if (!t) return;
  const rv = RC.rvSeries(series[t], 21);
  const pct = RC.pctRankSeries(rv, 252);
  const s = RC.pctVoteSeries(pct, 0.30, 0.70);
  legs.push(s); legKeys.push(key);
  const pl = pct.length ? pct[pct.length - 1] : null;
  info.push({ key, label: name, type: "vol", val: t + " rv21" + (pl == null ? "" : " p" + Math.round(pl * 100)), macro: !!macro, voting: true, series: s, last: s[s.length - 1], cont: rv, first_active: RC.firstNonNull(pct) });
}
function addDisp(key, name, names) {
  const ac = RC.avgCorrSeries(series, names, 63, 8);
  if (RC.firstNonNull(ac) >= ac.length) return;
  const pct = RC.pctRankSeries(ac, 252);
  const s = RC.pctVoteSeries(pct, 0.30, 0.70);
  legs.push(s); legKeys.push(key);
  const pl = pct.length ? pct[pct.length - 1] : null;
  info.push({ key, label: name, type: "disp", val: "avg corr" + (pl == null ? "" : " p" + Math.round(pl * 100)), macro: false, voting: true, series: s, last: s[s.length - 1], cont: ac, first_active: RC.firstNonNull(pct) });
}
addRatio("credit_hy_ig", "Credit — HY vs IG", ["HYG"], ["LQD"], true);
addRatio("copper_gold", "Copper / gold", ["CPER"], ["GLD"], true);
addTrend("dollar_inv", "Dollar (inverse)", ["UUP"], true, true);
addCorr("stocks_bonds_corr", "Stocks–bonds corr", ["SPY"], ["TLT"], true);
addRatio("offense_defense", "Offense vs defense", ["SMH", "SOXX"], ["GDX", "GDXJ", "RING"], false);
addRatio("beta_appetite", "Beta appetite", ["QQQ", "IWM", "SOXX"], ["SPY"], false);
addVol("spy_vol", "Volatility (SPY)", ["SPY"], false);

let frac = book.length ? RC.breadthSeries(series, book) : null;
let breadthLeg = null;
if (frac) {
  breadthLeg = RC.breadthLegSeries(frac);
  legs.push(breadthLeg); legKeys.push("breadth_book_200d");
  info.push({ key: "breadth_book_200d", label: "Breadth (book >200d)", type: "breadth", val: null, macro: false, voting: true, series: breadthLeg, last: breadthLeg[breadthLeg.length - 1], cont: frac, first_active: RC.firstNonNull(frac) });
}
if (book.length) addDisp("book_dispersion", "Book dispersion", book);

const fas = info.filter((x) => x.voting).map((x) => x.first_active);
const comp = RC.compositeSeries(legs, fas);
const active = legs.length;  // voting legs only
const macroCount = info.filter((x) => x.macro && x.voting).length;
const last = comp.sum.length - 1;
const kLast = last >= 0 ? comp.k[last] : 0;
const netLast = kLast ? comp.sum[last] / kLast : null;
const dur = RC.durations(comp.state);

// receipts pool = emitted (>=MIN_BARS) tickers minus sector ETFs (client's TECH.tickers filter)
const recPool = P.emitted.filter((t) => series[t] && !etfSet[t]);
const rr = RC.rankReceipts(series, recPool);
const receipts = {};
Object.keys(rr).forEach((t) => {
  const r = rr[t];
  receipts[t] = { rank: r.rank, tier: r.tier, streak: r.streak, sinceRet: r.sinceRet, entryIdx: r.entryIdx };
});

const lad = RC.sectorLadder(series, REG_ETFS);
const ladderRows = lad.rows.map((r) => ({ t: r.t, name: r.name, side: r.side, r5: r.r5, r21: r.r21, r63: r.r63, r126: r.r126, blend: r.blend, third: r.third, third21: r.third21, streak: r.streak, twin_of: r.twin_of }));

const baseRates = RC.baseRatesMulti(series, book, comp.state, [5, 21, 63]);

const out = {
  dates,
  asof: dates[dates.length - 1],
  axis: "equity-trading-day (SPY)",
  mode: macroCount >= 2 ? "cross-asset" : "equity-internal",
  composite: {
    sum: comp.sum, state: comp.state, k: comp.k, net_last: netLast,
    state_last: comp.state[last], score_last: comp.sum[last],
    active_legs: active, k_last: kLast, macro_legs: macroCount,
    hysteresis: { enter: 0.34, exit: 0.17 },
  },
  durations: dur,
  legs: info.map((x) => ({ key: x.key, label: x.label, type: x.type, val: x.val, macro: x.macro, voting: x.voting, series: x.series, last: x.last, first_active: x.first_active, cont: x.cont })),
  breadth: frac == null ? null : { frac, leg: breadthLeg, pool_size: book.length, pool: "BOOK", pool_tickers: book },
  ladder: { rows: ladderRows, divergence: lad.divergence },
  receipts,
  base_rates: baseRates,
  flips: RC.flips(dates, comp.state),
};

fs.writeFileSync(outPath, JSON.stringify(out));
console.log("wrote oracle regime -> " + outPath + " (" + active + " legs, book " + book.length + ", receipts " + Object.keys(receipts).length + ")");
