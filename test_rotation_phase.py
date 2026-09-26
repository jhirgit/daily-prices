#!/usr/bin/env python3
"""
Tests for rotation_phase.py / rotation_theses.py / rotation_evidence.py
(SPEC-86 s4-s8, s10). Offline: synthetic series and hand-built fixtures only,
no prices.db, no network (fetch_yf is never called).

What is pinned, and why:
  1. THE NINE LABELS (s4) -- one synthetic tape per label, first match wins.
  2. THE DIRECTION-AWARE STATUS TABLE (s5) for up / down / flat trends, and the
     vectorised twin the calibration walks history with.
  3. DEAD ZONES IN THE MEASURE'S OWN VOL -- a status is invariant to the
     measure's scale (copper vs the curve), and a fixed absolute move that
     clears the bar on a quiet series does not on a 3x noisier one.
  4. NO LOOK-AHEAD, WITH A POSITIVE CONTROL -- every state at <= d is unchanged
     when every bar after d is replaced; the replaced tape must change later
     states (so a pass is not a no-op); and a deliberately leaky function is
     caught by the same harness.
  5. THE TABLE (s6) -- all 63 primaries present with >= 3 checks, twins resolve
     to their primary, row-specific signatures present, spec sign conventions.
  6. THE PAYLOAD (s10) -- an offline build over a synthetic panel: 80 rows,
     twins carry `primary` and inherited checks, holdings checks read the
     members fixture, no NaN, no banned word in any generated text.
  7. THE EVIDENCE STATISTICS (s7) -- episodes, weighted median, edge rule.

Run:  python test_rotation_phase.py      (prints "N passed, M failed")
"""

import json
import math
import sys

import numpy as np
import pandas as pd

import regime as RG
import rotation_evidence as EV
import rotation_phase as RP
import rotation_theses as TH

try:  # the read lines carry an em dash and a true minus; a cp1252 console cannot print them
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
# synthetic tapes
# --------------------------------------------------------------------------

def tape(segs, wig=0.002, period=7):
    """Price path from (n_bars, daily log drift) segments plus a small
    deterministic wiggle (a pure line has zero residual and no t-stat)."""
    d = []
    for n, mu in segs:
        d += [mu] * n
    d = np.array(d, float)
    i = np.arange(len(d))
    return 100.0 * np.exp(np.cumsum(d) + wig * np.sin(2 * np.pi * i / period))


def flat_tape(n=400, amp=0.03, period=42):
    """A cosine symmetric about the centre of the last 126 bars: OLS slope 0."""
    i = np.arange(n)
    return 100.0 * np.exp(amp * np.cos(2 * np.pi * (i - (n - 63.5)) / period))


LABEL_TAPES = {
    "steady_up": tape([(400, 0.002)]),
    "accelerating_up": tape([(379, 0.0015), (21, 0.006)]),
    "rolling_over": tape([(264, 0.001), (63, 0.004), (73, -0.0012)]),
    "slowing_up": tape([(379, 0.003), (21, 0.0)]),
    "accelerating_down": tape([(379, -0.0015), (21, -0.006)]),
    "turning_up": tape([(274, -0.001), (63, -0.0045), (63, 0.0012)]),
    "slowing_down": tape([(379, -0.003), (21, 0.0)]),
    "steady_down": tape([(400, -0.002)]),
    "flat": flat_tape(),
}


def last_label(close):
    code = RP.labels(RP.phase_frame(close))[-1]
    return RP.LABEL_KEYS[code] if code >= 0 else None


# ==========================================================================
print("\n== 1. the nine phase labels (s4) ==")
check("nine labels declared, in the spec's first-match order", RP.LABEL_KEYS,
      ["accelerating_up", "rolling_over", "slowing_up", "steady_up", "accelerating_down",
       "turning_up", "slowing_down", "steady_down", "flat"])
for want, px in LABEL_TAPES.items():
    check(f"synthetic tape -> {want}", last_label(px), want)

f = RP.phase_frame(LABEL_TAPES["steady_up"])
check_true("up = t126 >= +2 (steady ramp)", f["t126"][-1] >= RP.T_TREND and f["trend"][-1] == 1)
f = RP.phase_frame(LABEL_TAPES["flat"])
check_true("flat = |t126| < 2 (symmetric cosine)", abs(f["t126"][-1]) < RP.T_TREND and f["trend"][-1] == 0,
           f"t={f['t126'][-1]:.3g}")
f = RP.phase_frame(LABEL_TAPES["steady_down"])
check_true("down = t126 <= -2", f["trend"][-1] == -1)

# paces and accelerations, by hand on the accelerating-up tape
px = LABEL_TAPES["accelerating_up"]
f = RP.phase_frame(px)
lp = np.log(px)
p21 = (lp[-1] - lp[-22]) * 252 / 21
p63 = (lp[-1] - lp[-64]) * 252 / 63
p126 = (lp[-1] - lp[-127]) * 252 / 126
sig = np.std(np.diff(lp)[-126:], ddof=1) * math.sqrt(252)
check_true("p21 / p63 / p126 are annualised log returns",
           abs(f["p21"][-1] - p21) < 1e-12 and abs(f["p63"][-1] - p63) < 1e-12
           and abs(f["p126"][-1] - p126) < 1e-12)
