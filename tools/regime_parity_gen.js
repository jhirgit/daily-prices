// regime_parity_gen.js — generates the golden-fixture parity data for the
// Python port of the regime engine (SPEC-8).
//
//   node tools/regime_parity_gen.js
//
// Writes two files under parity/:
//   fixture.json   — the INPUTS: a list of {id, fn, args} cases.
//   expected.json  — {id: output} produced by the ORACLE (tools/regime_core.js).
//
// Process, so the frozen expected is computed on EXACTLY the bytes Python reads:
//   1. build the cases in memory,
//   2. write fixture.json,
//   3. RE-READ fixture.json from disk (JSON round-trip),
//   4. run regime_core.js on the re-read cases,
//   5. write expected.json.
//
// test_technicals.py then re-runs the Python port on fixture.json and asserts
// it matches expected.json (exact for int votes/states/tiers/streaks; abs<1e-9
// for blend/sinceRet/base-rate medians). regime_core.js is the oracle; if the
// port and the oracle ever disagree the parity test fails and blocks the port.

const fs = require("fs");
const path = require("path");
const RC = require("./regime_core.js");

const OUT = path.join(__dirname, "..", "parity");

// ---- deterministic array builders (serialized to JSON, so no shared RNG) ----
function lcg(seed) { let s = seed >>> 0; return () => (s = (1103515245 * s + 12345) >>> 0) / 4294967296; }
function rising(n, base = 100, step = 1) { const a = []; for (let i = 0; i < n; i++) a.push(base + i * step); return a; }
function falling(n) { const a = []; for (let i = 0; i < n; i++) a.push(100 - i * 0.5 > 1 ? 100 - i * 0.5 : 1); return a; }
function geo(n, g) { const a = []; let p = 100; for (let i = 0; i < n; i++) { a.push(p); p *= g; } return a; }

// A ragged cross-sectional panel: several names of length N, some starting late
// (leading nulls), a couple of forward-fill gaps, and a mid/flip in the ranks —
// this is the case that actually exercises the null-carry EMA, null-skipping
// SMA and the ragged rank/streak logic the emitter relies on.
function raggedPanel() {
  const N = 340;
  const rnd = lcg(42);
  const series = {};
  const names = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"];
  names.forEach((t, k) => {
    const start = k < 4 ? 0 : 30 * (k - 3);      // EEE, FFF start late -> leading nulls
    const a = new Array(N).fill(null);
    let p = 40 + 10 * k;
    for (let i = start; i < N; i++) {
      p *= Math.exp((rnd() - 0.48 + 0.0006 * k) * 0.02);   // gentle idiosyncratic drift
      a[i] = p;
    }
    // a couple of interior gaps (forward-fill target) on BBB
    if (t === "BBB") { a[120] = null; a[121] = null; a[200] = null; }
    series[t] = a;
  });
  const dates = [];
  for (let i = 0; i < N; i++) dates.push("2020-" + String(1 + (i % 12)).padStart(2, "0") + "-" + String(1 + (i % 27)).padStart(2, "0"));
  return { N, series, names, dates };
}

const RP = raggedPanel();

// ---- rankReceipts fixtures from test_regime.js (stable + flip-on-last-bar) ----
function rrStable() {
  const N = 280;
  const A = new Array(N).fill(100), B = new Array(N).fill(100);
  for (let i = 252; i <= 258; i++) { A[i] = 200; B[i] = 120; }
  A[273] = 100; A[279] = 130;
  return { A, B };
}
function rrFlip() {
  const N = 280;
  const A = new Array(N).fill(100), B = new Array(N).fill(100);
  for (let i = 252; i <= 257; i++) { A[i] = 150; B[i] = 140; }
  A[258] = 150; B[258] = 160;
  return { A, B };
}
function rrMid() {
  const { A, B } = rrStable();
  const N = A.length;
  const C = new Array(N).fill(100);
  for (let j = 252; j <= 258; j++) C[j] = 160;
  return { A, B, C };
}

