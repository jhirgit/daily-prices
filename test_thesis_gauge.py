#!/usr/bin/env python3
"""
Tests for thesis_gauge.py (the AI-hardware thesis gauge). Offline: synthetic
tapes and an in-memory SQLite fixture only, no prices.db, no network.

What is pinned, and why:
  1. SIGN ALIGNMENT -- + = healthy for every input: semis beating SPY scores +,
     a rising 10-year yield scores -, a deeper neocloud drawdown scores -.
  2. THE NEUTRAL BAND -- strict at +/-BAND, edges named (A/B, B/C, C/D, A/D),
     both neutral = center, quadrant "neutral" on any edge.
  3. MISSING INPUTS -- a leg absent from prices.db is "missing", scores
     nothing, and the dial still reads from the rest; under MIN_INPUTS it
     reads None rather than a mean of one.
  4. STALE SERIES -- a leg that stopped printing (regime._series_stale) is
     "stale" and scores nothing on the last day; a basket goes stale only when
     over half its members have.
  5. THE FIXTURE CASE -- a tape built to be "demand up, financing tightening"
     lands in B; the event hook moves a dial and is clipped.
  6. NO LOOK-AHEAD -- every score at <= d is unchanged when the tape after d is
     replaced (with a positive control that later scores DO change).
  7. THE PAYLOAD -- build() over an in-memory database: no NaN, the market-proxy
     note, the event hook empty, history lengths agree, evidence says "states,
     not forecasts" when nothing earns an edge, and no position words.

Run:  python test_thesis_gauge.py      (prints "N passed, M failed")
"""

import json
import math
import sqlite3
import sys

import numpy as np
import pandas as pd

import regime as RG
import thesis_gauge as TG

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

PASSED = []
FAILED = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}" + ("" if ok else f", want {want!r}"))
    (PASSED if ok else FAILED).append(name)