check_true("sigma = 126d annualised vol of daily log returns", abs(f["sigma"][-1] - sig) < 1e-12)
check_true("a1 = (p21-p63)/sigma, a2 = (p63-p126)/sigma",
           abs(f["a1"][-1] - (p21 - p63) / sig) < 1e-9 and abs(f["a2"][-1] - (p63 - p126) / sig) < 1e-9)

# t-stat by hand (plain OLS on the last 126 log prices)
x = np.arange(126, dtype=float)
y = lp[-126:]
b, a = np.polyfit(x, y, 1)
res = y - (a + b * x)
se = math.sqrt((res @ res) / 124 / ((x - x.mean()) @ (x - x.mean())))
check_true("t126 = plain OLS slope / its standard error", abs(f["t126"][-1] - b / se) < 1e-6 * abs(b / se),
           f"{f['t126'][-1]:.4f} vs {b / se:.4f}")
check_true("slope annualised x252", abs(f["slope"][-1] - b * 252) < 1e-9)

codes = RP.labels(RP.phase_frame(LABEL_TAPES["steady_up"][:RP.MIN_PHASE_BARS - 1]))
check_true(f"under {RP.MIN_PHASE_BARS} bars no label is computable", bool(np.all(codes < 0)))
codes = RP.labels(RP.phase_frame(LABEL_TAPES["steady_up"][:RP.MIN_PHASE_BARS]))
check_true(f"at {RP.MIN_PHASE_BARS} bars the last bar is labelled", codes[-1] >= 0)
cc = np.array([3, 3, 0, 0, 0])
check("run_start = first session of the current run", RP.run_start(cc, 4), 2)


# ==========================================================================
print("\n== 2. the direction-aware status table (s5) ==")
table = {  # (trend, code) -> status
    (1, 1): "confirms", (1, 0): "neutral", (1, -1): "diverges",
    (-1, 1): "diverges", (-1, 0): "neutral", (-1, -1): "confirms",
    (0, 1): "leans_up", (0, 0): "neutral", (0, -1): "leans_down",
}
for (tr, code), want in table.items():
    check(f"trend {RP.TREND_WORD[tr]:4s} s*m {'> +dz' if code > 0 else '< -dz' if code < 0 else 'in dz'}",
          RP.status_word(code, tr), want)
check("no trend -> n/a", RP.status_word(1, None), "n/a")
check("no measure -> n/a", RP.status_word(float("nan"), 1), "n/a")
num = {"confirms": 1.0, "diverges": -1.0, "neutral": 0.0, "leans_up": 2.0, "leans_down": -2.0}
arr = RP.status_word_array([c for (_, c) in table], [t for (t, _) in table])
check("status_word_array agrees with status_word on every cell",
      list(arr), [num[w] for w in table.values()])
check("expected sign s = -1 flips the code (a falling yield confirms)",
      list(RP.status_codes([-3.0, 3.0], [1.0, 1.0], -1)), [1.0, -1.0])


# ==========================================================================
print("\n== 3. dead zones in the measure's own volatility ==")
check("dead zone is 0.5 x the window vol", RP.CHECK_DZ, 0.5)
check("z = 0.49 is inside the dead zone, 0.51 is not",
      list(RP.status_codes([0.49, 0.51, -0.51, -0.49], [1.0] * 4, 1)), [0.0, 1.0, -1.0, 0.0])

rng = np.random.default_rng(5)
lx = np.cumsum(rng.normal(0.0004, 0.01, 500))
quiet = np.exp(lx)
loud = np.exp(3.0 * lx)                       # the same tape at 3x the volatility
mq, sq = RP.measure("driver", [quiet], 63)
ml, sl = RP.measure("driver", [loud], 63)
d = np.diff(lx)
check_true("sdw = std(daily log changes, trailing 252) x sqrt(window)",
           abs(sq[-1] - np.std(d[-252:], ddof=1) * math.sqrt(63)) < 1e-12)
check_true("m = the window's log change", abs(mq[-1] - (lx[-1] - lx[-64])) < 1e-12)
check_true("z is scale-free: the 3x tape has 3x the move and 3x the bar",
           abs(ml[-1] / sl[-1] - mq[-1] / sq[-1]) < 1e-9)
check("so every status is identical at either scale",
      list(np.nan_to_num(RP.status_codes(ml, sl, 1), nan=9)),
      list(np.nan_to_num(RP.status_codes(mq, sq, 1), nan=9)))
move = 0.6 * sq[-1]                          # clears the quiet series' bar ...
check("a fixed move clears the quiet series' bar", RP.status_codes([move], [sq[-1]], 1)[0], 1.0)
check("... and sits in the 3x series' dead zone", RP.status_codes([move], [sl[-1]], 1)[0], 0.0)