// ---- sectorLadder fixture from test_regime.js ----
const LAD_N = 200;
const ladSeries = {
  E1: geo(LAD_N, 1.010), E2: geo(LAD_N, 1.008), E3: geo(LAD_N, 1.006),
  E4: geo(LAD_N, 1.004), E5: geo(LAD_N, 1.002), E6: geo(LAD_N, 1.000),
};
const ladEtfsDiv = [
  { t: "E1", name: "off-hi", side: "offense" }, { t: "E2", name: "def-hi", side: "defense" },
  { t: "E3", name: "off-mid", side: "offense" }, { t: "E4", name: "def-mid", side: "defense" },
  { t: "E5", name: "neu", side: null }, { t: "E6", name: "neu2", side: null },
];
const ladEtfsNoDiv = [
  { t: "E1", name: "off-hi", side: "offense" }, { t: "E2", name: "off-2", side: "offense" },
  { t: "E3", name: "def-mid", side: "defense" }, { t: "E4", name: "def-lo", side: "defense" },
  { t: "E5", name: "neu", side: null }, { t: "E6", name: "neu2", side: null },
];

// breadth fixture from test_regime.js
const BR_N = 250;
const up1 = rising(BR_N, 100, 1), up2 = rising(BR_N, 50, 2), down = rising(BR_N, 400, -1);

// baseRates realistic: use the ragged panel + a hand state vector
const stateVec = RP.dates.map((_, i) => (i % 3 === 0 ? "risk-on" : i % 3 === 1 ? "neutral" : "defensive"));

// leg vote series computed FROM the ragged panel (ratio + trend legs), then
// fed to compositeSeries — mirrors the real emitter path end to end.
const ratAB = RC.ratio(RP.series.AAA, RP.series.BBB);
const ratCD = RC.ratio(RP.series.CCC, RP.series.DDD);
const legR1 = RC.ratioLegSeries(ratAB);
const legR2 = RC.ratioLegSeries(ratCD);
const legT1 = RC.trendLegSeries(RP.series.EEE, false);
const legT2 = RC.trendLegSeries(RP.series.FFF, true);
const fracRP = RC.breadthSeries(RP.series, RP.names);
const legB = RC.breadthLegSeries(fracRP);