def check_true(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{(' -- ' + str(detail)) if detail else ''}")
    (PASSED if cond else FAILED).append(name)


# --------------------------------------------------------------------------
# synthetic tape: flat noise for 500 sessions, then a regime for 100
# --------------------------------------------------------------------------
N = 600
TURN = 500
DATES = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2024-01-02", periods=N)]

DEMAND_UP = ["SMH", "SOXX"] + ["LITE", "COHR", "AAOI", "CIEN", "FN", "GLW"] \
    + ["ON", "NVTS", "MPWR", "AOSL", "VICR", "STM"] + TG.AI_HW
FIN_DOWN = ["CRWV", "NBIS", "IREN", "APLD", "CORZ", "WULF", "ORCL"]


def tape(drifts, seed=7, n=N, turn=TURN, absent=(), tnx_up=True):
    """{ticker: [closes]} -- every ticker a seeded random walk with no drift
    before `turn` and drifts[t] per session after it."""
    rng = np.random.default_rng(seed)
    names = set(["SPY", "SMH", "SOXX", "MU", "LRCX", "ASML", "HYG", "IEF", "^TNX",
                 "MSFT", "GOOGL", "AMZN", "META"]) | set(DEMAND_UP) | set(FIN_DOWN)
    out = {}
    for t in sorted(names - set(absent)):
        if t == "^TNX":
            lvl = 4.0 + np.cumsum(rng.normal(0, 0.02, n))
            if tnx_up:
                lvl[turn:] += np.arange(1, n - turn + 1) * 0.01
            out[t] = [float(x) for x in lvl]
            continue
        r = rng.normal(0, 0.006, n)
        r[turn:] += drifts.get(t, 0.0)
        out[t] = [float(x) for x in 100 * np.exp(np.cumsum(r))]
    return out


def b_tape(**kw):
    dr = {t: 0.004 for t in DEMAND_UP}
    dr.update({"MU": 0.008, "LRCX": 0.008, "ASML": 0.008, "SMH": 0.004, "SOXX": 0.004})
    dr.update({t: -0.006 for t in FIN_DOWN})
    dr["HYG"] = -0.001
    return tape(dr, **kw)


def zlast(G, key):
    v = G["z"][key].iloc[-1]
    return None if pd.isna(v) else float(v)


# --------------------------------------------------------------------------
print("\n# 1. sign alignment")
S = b_tape()
G = TG.compute(DATES, S)
check_true("SMH beating SPY scores +", zlast(G, "smh_spy_63") > 1.0, zlast(G, "smh_spy_63"))
check_true("MU beating SMH scores +", zlast(G, "mu_smh_63") > 1.0, zlast(G, "mu_smh_63"))
check_true("breadth all-above scores +", zlast(G, "ai_breadth_50") > 0.5, zlast(G, "ai_breadth_50"))
check_true("rising 10y level scores -", zlast(G, "tnx_level") < -1.0, zlast(G, "tnx_level"))
check_true("rising 10y change scores -", zlast(G, "tnx_chg_21") < 0, zlast(G, "tnx_chg_21"))
check_true("neocloud drawdown scores -", zlast(G, "neocloud_dd_63") < -1.0, zlast(G, "neocloud_dd_63"))
check_true("neocloud lagging hyperscalers scores -", zlast(G, "neo_hyper_63") < -1.0, zlast(G, "neo_hyper_63"))
check_true("every z clipped to +/-3", float(G["z"].abs().max().max()) <= TG.Z_CLIP + 1e-12)
raw = pd.Series(np.r_[np.zeros(300), np.ones(10)])
check_true("zscore sign -1 flips", TG.zscore(raw, -1).iloc[-1] < 0 < TG.zscore(raw, 1).iloc[-1])
check("zscore needs Z_MIN sessions", bool(pd.isna(TG.zscore(pd.Series(np.arange(100.0)), 1).iloc[-1])), True)

# --------------------------------------------------------------------------
print("\n# 2. neutral band")
b = TG.BAND
check("A", TG.zone_of(b + 0.01, b + 0.01), "A")
check("B", TG.zone_of(1.0, -1.0), "B")
check("C", TG.zone_of(-1.0, -1.0), "C")
check("D", TG.zone_of(-1.0, 1.0), "D")
check("boundary is neutral (strict)", TG.zone_of(b, -1.0), "B/C")
check("A/B edge", TG.zone_of(1.0, 0.0), "A/B")
check("C/D edge", TG.zone_of(-1.0, -b), "C/D")
check("A/D edge", TG.zone_of(0.1, 1.0), "A/D")
check("center", TG.zone_of(0.0, 0.0), "center")
check("edge -> quadrant neutral", TG.quadrant_of("A/B"), "neutral")
check("center -> quadrant neutral", TG.quadrant_of("center"), "neutral")
check("no dial -> no zone", TG.zone_of(None, 1.0), None)
check("NaN dial -> no zone", TG.zone_of(float("nan"), 1.0), None)

# --------------------------------------------------------------------------
print("\n# 3. missing inputs")
S3 = b_tape(absent=("CRWV", "ORCL"))
G3 = TG.compute(DATES, S3)
check("CRWV missing", G3["status"]["crwv_spy_63"]["status"], "missing")
check("ORCL missing", G3["status"]["orcl_spy_63"]["status"], "missing")
check("DRAM missing (not in the tape)", G3["status"]["dram_smh_63"]["status"], "missing")
check_true("missing scores nothing", zlast(G3, "crwv_spy_63") is None)
check_true("financing still reads from the rest", G3["dial"]["financing"].iloc[-1] < -TG.BAND,
           G3["dial"]["financing"].iloc[-1])
check("financing n drops by two", int(G3["n"]["financing"].iloc[-1]), int(G["n"]["financing"].iloc[-1]) - 2)
check("dial_mean under MIN_INPUTS is None", TG.dial_mean([1.0, 2.0]), (None, 2))
check("dial_mean skips None/NaN", TG.dial_mean([1.0, None, float("nan"), 2.0, 3.0]), (2.0, 3))
# a missing basket member just sits out; the basket still prices
S3b = b_tape(absent=("AAOI", "FN"))
G3b = TG.compute(DATES, S3b)
check("OPTICS prices on 4 of 6 members", G3b["present"]["OPTICS"], ["LITE", "COHR", "CIEN", "GLW"])
check("OPTICS input ok", G3b["status"]["optics_spy_63"]["status"], "ok")

# --------------------------------------------------------------------------
print("\n# 4. stale series")
S4 = b_tape()
cut = 8  # past regime.MAX_STALE_SESSIONS: the panel would have None-d the tail
S4["CRWV"] = S4["CRWV"][:-cut] + [None] * cut
check_true("fixture is stale by regime's own rule", RG._series_stale(S4["CRWV"]))
G4 = TG.compute(DATES, S4)
check("CRWV stale", G4["status"]["crwv_spy_63"]["status"], "stale")
check_true("stale scores nothing today", zlast(G4, "crwv_spy_63") is None)
check_true("but it scored while it printed", not pd.isna(G4["z"]["crwv_spy_63"].iloc[-cut - 1]))
# basket: 2 of 6 neocloud members dead -> still ok; 4 of 6 -> stale
S4b = b_tape()
for t in ("NBIS", "IREN"):
    S4b[t] = S4b[t][:-cut] + [None] * cut
G4b = TG.compute(DATES, S4b)
check("NEOCLOUD with 2/6 dead is ok", G4b["status"]["neocloud_dd_63"]["status"], "ok")
check("dead members are named", sorted(G4b["status"]["neocloud_dd_63"]["stale_legs"]), ["IREN", "NBIS"])
for t in ("APLD", "CORZ"):
    S4b[t] = S4b[t][:-cut] + [None] * cut
G4c = TG.compute(DATES, S4b)
check("NEOCLOUD with 4/6 dead is stale", G4c["status"]["neocloud_dd_63"]["status"], "stale")
check("neo vs hyper stale with it", G4c["status"]["neo_hyper_63"]["status"], "stale")
check_true("stale basket scores nothing today", zlast(G4c, "neocloud_dd_63") is None)
# an all-dead basket must not read as a flat line
S4d = b_tape()
for t in ("CRWV", "NBIS", "IREN", "APLD", "CORZ", "WULF"):
    S4d[t] = S4d[t][:-cut] + [None] * cut
L, _ = TG.legs(S4d)
check("all-dead basket ends where its members did", L["NEOCLOUD"][-1], None)

# --------------------------------------------------------------------------
print("\n# 5. fixture case + event hook")
dl = G["dial"]
check_true("demand dial up", dl["demand"].iloc[-1] > TG.BAND, dl["demand"].iloc[-1])
check_true("financing dial down", dl["financing"].iloc[-1] < -TG.BAND, dl["financing"].iloc[-1])
check("quadrant B", G["quad"][-1], "B")
check("zone B", G["zone"][-1], "B")
check_true("flat history before the turn is mostly neutral",
           sum(q == "neutral" for q in G["quad"][300:TURN]) > 0)
mz = {k: zlast(G, k) for k in G["z"].columns}
base = TG.combine(mz)
check("combine == compute on the last day", base["quadrant"], "B")
check_true("combine dials match", abs(base["demand"] - dl["demand"].iloc[-1]) < 1e-12)
ev = TG.combine(mz, [{"dial": "financing", "key": "capex_guide", "z": 99.0}] * 20)
check_true("events join their dial, clipped", ev["financing"] > base["financing"] and ev["financing"] <= TG.Z_CLIP,
           ev["financing"])
check("events counted as inputs", ev["n"]["financing"], base["n"]["financing"] + 20)
check("unknown dial / bad z ignored",
      TG.combine(mz, [{"dial": "x", "z": 1.0}, {"dial": "demand", "z": "up"}])["n"], base["n"])
check("enough positive financing events flip B to A", ev["quadrant"], "A")

# --------------------------------------------------------------------------
print("\n# 6. no look-ahead")
d = 540
S6 = b_tape()
alt = tape({t: -0.02 for t in DEMAND_UP}, seed=99)
S6b = {t: (S6[t][:d + 1] + alt[t][d + 1:]) if t in alt else S6[t] for t in S6}
Ga, Gb = TG.compute(DATES, S6), TG.compute(DATES, S6b)
za, zb = Ga["z"].iloc[:d + 1], Gb["z"].iloc[:d + 1]
check_true("scores at <= d unchanged", bool(((za - zb).abs().fillna(0) < 1e-12).all().all()))
check("zones at <= d unchanged", Ga["zone"][:d + 1], Gb["zone"][:d + 1])
check_true("positive control: later scores change",
           float((Ga["z"].iloc[d + 30:] - Gb["z"].iloc[d + 30:]).abs().max().max()) > 0.5)

# --------------------------------------------------------------------------
print("\n# 7. evidence + payload")
check("episodes", TG.episodes(["A", "A", None, "A", "B", "B"]), [0, 0, -1, 1, 2, 2])
p = TG.persistence(["A", "A", "B", "B", "B", "A"])
check("persistence next-day", p["next_day_same"], 0.6)
check("persistence episodes", p["episodes"], 3)


def fixture_db(series):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE daily_prices (ticker TEXT, date TEXT, open REAL, high REAL, low REAL, "
                 "close REAL, adj_close REAL, volume INTEGER, source TEXT, updated_at TEXT, "
                 "PRIMARY KEY (ticker, date))")
    rows = [(t, DATES[i], v, v) for t, arr in series.items() for i, v in enumerate(arr) if v is not None]
    conn.executemany("INSERT INTO daily_prices (ticker, date, close, adj_close) VALUES (?,?,?,?)", rows)
    return conn


out = TG.build(fixture_db(b_tape()), reps=200)
text = json.dumps(out, allow_nan=False)  # raises on NaN
check("asof", out["asof"], DATES[-1])
check("quadrant B in the payload", out["quadrant"], "B")
check_true("market-proxy note", "MARKET PROXY" in out["note"])
check("event hook empty", out["event_inputs"]["applied"], [])
check("one input row per definition", [i["key"] for i in out["inputs"]], [i["key"] for i in TG.INPUTS])
check("DRAM reported missing", next(i["status"] for i in out["inputs"] if i["key"] == "dram_smh_63"), "missing")
h = out["history"]
check("history window", len(h["dates"]), min(TG.HISTORY, N))
check_true("history columns aligned",
           all(len(h[k]) == len(h["dates"]) for k in ("demand", "financing", "zone", "quadrant", "smh"))
           and all(len(v) == len(h["dates"]) for v in h["inputs_z"].values()))
check("zone_since on the last run", out["zone"], h["zone"][-1])
ev = out["evidence"]
check_true("evidence verdict honest", ev["edges"] or ev["verdict"].startswith("states, not forecasts"),
           ev["verdict"])
check_true("evidence carries N", all("days_labelled" in c and "episodes" in c for c in ev["by_quadrant"].values()))
lower = text.lower()
for w in ("holding", "basis", "account", "position", "my book", "shares"):
    check_true(f"no '{w}' in the payload", w not in lower)

print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
sys.exit(1 if FAILED else 0)