yld = 4.0 + np.cumsum(rng.normal(0, 0.03, 500))          # a yield in percent
mb, sb = RP.measure("driver", [yld], 63, unit="bp")
check_true("a yield's measure is a change in bp", abs(mb[-1] - (yld[-1] - yld[-64]) * 100) < 1e-9)
mc, scv = RP.measure("curve", [yld + 0.5, yld * 0 + 4.0], 63)
check_true("curve = change in bp of (long - short), z in its own vol",
           abs(mc[-1] - (yld[-1] - yld[-64]) * 100) < 1e-9 and abs(mc[-1] / scv[-1] - mb[-1] / sb[-1]) < 1e-9)
a_ = 20 + np.sin(np.arange(500) / 9.0)
b_ = 18 + np.cos(np.arange(500) / 7.0)
mlv, slv = RP.measure("level", [a_, b_], 63)
check_true("level = (A - B - threshold), bar = its own std over the window",
           abs(mlv[-1] - (a_[-1] - b_[-1])) < 1e-12
           and abs(slv[-1] - np.std((a_ - b_)[-63:], ddof=1)) < 1e-12)


# ==========================================================================
print("\n== 4. no look-ahead, with a positive control ==")


def states(close, drv, other):
    """Every trailing state the board and the calibration read."""
    f_ = RP.phase_frame(close)
    out = {"label": RP.labels(f_).astype(float)}
    tr = f_["trend"]
    for kind, xs in (("driver", [drv]), ("ratio", [close, other]), ("curve", [drv, other]),
                     ("level", [drv, other])):
        m_, s_ = RP.measure(kind, xs, 63)
        out[kind] = RP.status_word_array(RP.status_codes(m_, s_, 1), tr)
        out[kind + "_m"] = m_
    out["ext_pct"] = RP.ext_frame(close)[1]
    return out


rng = np.random.default_rng(11)
N = 1100
close0 = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.012, N)))
drv0 = 50 * np.exp(np.cumsum(rng.normal(0.0, 0.01, N)))
oth0 = 30 * np.exp(np.cumsum(rng.normal(0.0001, 0.011, N)))
D = 800
base = states(close0, drv0, oth0)
c1, d1, o1 = close0.copy(), drv0.copy(), oth0.copy()
c1[D + 1:] = c1[D] * np.exp(np.cumsum(rng.normal(-0.004, 0.03, N - D - 1)))
d1[D + 1:] = d1[D] * np.exp(np.cumsum(rng.normal(0.004, 0.03, N - D - 1)))
o1[D + 1:] = o1[D] * np.exp(np.cumsum(rng.normal(0.003, 0.02, N - D - 1)))
mut = states(c1, d1, o1)
for k in base:
    check_true(f"{k}: every state at <= d unchanged when every bar after d is replaced",
               np.array_equal(base[k][:D + 1], mut[k][:D + 1], equal_nan=True))
    check_true(f"{k}: positive control -- the states after d did change",
               not np.array_equal(base[k][D + 1:], mut[k][D + 1:], equal_nan=True))


def leaky(close):   # a centred moving average reads the future
    return pd.Series(close).rolling(21, center=True).mean().to_numpy()


check_true("the harness catches a leaky (centred) function",
           not np.array_equal(leaky(close0)[:D + 1], leaky(c1)[:D + 1], equal_nan=True))
same, changed = EV.lookahead_check(close0.copy(), [drv0.copy()], {"kind": "driver", "window": 63, "s": 1}, D)
check_true("rotation_evidence.lookahead_check (the run's own proof) passes on a synthetic tape",
           same and changed, f"same={same} changed={changed}")

# a basket built from members is trailing too
mem0 = {m: list(100 * np.exp(np.cumsum(rng.normal(0, 0.02, N)))) for m in ("A", "B", "C", "D")}
mem1 = {m: list(v) for m, v in mem0.items()}
for m in mem1:
    for i in range(D + 1, N):
        mem1[m][i] = mem1[m][i] * (1.5 if m == "A" else 0.7)
b0 = np.array(RG.basket_series(mem0, list(mem0)), float)
b1 = np.array(RG.basket_series(mem1, list(mem1)), float)
check_true("basket series at <= d unchanged by member bars after d",
           np.array_equal(b0[:D + 1], b1[:D + 1], equal_nan=True)
           and not np.array_equal(b0[D + 1:], b1[D + 1:], equal_nan=True))


# ==========================================================================
print("\n== 5. the s6 table ==")
prims = TH.primaries()
check("63 primaries in regime.REG_ETFS", len(prims), 63)
check("every primary has a THESES entry and nothing extra", sorted(TH.THESES), sorted(prims))
few = [p for p in prims if len([c for c in TH.checks_for(p) if c["kind"] != "extension"]) < 3]
check("every row carries >= 3 checks (plus extension)", few, [])
check("every row has extension", [p for p in prims if not any(c["kind"] == "extension" for c in TH.checks_for(p))], [])
fams = sorted({TH.THESES[p]["family"] for p in prims})
check("families are the s7 five", fams, sorted(TH.FAMILIES))
missing = [p for p in prims if not (TH.THESES[p].get("bet") and TH.THESES[p].get("topping")
                                    and TH.THESES[p].get("bottoming"))]