const cases = [
  // ratio
  { id: "ratio_basic", fn: "ratio", args: { num: [10, null, 30, 40], den: [2, 5, 0, 8] } },
  { id: "ratio_ragged", fn: "ratio", args: { num: RP.series.AAA, den: RP.series.BBB } },

  // leg votes
  { id: "ratioLeg_rising", fn: "ratioLegSeries", args: { rat: rising(120) } },
  { id: "ratioLeg_falling", fn: "ratioLegSeries", args: { rat: falling(120) } },
  { id: "ratioLeg_null_last", fn: "ratioLegSeries", args: { rat: (() => { const r = rising(120); r[119] = null; return r; })() } },
  { id: "ratioLeg_ragged_ratio", fn: "ratioLegSeries", args: { rat: ratAB } },
  { id: "trendLeg_noinvert", fn: "trendLegSeries", args: { px: rising(120), invert: false } },
  { id: "trendLeg_invert", fn: "trendLegSeries", args: { px: rising(120), invert: true } },
  { id: "trendLeg_ragged_late", fn: "trendLegSeries", args: { px: RP.series.EEE, invert: false } },
  { id: "trendLeg_ragged_invert", fn: "trendLegSeries", args: { px: RP.series.FFF, invert: true } },

  // breadth
  { id: "breadth_both_up", fn: "breadthSeries", args: { series: { AAA: up1, BBB: up2 }, names: ["AAA", "BBB"] } },
  { id: "breadth_split", fn: "breadthSeries", args: { series: { AAA: up1, DDD: down }, names: ["AAA", "DDD"] } },
  { id: "breadth_missing", fn: "breadthSeries", args: { series: { AAA: up1 }, names: ["AAA", "ZZZ"] } },
  { id: "breadth_ragged", fn: "breadthSeries", args: { series: RP.series, names: RP.names } },
  { id: "breadthLeg_boundaries", fn: "breadthLegSeries", args: { frac: [null, 0.55, 0.5499, 0.35, 0.3501, 0.45, 1, 0] } },
  { id: "breadthLeg_ragged", fn: "breadthLegSeries", args: { frac: fracRP } },

  // composite
  { id: "composite_mixed", fn: "compositeSeries", args: { legs: [[1, -1], [1, 0], [-1, 1]] } },
  { id: "composite_6leg_on", fn: "compositeSeries", args: { legs: [[1, 1], [1, 1], [1, 1], [0, 0], [0, 0], [0, 0]] } },
  { id: "composite_frac_p34", fn: "compositeSeries", args: { legs: [[0.34]] } },
  { id: "composite_frac_p3399", fn: "compositeSeries", args: { legs: [[0.3399]] } },
  { id: "composite_frac_m34", fn: "compositeSeries", args: { legs: [[-0.34]] } },
  { id: "composite_frac_m3399", fn: "compositeSeries", args: { legs: [[-0.3399]] } },
  { id: "composite_ragged_6leg", fn: "compositeSeries", args: { legs: [legR1, legR2, legT1, legT2, legB] } },

  // flips
  { id: "flips_basic", fn: "flips", args: { dates: ["d0", "d1", "d2", "d3", "d4", "d5"], state: ["neutral", "neutral", "risk-on", "risk-on", "defensive", "neutral"] } },
  { id: "flips_nochange", fn: "flips", args: { dates: ["a", "b", "c"], state: ["x", "x", "x"] } },

  // baseRates
  { id: "baserates_H1", fn: "baseRates", args: { series: { N: [100, 110, 99, 99, 108] }, names: ["N"], state: ["risk-on", "risk-on", "defensive", "neutral", "risk-on"], H: 1 } },
  { id: "baserates_short_default", fn: "baseRates", args: { series: { N: [1, 2, 3] }, names: ["N"], state: ["risk-on", "risk-on", "risk-on"] } },
  { id: "baserates_ragged", fn: "baseRates", args: { series: RP.series, names: RP.names, state: stateVec, H: 21 } },

  // rankReceipts
  { id: "receipts_stable", fn: "rankReceipts", args: (() => { const { A, B } = rrStable(); return { series: { A, B }, names: ["A", "B"] }; })() },
  { id: "receipts_mid", fn: "rankReceipts", args: (() => { const { A, B, C } = rrMid(); return { series: { A, B, C }, names: ["A", "B", "C"] }; })() },
  { id: "receipts_flip", fn: "rankReceipts", args: (() => { const { A, B } = rrFlip(); return { series: { A, B }, names: ["A", "B"] }; })() },
  { id: "receipts_missing", fn: "rankReceipts", args: (() => { const { A, B } = rrStable(); return { series: { A, B }, names: ["A", "B", "GHOST"] }; })() },
  { id: "receipts_ragged", fn: "rankReceipts", args: { series: RP.series, names: RP.names } },

  // ret
  { id: "ret_lag3", fn: "ret", args: { close: [10, 11, 12, 15], lag: 3 } },
  { id: "ret_lag1", fn: "ret", args: { close: [10, 11, 12, 15], lag: 1 } },
  { id: "ret_beyond", fn: "ret", args: { close: [10, 11, 12, 15], lag: 10 } },
  { id: "ret_zero_base", fn: "ret", args: { close: [0, 5], lag: 1 } },

  // sectorLadder
  { id: "ladder_div", fn: "sectorLadder", args: { series: ladSeries, etfs: ladEtfsDiv } },
  { id: "ladder_nodiv", fn: "sectorLadder", args: { series: ladSeries, etfs: ladEtfsNoDiv } },
  { id: "ladder_missing", fn: "sectorLadder", args: { series: { E1: ladSeries.E1 }, etfs: [{ t: "E1", name: "x", side: null }, { t: "NOPE", name: "y", side: null }] } },
  { id: "ladder_ragged", fn: "sectorLadder", args: { series: RP.series, etfs: RP.names.map((t, i) => ({ t, name: t, side: i % 2 ? "offense" : "defense" })) } },
  // Stream D (9/2/26): basket sleds + sector passthrough. E9 has a null hole and a
  // leading gap; NOPE is absent; the basket joins the field as a first-class row.
  { id: "ladder_basket", fn: "sectorLadder", args: { series: Object.assign({}, ladSeries, { E9: [null, null].concat(geo(LAD_N - 2, 1.003).map((v, i) => (i === 50 ? null : v))) }), etfs: [
    { t: "E1", name: "off-hi", side: "offense", sector: "A" }, { t: "E2", name: "def-hi", side: "defense", sector: "B" },
    { t: "BK", name: "basket", side: "offense", sector: "A", gics: "45301020", basket: ["E3", "E9", "NOPE", "E5"] },
    { t: "E4", name: "def-mid", side: "defense", sector: "B" }, { t: "E6", name: "neu2", side: null, sector: "C" },
  ] } },
  { id: "ladder_basket_empty", fn: "sectorLadder", args: { series: { E1: ladSeries.E1 }, etfs: [ { t: "E1", name: "x", side: null }, { t: "BK", name: "b", side: null, basket: ["NOPE", "NOPE2"] } ] } },
  // v60: grp dedup — E2/E4 twin E1/E3; twins carry levels only (third/third21 null, twin_of set)
  { id: "ladder_grp_twins", fn: "sectorLadder", args: { series: ladSeries, etfs: [
    { t: "E1", name: "semi", side: "offense", grp: "semi" }, { t: "E2", name: "semi2", side: "offense", grp: "semi" },
    { t: "E3", name: "gold", side: "defense", grp: "gold" }, { t: "E4", name: "gold2", side: "defense", grp: "gold" },
    { t: "E5", name: "neu", side: null }, { t: "E6", name: "neu2", side: null } ] } },
  { id: "ladder_grp_missing_primary", fn: "sectorLadder", args: { series: { E2: ladSeries.E2, E3: ladSeries.E3, E5: ladSeries.E5 }, etfs: [
    { t: "E1", name: "semi", side: "offense", grp: "semi" }, { t: "E2", name: "semi2", side: "offense", grp: "semi" },
    { t: "E3", name: "gold", side: "defense", grp: "gold" }, { t: "E5", name: "neu", side: null } ] } },

  // v60 composite: hysteresis + first_actives / per-bar k
  { id: "composite_hyst_hold", fn: "compositeSeries", args: { legs: [[1, 1, 0, 0, -1], [1, 0, 1, 0, -1], [1, 0, 0, 0, -1]] } },
  { id: "composite_hyst_direct_flip", fn: "compositeSeries", args: { legs: [[1, -1], [1, -1], [1, -1]] } },
  { id: "composite_first_actives", fn: "compositeSeries", args: { legs: [[1, 1, 1, 1], [0, 0, 1, 1], [0, 0, 0, -1]], firstActives: [0, 2, 3] } },
  { id: "composite_ragged_fas", fn: "compositeSeries", args: { legs: [legR1, legR2, legT1, legT2, legB], firstActives: [22, 22, 60, 120, 199] } },

  // v60 durations
  { id: "durations_basic", fn: "durations", args: { state: ["neutral", "neutral", "risk-on", "risk-on", "risk-on", "defensive", "neutral", "neutral"] } },
  { id: "durations_single", fn: "durations", args: { state: ["risk-on"] } },
  { id: "durations_empty", fn: "durations", args: { state: [] } },

  // v60 series helpers
  { id: "retSeries_gaps", fn: "retSeries", args: { close: [100, 110, null, 121, 0, 50, 55] } },
  { id: "rv_geo", fn: "rvSeries", args: { close: geo(60, 1.01), n: 21 } },
  { id: "rv_ragged", fn: "rvSeries", args: { close: RP.series.EEE, n: 21 } },
  { id: "pct_basic", fn: "pctRankSeries", args: { vals: rising(30), win: 10 } },
  { id: "pct_nullwin", fn: "pctRankSeries", args: { vals: (() => { const v = rising(30); v[25] = null; return v; })(), win: 10 } },
  { id: "pctVote_bounds", fn: "pctVoteSeries", args: { pct: [null, 0.30, 0.3001, 0.70, 0.6999, 0.5, 0, 1], lo: 0.30, hi: 0.70 } },
  { id: "corrPair_same", fn: "corrPairSeries", args: { a: RP.series.AAA, b: RP.series.AAA, win: 63 } },
  { id: "corrPair_ab", fn: "corrPairSeries", args: { a: RP.series.AAA, b: RP.series.BBB, win: 63 } },
  { id: "corrPair_late", fn: "corrPairSeries", args: { a: RP.series.AAA, b: RP.series.FFF, win: 63 } },
  { id: "corrVote_bounds", fn: "corrVoteSeries", args: { corr: [null, -0.2, -0.1999, 0.2, 0.1999, 0, -1, 1], thr: 0.20 } },
  { id: "avgCorr_ragged", fn: "avgCorrSeries", args: { series: RP.series, names: RP.names, win: 63, minN: 3 } },
  { id: "avgCorr_minN_gate", fn: "avgCorrSeries", args: { series: RP.series, names: RP.names, win: 63, minN: 7 } },
  { id: "legFirstActive_ragged", fn: "legFirstActive", args: { cont: ratAB } },
  { id: "legFirstActive_never", fn: "legFirstActive", args: { cont: [null, null, null] } },
  { id: "firstNonNull_mid", fn: "firstNonNull", args: { arr: [null, null, 5, null] } },
  { id: "firstNonNull_none", fn: "firstNonNull", args: { arr: [null, null] } },

  // v60 baseRatesMulti
  { id: "baseratesmulti_ragged", fn: "baseRatesMulti", args: { series: RP.series, names: RP.names, state: stateVec, hs: [5, 21, 63] } },
];

