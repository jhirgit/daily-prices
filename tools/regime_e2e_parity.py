#!/usr/bin/env python3
"""End-to-end CONFIG parity check for the regime port (SPEC-8).

The golden-fixture test in test_technicals.py proves the ported FUNCTIONS match
the JS oracle. This checks the layer above that: the EMITTER config -- leg order,
ratio/trend pair selection, the breadth-pool filter, the ladder ETF set and the
receipts pool -- by running the EXACT client-side assembly (regime_core.js via
tools/regime_emit_oracle.js) on the same real-data SPY-axis panel that
regime.build_regime uses, then diffing the two `regime` blocks.

Requires Node and prices.db (unlike the pure-Python fixture test), so it is a
standalone on-demand check rather than part of test_technicals.py. Run:

    python tools/regime_e2e_parity.py

Exits 0 on exact parity (int votes/states/tiers/streaks exact; floats abs<1e-9).
"""
import json
import os
import sqlite3
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # dp8/
sys.path.insert(0, HERE)
import regime as RG          # noqa: E402
import technicals as T       # noqa: E402

PAR = os.path.join(HERE, "parity")
TOL = 1e-9


def diff(exp, act, path=""):
    if exp is None:
        return [] if act is None else [f"{path}: expected null, got {act!r}"]
    if isinstance(exp, bool) or isinstance(act, bool):
        return [] if exp == act else [f"{path}: bool {exp!r} != {act!r}"]
    if isinstance(exp, str):
        return [] if exp == act else [f"{path}: str {exp!r} != {act!r}"]
    if isinstance(exp, (int, float)):
        if not isinstance(act, (int, float)):
            return [f"{path}: number {exp!r} vs {act!r}"]
        return [] if abs(exp - act) <= TOL else [f"{path}: {exp!r} != {act!r}"]
    if isinstance(exp, list):
        if not isinstance(act, list) or len(exp) != len(act):
            return [f"{path}: list shape mismatch"]
        out = []
        for i, (e, a) in enumerate(zip(exp, act)):
            out += diff(e, a, f"{path}[{i}]")
        return out
    if isinstance(exp, dict):
        if not isinstance(act, dict):
            return [f"{path}: dict vs {type(act).__name__}"]
        out = []
        if set(exp) != set(act):
            out.append(f"{path}: keys differ")
        for k in set(exp) & set(act):
            out += diff(exp[k], act[k], f"{path}.{k}")
        return out
    return [f"{path}: unhandled {type(exp).__name__}"]


def main():
    os.makedirs(PAR, exist_ok=True)
    conn = sqlite3.connect(os.path.join(HERE, "prices.db"))
    syms = [r[0] for r in conn.execute(
        "SELECT DISTINCT ticker FROM daily_prices ORDER BY ticker")]
    emitted = sorted(s for s in syms if len(T.load_bars(conn, s)) >= T.MIN_BARS)
    dates, series = RG.build_panel(conn)

    panel_path = os.path.join(PAR, "panel.json")
    oracle_path = os.path.join(PAR, "oracle_regime.json")
    with open(panel_path, "w", encoding="utf-8") as fh:
        json.dump({"dates": dates, "series": series, "emitted": emitted,
                   "book_universe": RG.BOOK_UNION}, fh)

    subprocess.run(["node", os.path.join(HERE, "tools", "regime_emit_oracle.js"),
                    panel_path, oracle_path], check=True)

    mine = RG.build_regime(conn, set(emitted), panel=(dates, series), round_floats=False)
    conn.close()
    with open(oracle_path, encoding="utf-8") as fh:
        oracle = json.load(fh)

    # transient real-data dumps; regenerated each run
    for p in (panel_path, oracle_path):
        try:
            os.remove(p)
        except OSError:
            pass

    mism = diff(oracle, mine, "regime")
    print("=" * 60)
    if mism:
        print(f"E2E CONFIG PARITY FAILED: {len(mism)} mismatches")
        for m in mism[:20]:
            print("  ", m)
        return 1
    print("E2E CONFIG PARITY: exact match on the real-data panel")
    print(f"  dates={len(mine['dates'])}  legs={len(mine['legs'])}  "
          f"book={mine['breadth']['pool_size']}  receipts={len(mine['receipts'])}  "
          f"ladder_rows={len(mine['ladder']['rows'])}  flips={len(mine['flips'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