check("every row has bet, topping and bottoming text", missing, [])
tops = [TH.THESES[p]["topping"] for p in prims]
bots = [TH.THESES[p]["bottoming"] for p in prims]
check_true("topping lines are row-specific (all 63 distinct)", len(set(tops)) == 63)
check_true("bottoming lines are row-specific (all 63 distinct)", len(set(bots)) == 63)
dup = [p for p in prims if len({c["id"] for c in TH.checks_for(p)}) != len(TH.checks_for(p))]
check("check ids unique within a row", dup, [])
nophr = [(p, c["id"]) for p in prims for c in TH.checks_for(p)
         if c["kind"] in ("driver", "ratio", "curve", "level", "participation") and c["s"] != 0
         and not (c.get("up_div") and c.get("dn_div"))]
check("every signed check has its own topping-side and bottoming-side words", nophr, [])
bad_kind = [(p, c["id"]) for p in prims for c in TH.checks_for(p)
            if c["kind"] not in ("driver", "ratio", "curve", "level", "concentration", "split",
                                 "participation", "extension")]
check("every kind is one of the s5 eight", bad_kind, [])
bad_s = [(p, c["id"]) for p in prims for c in TH.checks_for(p) if c["s"] not in (-1, 0, 1)]
check("s is +1 / -1 / 0", bad_s, [])
bad_w = [(p, c["id"]) for p in prims for c in TH.checks_for(p)
         if c["kind"] != "extension" and c["window"] not in (21, 63, 126)]
check("windows are 21 / 63 / 126", bad_w, [])
nin = [(p, c["id"], len(c["inputs"])) for p in prims for c in TH.checks_for(p)
       if (c["kind"] == "driver" and len(c["inputs"]) != 1)
       or (c["kind"] in ("ratio", "curve", "level") and len(c["inputs"]) != 2)]
check("driver has 1 input; ratio / curve / level have 2", nin, [])
nog = [(p, c["id"]) for p in prims for c in TH.checks_for(p) if c["kind"] == "split"
       and (len(c.get("groups") or []) < 2 or not c.get("why"))]
check("every split names >= 2 groups and why it matters", nog, [])


def sig(p, cid):
    c = next((c for c in TH.checks_for(p) if c["id"] == cid), None)
    return None if c is None else (c["kind"], tuple(c["inputs"]), c["s"], c["window"])


spec_pins = [
    ("GDX", "gold", ("driver", ("GC=F",), 1, 63)),
    ("GDX", "lev", ("ratio", ("GDX", "GLD"), 1, 63)),
    ("GDX", "jr", ("ratio", ("GDXJ", "GDX"), 1, 63)),
    ("GDX", "rates", ("driver", ("^TNX",), -1, 63)),
    ("KRE", "front", ("driver", ("^IRX",), -1, 63)),
    ("KRE", "curve", ("curve", ("^TNX", "^IRX"), 1, 63)),
    ("OIH", "crude126", ("driver", ("CL=F",), 1, 126)),
    ("XOP", "crude21", ("driver", ("CL=F",), 1, 21)),
    ("SILJ", "gsr", ("ratio", ("GC=F", "SI=F"), -1, 63)),
    ("KIE", "rates", ("driver", ("^TNX",), 1, 63)),
    ("IYT", "oil", ("driver", ("CL=F",), -1, 63)),
    ("JETS", "fuel", ("driver", ("HO=F",), -1, 63)),
    ("SPY", "vixts", ("level", ("^VIX3M", "^VIX"), 1, 63)),
    ("HYPERSCALE", "orcl", ("ratio", ("ORCL", "MSFT"), 1, 63)),
    ("XBI", "appetite", ("ratio", ("XBI", "IBB"), 1, 63)),
    ("COPX", "lev", ("ratio", ("COPX", "HG=F"), 1, 63)),
    ("EWY", "won", ("driver", ("KRWUSD=X",), 1, 63)),
    ("BOTZ", "yen", ("driver", ("JPYUSD=X",), 1, 63)),
]
for p, cid, want in spec_pins:
    check(f"{p} {cid} is the spec's check", sig(p, cid), want)
check("URA's physical uranium is FX-converted from CAD",
      next(c for c in TH.checks_for("URA") if c["id"] == "spot").get("fx"), "CADUSD=X")
check("yield drivers are measured in bp",
      sorted({c["unit"] for p in prims for c in TH.checks_for(p)
              if c["kind"] == "driver" and c["inputs"][0] in TH.YIELD_SYMS}), ["bp"])
check("informational checks carry s = 0 (XLI GRID/XLI, XLK IGV/XLK, URA NLR/URA)",
      [sig("XLI", "whose")[2], sig("XLK", "sw")[2], sig("URA", "nlr")[2]], [0, 0, 0])
check("SPY carries no benchmark ratio against itself",
      [c["id"] for c in TH.checks_for("SPY") if c["id"] == "bench"], [])
