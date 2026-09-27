#!/usr/bin/env python3
"""
health.py (review M1): each silent failure path reads as DEGRADED, offline.

A temp data dir of minimal payloads that the real emitters would write, healthy
first (a positive control: status ok, nothing listed), then one fault at a time.

Run:  python test_health.py
"""

import json
import os
import shutil
import sys
import tempfile

import health as H

FAILS = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}" + ("" if ok else f", want {want!r}"))
    if not ok:
        FAILS.append(name)


S = "2026-09-25"


def healthy():
    tick = {t: {"asof": S} for t in ("SPY", "NVDA", "MU")}
    return {
        "latest.json": {"generated_at": S + "T22:06:00Z", "session": S, "lagging": [],
                        "tickers": [{"ticker": t, "date": S} for t in ("SPY", "NVDA", "MU")]},
        "technicals.json": {"generated_at": S + "T22:06:00Z", "tickers": tick, "regime": {"asof": S}},
        "closes.json": {"generated_at": S + "T22:06:00Z", "asof": S, "tickers": tick},
        "crowd.json": {"generated_at": S + "T22:06:00Z", "tickers": tick},
        "rotation_members.json": {"generated_at": S + "T22:20:00Z", "asof": S, "rows": {}, "errors": {}},
        "rotation_phase.json": {"generated_at": S + "T22:21:00Z", "asof": S, "rows": {}, "errors": {}},
        "options_flow.json": {"generated_at": S + "T22:09:00Z", "as_of": S, "names": {"NVDA": {}}},
        "earnings_dates.json": {"generated_at": S + "T22:07:00Z", "dates": {}, "errors": {}},
        "earnings_detected.json": {"generated_at": S + "T22:07:00Z", "names": {},
                                   "counts": {"errors": 0, "overdue": 0}},
        "overnight.json": {"generated_at": S + "T11:31:00Z",
                           "groups": [{"group": "Asia", "items": [{"ticker": "^N225", "last": 1.0}]}]},
        "news.json": {"generated_at": S + "T11:31:00Z", "names": {"NVDA": []}, "empty": ["MU"],
                      "unavailable": []},
        "intraday.json": {"as_of": S + "T19:45:00Z", "quotes": [], "errors": []},
        "insiders_12m.json": {"generated_at": "2026-09-19T16:10:11Z", "tickers": {"NVDA": {"status": "ok"}}},
    }


def run(payloads, steps="", drop=(), raw=None):
    d = tempfile.mkdtemp(prefix="health_")
    for name, j in payloads.items():
        if name in drop:
            continue
        with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(j) if raw is None or name not in raw else raw[name])
    tk = os.path.join(d, "tickers.txt")
    with open(tk, "w", encoding="utf-8") as fh:
        fh.write("SPY\nNVDA\nMU\n")
    try:
        return H.build(d, tk, steps_env=steps)
    finally:
        shutil.rmtree(d, ignore_errors=True)


print("# positive control: a healthy run")
h = run(healthy())
check("status ok", h["status"], "ok")
check("nothing degraded", h["degraded"], [])
check("session is latest.json's", h["session"], S)
check("every payload reported", sorted(h["payloads"]), sorted(healthy()))
check("rows counted", h["payloads"]["latest.json"]["rows"], 3)

print("\n# an EOD payload that re-exported an old session")
p = healthy(); p["closes.json"]["asof"] = "2026-09-24"
check("closes behind the session is degraded", run(p)["degraded"],
      ["closes.json speaks for 2026-09-24, behind the 2026-09-25 session"])

print("\n# the regime layer failed (technicals.py swallows it into regime.error)")
p = healthy(); p["technicals.json"]["regime"] = {"error": "KeyError: 'XLE' token=abc123 /some/path"}
h = run(p)
check("degraded names the failure by type", [x for x in h["degraded"] if "regime" in x],
      ["technicals.json: the regime layer failed (KeyError); ladders render nothing"])
check("the exception text never reaches the public file",
      any(s in json.dumps(h) for s in ("abc123", "/some/path", "'XLE'")), False)

print("\n# a payload missing or unreadable")
h = run(healthy(), drop=("rotation_phase.json",))
check("missing is degraded", h["degraded"], ["rotation_phase.json missing"])
h = run(healthy(), raw={"crowd.json": "{not json"})
check("unreadable is degraded", h["degraded"], ["crowd.json unreadable (JSONDecodeError)"])

print("\n# a partial price outage: covered names trail the session")
p = healthy()
p["latest.json"]["lagging"] = [{"ticker": "MU", "date": "2026-09-23"}, {"ticker": "PBS", "date": "2026-07-17"}]
h = run(p)
check("only tickers.txt names are counted (PBS is not covered)",
      h["payloads"]["latest.json"]["errors"], {"lagging": ["MU"]})
check("1 of 3 is over the 5% line", h["degraded"],
      ["latest.json: 1 of 3 covered names trail the 2026-09-25 session"])

print("\n# EDGAR detection errors and overdue prints")
p = healthy(); p["earnings_detected.json"]["counts"] = {"errors": 2, "overdue": 1}
check("both are degraded", run(p)["degraded"],
      ["earnings_detected.json: 2 EDGAR error(s)", "earnings_detected.json: 1 print(s) overdue"])

print("\n# the news key expired: every name unavailable")
p = healthy(); p["news.json"].update(names={}, empty=[], unavailable=[{"ticker": "NVDA"}, {"ticker": "MU"}])
check("degraded", run(p)["degraded"], ["news.json: 2 of 2 names unavailable (key or feed down?)"])

print("\n# continue-on-error steps report through STEP_OUTCOMES")
h = run(healthy(), steps="rotation_members=success rotation_phase=failure junk =x")
check("outcomes parsed, junk ignored", h["steps"],
      {"rotation_members": "success", "rotation_phase": "failure"})
check("a failed step is degraded", h["degraded"],
      ["step rotation_phase failed (continue-on-error; its payload is yesterday's)"])

print("\n# options payload near its hard cap: a warning, not a failure")
p = healthy(); p["options_flow.json"]["pad"] = "x" * int(0.95 * H.options_size_fail())
h = run(p)
check("status stays ok", h["status"], "ok")
check("one warning about the cap", len(h["warnings"]) == 1 and "cap" in h["warnings"][0], True)

print("\n# main(): writes the file; --strict exits 1 only when degraded")
d = tempfile.mkdtemp(prefix="health_main_")
for name, j in healthy().items():
    with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
        json.dump(j, fh)
tk = os.path.join(d, "t.txt")
with open(tk, "w", encoding="utf-8") as fh:
    fh.write("SPY\nNVDA\nMU\n")
out = os.path.join(d, "health.json")
check("healthy --strict exits 0", H.main(["--data", d, "--tickers", tk, "--out", out, "--strict"]), 0)
check("the file was written", json.load(open(out, encoding="utf-8"))["status"], "ok")
os.remove(os.path.join(d, "closes.json"))
check("degraded --strict exits 1", H.main(["--data", d, "--tickers", tk, "--out", out, "--strict"]), 1)
check("degraded without --strict still exits 0", H.main(["--data", d, "--tickers", tk, "--out", out]), 0)
shutil.rmtree(d, ignore_errors=True)

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s): {', '.join(FAILS)}")
    sys.exit(1)
print("all tests passed")