// prefix-invariance (lookahead) cases from test_regime.js: truncate the flip
// fixture at 274..279 and confirm parity holds at each length too.
{
  const { A, B } = rrFlip();
  for (let L = 274; L <= 279; L++) {
    cases.push({ id: "receipts_prefix_" + L, fn: "rankReceipts", args: { series: { A: A.slice(0, L), B: B.slice(0, L) }, names: ["A", "B"] } });
  }
}

function run(fn, a) {
  switch (fn) {
    case "ratio": return RC.ratio(a.num, a.den);
    case "ratioLegSeries": return RC.ratioLegSeries(a.rat);
    case "trendLegSeries": return RC.trendLegSeries(a.px, a.invert);
    case "breadthSeries": return RC.breadthSeries(a.series, a.names);
    case "breadthLegSeries": return RC.breadthLegSeries(a.frac);
    case "compositeSeries": return RC.compositeSeries(a.legs, a.firstActives);
    case "flips": return RC.flips(a.dates, a.state);
    case "baseRates": return RC.baseRates(a.series, a.names, a.state, a.H);
    case "baseRatesMulti": return RC.baseRatesMulti(a.series, a.names, a.state, a.hs);
    case "rankReceipts": return RC.rankReceipts(a.series, a.names);
    case "ret": return RC.ret(a.close, a.lag);
    case "sectorLadder": return RC.sectorLadder(a.series, a.etfs);
    case "durations": return RC.durations(a.state);
    case "retSeries": return RC.retSeries(a.close);
    case "rvSeries": return RC.rvSeries(a.close, a.n);
    case "pctRankSeries": return RC.pctRankSeries(a.vals, a.win);
    case "pctVoteSeries": return RC.pctVoteSeries(a.pct, a.lo, a.hi);
    case "corrPairSeries": return RC.corrPairSeries(a.a, a.b, a.win);
    case "corrVoteSeries": return RC.corrVoteSeries(a.corr, a.thr);
    case "avgCorrSeries": return RC.avgCorrSeries(a.series, a.names, a.win, a.minN);
    case "legFirstActive": return RC.legFirstActive(a.cont);
    case "firstNonNull": return RC.firstNonNull(a.arr);
    default: throw new Error("unknown fn " + fn);
  }
}

fs.mkdirSync(OUT, { recursive: true });
const fixturePath = path.join(OUT, "fixture.json");
fs.writeFileSync(fixturePath, JSON.stringify({ cases }, null, 1));

// Re-read from disk so expected is computed on the exact round-tripped bytes.
const reread = JSON.parse(fs.readFileSync(fixturePath, "utf8"));
const expected = {};
for (const c of reread.cases) expected[c.id] = run(c.fn, c.args);
fs.writeFileSync(path.join(OUT, "expected.json"), JSON.stringify(expected, null, 1));

console.log("wrote " + reread.cases.length + " cases -> parity/fixture.json + parity/expected.json");