check("a sub-industry's auto ratio is against its sector (KRE vs XLF)", sig("KRE", "bench"),
      ("ratio", ("KRE", "XLF"), 1, 63))

print("\n  -- twins --")
twins = [e["t"] for e in RG.REG_ETFS if TH.primary_of(e["t"]) != e["t"]]
check("17 twins", len(twins), 17)
unres = [t for t in [e["t"] for e in RG.REG_ETFS] if TH.primary_of(t) not in TH.THESES]
check("every row resolves to a primary in the table", unres, [])
grp = {e["t"]: e.get("grp") for e in RG.REG_ETFS}
check("a twin's primary is the first-listed member of its grp",
      [t for t in twins if grp[TH.primary_of(t)] != grp[t]], [])
check("spec s6's twin brackets", sorted((t, TH.primary_of(t)) for t in twins), sorted([
    ("XES", "OIH"), ("ICOP", "COPX"), ("GDXJ", "GDX"), ("RING", "GDX"), ("XAR", "ITA"),
    ("PPA", "ITA"), ("XTN", "IYT"), ("XHB", "ITB"), ("IBB", "XBI"), ("PJP", "XPH"),
    ("KCE", "IAI"), ("WCLD", "IGV"), ("HACK", "CIBR"), ("SOXX", "SMH"), ("XSD", "SMH"),
    ("TAN", "ICLN"), ("VNQ", "XLRE")]))
check("a twin inherits its primary's check list",
      [c["id"] for c in TH.checks_for("GDXJ")], [c["id"] for c in TH.checks_for("GDX")])


# ==========================================================================
print("\n== 6. the payload, built offline over a synthetic panel ==")
rng = np.random.default_rng(86)
NB = 400                      # the label tapes' length
dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(end="2026-09-25", periods=NB)]
row_ts = [e["t"] for e in RG.REG_ETFS]
need = set(TH.price_inputs())
for e in RG.REG_ETFS:
    need.update(e.get("basket") or [e["t"]])
for p in prims:
    for c in TH.checks_for(p):
        if c.get("fx"):
            need.add(c["fx"])
need -= set(TH.BASKETS)
shapes = list(LABEL_TAPES.values())
series = {}
for k, s in enumerate(sorted(need)):
    if s in row_ts:               # rows walk through the nine designed tapes
        px = shapes[row_ts.index(s) % 9][-NB:] * (1 + 0.1 * (k % 7))
    else:
        px = 20 * np.exp(np.cumsum(rng.normal(rng.uniform(-0.002, 0.002), 0.012, NB)))
    series[s] = [float(v) for v in px]
series["LATE"] = [None] * (NB - 50) + [10.0 + i * 0.1 for i in range(50)]


def members_fixture():
    rows = {}
    for t in row_ts:
        p = TH.primary_of(t)
        names = []
        for c in TH.checks_for(p):
            if c["kind"] == "concentration":
                names += c["inputs"]
            if c["kind"] == "split":
                for g in c["groups"]:
                    names += (g["members"] or [])[:2]
        names = list(dict.fromkeys(names)) + ["FILL1", "FILL2", "FILL3"]
        R = {k: float(rng.uniform(-0.25, 0.35)) for k in ("r5", "r21", "r63", "r126")}
        w = 0.8 / len(names)
        hold = []
        for nm in names:
            r = {k: float(rng.uniform(-0.3, 0.4)) for k in R}
            cc = {k: w * (1 + R[k]) / (1 + r[k]) * r[k] for k in R}
            hold.append({"t": nm, "name": nm, "w": w, "ccy": "USD", "r": r, "c": cc})
        part_ = {}
        for key in ("r21", "r63"):
            up = sum(h["w"] for h in hold if h["r"][key] > 0)
            part_[key] = {"share_up": up / sum(h["w"] for h in hold),
                          "n_up": sum(h["r"][key] > 0 for h in hold), "n": len(hold)}
            if len(rows) % 2:
                part_[key].update({"conc_top3": None, "conc_note": "n/a -- too small a move to attribute"})
            else:
                part_[key]["conc_top3"] = 1.7
        rows[t] = {"tier": "top10", "R": R, "holdings": hold, "participation": part_,
                   "rest": {"w": 0.2, "c": {k: R[k] - sum(h["c"][k] for h in hold) for k in R}}}
    return {"asof": dates[-1], "rows": rows}


def evidence_fixture():
    cell = lambda tag, n: {"tag": tag, "n_conf": n, "n_div": n, "med_conf": 0.011, "med_div": 0.001,
                           "diff": 0.01, "ci": [0.002, 0.02], "ci_time": [0.001, 0.03]}
    ch_all = {k: {tw: cell("edge" if k == "ratio" else "descriptive", 40) for tw in ("up", "down", "flat")}
              for k in ("driver", "ratio", "curve", "level", "extension")}
    ch_fam = {f_: {"driver": {tw: cell("edge", 5) for tw in ("up", "down", "flat")}} for f_ in TH.FAMILIES}
    ph = {lab: {"episodes": 120, "persist": 0.61, "med_rel63": 0.004, "days": 2000, "share_pos": 0.52}
          for lab in RP.LABEL_KEYS}
    ph_fam = {"commodity": {lab: {"episodes": 7, "persist": 0.5, "med_rel63": -0.01, "days": 90}
                            for lab in RP.LABEL_KEYS}}
    rows = {t: {"labels": {lab: {"episodes": 3, "days": 40, "med_rel63": 0.0, "med_abs63": 0.01,
                                 "persist": 0.5} for lab in RP.LABEL_KEYS},
                "ext_q": [round(-0.3 + 0.03 * i, 5) for i in range(21)], "ext_n": 756} for t in row_ts}
    return {"asof": "2026-09-24", "checks": {"all": ch_all, "by_family": ch_fam},
            "phase": {"all": ph, "by_family": ph_fam}, "rows": rows}


MEM = members_fixture()
EVF = evidence_fixture()
rows, errors = RP.build(dates, series, {}, MEM, None, EVF)
check("all 80 rows present, in REG_ETFS", list(rows), row_ts)
check("twins carry their primary", [(t, rows[t]["primary"]) for t in twins],
      [(t, TH.primary_of(t)) for t in twins])
check("twins carry checks_from and the inherited check ids",
      [t for t in twins if rows[t].get("checks_from") != TH.primary_of(t)
       or [c["id"] for c in rows[t]["checks"]] != [c["id"] for c in TH.checks_for(TH.primary_of(t))]], [])
check("primaries carry primary == t", [t for t in prims if rows[t]["primary"] != t], [])
labs = {}
for t in row_ts:
    ph_ = rows[t]["phase"]
    labs[ph_["label"] if ph_ else None] = labs.get(ph_["label"] if ph_ else None, 0) + 1
check("every one of the nine labels appears in the synthetic board", sorted(k for k in labs if k),
      sorted(RP.LABEL_KEYS))
keys10 = {"primary", "family", "bet", "phase", "checks", "read", "topping", "bottoming", "phase_evidence"}
check("every row has the s10 fields", [t for t in row_ts if not keys10 <= set(rows[t])], [])
pk = {"label", "word", "since", "t126", "p21", "p63", "p126", "a1", "a2", "above50"}
check("every phase has the s10 fields", [t for t in row_ts if rows[t]["phase"] and not pk <= set(rows[t]["phase"])], [])
ck = {"id", "kind", "label", "window", "s", "m", "z", "status", "value", "note", "evidence"}
allowed = {"confirms", "diverges", "neutral", "leans_up", "leans_down", "info", "n/a"}
badc = [(t, c["id"]) for t in row_ts for c in rows[t]["checks"]
        if not ck <= set(c) or c["status"] not in allowed
        or c["evidence"].get("tag") not in ("edge", "descriptive", "uncalibrated")]
check("every check has the s10 fields, a legal status and an evidence tag", badc, [])
hold_na = [(t, c["id"]) for t in row_ts for c in rows[t]["checks"]
           if c["kind"] in ("participation", "concentration", "split") and c["status"] == "n/a"]
check("participation / concentration / split read the members file (never n/a)", hold_na, [])
unc = [(t, c["id"]) for t in row_ts for c in rows[t]["checks"]
       if c["kind"] in ("participation", "concentration", "split") and c["evidence"]["tag"] != "uncalibrated"]
check("holdings checks are tagged uncalibrated", unc, [])
reads = [rows[t]["read"] for t in row_ts]
check_true("every read line opens '<phase> since <m/d> — '",
           all(r.startswith(rows[t]["phase"]["word"] + " since ") and " — " in r
               for t, r in zip(row_ts, reads) if rows[t]["phase"]))
check_true("some read lines carry a divergence and some 'nothing diverging'",
           any("nothing diverging" in r for r in reads) and
           any(r.count(";") >= 1 and "nothing diverging" not in r for r in reads))
try:
    json.dumps({"rows": rows, "errors": errors}, allow_nan=False)
    ok = True
except ValueError:
    ok = False
check_true("payload serialises with allow_nan=False (no NaN / Infinity)", ok)
inp_ = RP.Inputs(dates, series, {})
mism = []
for t in row_ts:
    ph_ = rows[t]["phase"]
    tr_ = {"up": 1, "down": -1, "flat": 0}[ph_["trend"]] if ph_ else None
    for c in TH.checks_for(t):
        if c["kind"] in ("driver", "ratio", "curve", "level") and c["s"] != 0:
            xs_, _ = RP.check_inputs(c, inp_)
            m_, s_ = RP.measure(c["kind"], xs_, c["window"], c.get("unit"), c.get("threshold", 0.0))
            want_ = RP.status_word(RP.status_codes(m_, s_, c["s"])[-1], tr_)
            got_ = next(r for r in rows[t]["checks"] if r["id"] == c["id"])["status"]
            if got_ != want_:
                mism.append((t, c["id"], got_, want_))
check("the board's status = the calibration's functions at the last bar (one implementation)", mism, [])
ext_side = []
for t in row_ts:
    ph_ = rows[t]["phase"]
    if not ph_ or ph_["trend"] == "flat":
        continue
    ec = next(c for c in rows[t]["checks"] if c["kind"] == "extension")
    if ec.get("pct") is None or ec["evidence"]["tag"] == "uncalibrated":   # SPY: the benchmark
        continue
    inz = ec["pct"] >= RP.EXT_HI if ph_["trend"] == "up" else ec["pct"] <= RP.EXT_LO
    zone = "stretched" if ph_["trend"] == "up" else "washed out"
    ok_ = (ec["evidence"].get("today") == (zone if inz else "not " + zone)
           and ec["evidence"].get("med_today") == (0.011 if inz else 0.001))
    if not ok_:
        ext_side.append((t, ec["pct"], ec["evidence"].get("today")))
check("an extension evidence tag says which side of its zone today sits on", ext_side, [])
spy_ev = [c["evidence"]["tag"] for c in rows["SPY"]["checks"] if c["kind"] in ("ratio", "level")]
check("SPY's own checks are uncalibrated (it is the benchmark)", sorted(set(spy_ev)), ["uncalibrated"])
e1 = RP.evidence_cell(EVF, "growth", "driver", 1)
check("an evidence cell under 20 episodes a side falls back to the all-families pool",
      (e1["tag"], e1["pool"]), ("descriptive", "driver, all families, up-trends"))
pe = RP.phase_evidence(EVF, "GDX", "commodity", "steady_up")
check("a family phase cell under 20 episodes falls back to all families",
      (pe["episodes"], pe["pool"], pe["row"]["episodes"]), (120, "all families", 3))

rows0, err0 = RP.build(dates, series, {}, None, None, None)
na0 = sorted({c["note"] for t in row_ts for c in rows0[t]["checks"]
              if c["kind"] in ("participation", "concentration", "split")})
check("without rotation_members.json the holdings checks say so", na0, ["rotation_members.json not present"])
check_true("without evidence every tag is uncalibrated",
           all(c["evidence"]["tag"] == "uncalibrated" for t in row_ts for c in rows0[t]["checks"]))
mem_two = {"asof": dates[-1], "rows": {k: v for k, v in MEM["rows"].items() if k != "KRE"}}
rows2, _ = RP.build(dates, series, {}, mem_two, None, EVF)
check("a row absent from a present members file says so (not 'file not present')",
      sorted({c["note"] for c in rows2["KRE"]["checks"] if c["kind"] == "participation"}),
      ["no holdings for KRE in rotation_members.json"])

print("\n  -- banned words --")
for w in ("buy", "Buying", "sell", "SELL-off", "should", "will", "likely", "target", "recommend"):
    check_true(f"the banned-word pattern catches {w!r}", bool(RP.BANNED.search(f"x {w} y")))
check_true("... and not 'willow' / 'likelihood'", not RP.BANNED.search("willow likelihood"))
cfg_text = []
for p in prims:
    th = TH.THESES[p]
    cfg_text += [th["bet"], th["topping"], th["bottoming"], th.get("caveat") or ""]
    for c in TH.checks_for(p):
        cfg_text += [c.get("label") or "", c.get("up_div") or "", c.get("dn_div") or "", c.get("why") or ""]
        cfg_text += [g["name"] for g in c.get("groups") or []]
hits = [s for s in cfg_text if RP.BANNED.search(s)]
check("no banned word in any configured string of any row", hits, [])
gen = RP.generated_strings(rows) + RP.generated_strings(rows0) + RP.generated_strings(rows2)
hits = [s for s in gen if RP.BANNED.search(s)]
check(f"no banned word in any generated string ({len(gen)} strings, all 80 rows)", hits, [])
# force every divergence phrase through the read line
forced = []
for t in row_ts:
    p = TH.primary_of(t)
    for tr in (1, -1):
        for c in TH.checks_for(p):
            if c["kind"] in ("driver", "ratio", "curve", "level", "participation") and c["s"] != 0:
                rec = {"id": c["id"], "kind": c["kind"], "status": "diverges", "value": "+1.0% over 63d",
                       "z": -1.0, "evidence": {"tag": "edge"}}
                ph_ = {"label": "steady_up" if tr > 0 else "steady_down",
                       "word": "Steady up" if tr > 0 else "Steady down",
                       "since": "2026-08-14", "trend": "up" if tr > 0 else "down"}
                forced.append(RP.read_line(t, ph_, [rec, dict(rec)]))
hits = [s for s in forced if RP.BANNED.search(s)]
check(f"no banned word when every row's every divergence is forced into the read ({len(forced)})", hits, [])

print("\n  -- the s8 read line --")
ph_ = {"label": "steady_up", "word": "Steady up", "since": "2026-08-14", "trend": "up"}
chs = [
    {"id": "gold", "kind": "driver", "status": "confirms", "value": "+8.1% over 63d", "z": 1.4},
    {"id": "lev", "kind": "ratio", "status": "diverges", "value": "−3.0% over 63d", "z": -1.2,
     "evidence": {"tag": "descriptive"}},
    {"id": "jr", "kind": "ratio", "status": "confirms", "value": "+2.0% over 63d", "z": 0.9},
    {"id": "ext", "kind": "extension", "status": "info", "value": "", "pct": 0.93,
     "pct_basis": "of its 3y range"},
]
check("GDX read line has the s8 shape", RP.read_line("GDX", ph_, chs),
      "Steady up since 8/14 — 2 of 3 checks confirm; leverage failing — miners lagging gold "
      "(GDX/GLD −3.0% over 63d); stretched: 93rd pct of its 3y range.")
ph_t = {"label": "turning_up", "word": "Turning up", "since": "2026-09-19", "trend": "down"}
chs_t = [{"id": "appetite", "kind": "ratio", "status": "diverges", "value": "+4.0% over 63d", "z": 1.5},
         {"id": "credit", "kind": "ratio", "status": "diverges", "value": "+1.0% over 63d", "z": 0.8},
         {"id": "rates", "kind": "driver", "status": "confirms", "value": "+20bp over 63d", "z": 0.7}]
check("XBI turning-up read line names the bottoming-side tell", RP.read_line("XBI", ph_t, chs_t),
      "Turning up since 9/19 — the 126d down-trend intact, 1 of 3 checks still confirm it; small "
      "biotech outperforming while the group falls (XBI/IBB +4.0% over 63d); also the financing "
      "window reopening — high yield firming.")
check("no phase -> the reason, not a guess", RP.read_line("DRAM", None, [], "122 sessions of history"),
      "Phase n/a — 122 sessions of history.")


# ==========================================================================
print("\n== 7. evidence statistics (s7.3) ==")
keys = np.array([1, 1, 2, 2, 2, 1, 1], float)
valid = np.array([1, 1, 1, 1, 0, 1, 1], bool)
check("episodes: consecutive same state = one; a gap breaks it",
      list(EV.run_ids(keys, valid)), [0, 0, 1, 1, -1, 2, 2])
v = np.array([5.0, 1.0, 3.0, 2.0, 4.0])
sv, g, k = EV._wmed_setup(v, np.arange(5))
check("weighted median with unit weights = the median", EV._wmed(sv, g, np.ones(k)), 3.0)
check("weighted median follows the resampled counts", EV._wmed(sv, g, np.array([0, 0, 0, 3, 0])), 2.0)
ids, nxt = EV._offset(np.array([-1, 0, 0, 1]), 10)
check("episode ids are made globally unique across rows", (list(ids), nxt), ([-1, 10, 10, 11], 12))


def groups(center, n_ep, per=8, seed=0):
    r_ = np.random.default_rng(seed)
    vals = np.concatenate([center + r_.normal(0, 0.01, per) + r_.normal(0, 0.01) for _ in range(n_ep)])
    eps = np.repeat(np.arange(n_ep), per) + seed * 1000
    blocks = np.repeat(r_.integers(0, 60, n_ep), per)
    return vals, eps, blocks


EV.REPS = 300
v1, e1_, b1 = groups(+0.04, 30, seed=1)
v2, e2_, b2 = groups(-0.04, 30, seed=2)
rg = np.random.default_rng(0)
check("separated, >= 20 episodes a side, thesis direction -> edge",
      EV.check_cell(v1, e1_, b1, v2, e2_, b2, +1, rg)["tag"], "edge")
cr = EV.check_cell(v1, e1_, b1, v2, e2_, b2, -1, rg)
check("the same split against the thesis -> descriptive, flagged reversed",
      (cr["tag"], cr.get("reversed")), ("descriptive", True))
v3, e3, b3 = groups(+0.04, 12, seed=3)
check("separated but under 20 episodes a side -> descriptive",
      EV.check_cell(v3, e3, b3, v2, e2_, b2, +1, rg)["tag"], "descriptive")
v4, e4, b4 = groups(0.0, 30, seed=4)
v5, e5, b5 = groups(0.001, 30, seed=5)
check("overlapping -> descriptive", EV.check_cell(v4, e4, b4, v5, e5, b5, +1, rg)["tag"], "descriptive")
cc_ = EV.check_cell(v1, e1_, b1, v2, e2_, b2, +1, rg)
check("n counts episodes, not days", (cc_["n_conf"], cc_["days_conf"]), (30, 240))
lc = EV.label_cell(np.array([0.01, 0.02, -0.01, 0.03]), np.array([0.0] * 4),
                   np.array([0.05, -0.02, 0.01, 0.04]), np.array([1.0, 1, 1, 1]),
                   np.array([7, 7, 8, 8]), np.array([0, 0, 0, 0]), rg, boot=False)
check("label cell: days, episodes, share positive, persistence",
      (lc["days"], lc["episodes"], lc["share_pos"], lc["persist"]), (4, 2, 0.75, 0.75))


# ==========================================================================
print("\n" + "-" * 60)
print(f"{len(PASSED)} passed, {len(FAILED)} failed")
if FAILED:
    for n_ in FAILED:
        print(f"  FAILED: {n_}")
    sys.exit(1)
