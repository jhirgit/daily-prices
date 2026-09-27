#!/usr/bin/env python3
"""health.py -- one public-safe line per payload: there, fresh, full, errored (review M1).

Five failure paths shipped a fresh-stamped payload with no alert (review M1,
2026-09-26): a swallowed regime-layer exception, the continue-on-error rotation
steps, EDGAR errors that exit 0, per-name price lag, and a news feed whose key
expired. Each one reads FRESH on the board because its generated_at is fresh.

This reads what the run just wrote and emits data/health.json:

    session     SPY's last bar (latest.json), the session every EOD payload
                should speak for
    status      "ok" or "degraded"; `degraded` lists why, one line each
    warnings    worth a look, not a failure (options payload near its cap)
    steps       the outcomes of continue-on-error workflow steps, from the
                STEP_OUTCOMES env ("rotation_members=success rotation_phase=failure")
    payloads    per file: present, bytes, generated_at, asof, rows, errors

Degraded when: an EOD payload is missing or unreadable; its asof trails the
session; the regime layer failed; more than LAG_SHARE of the tickers.txt names
in latest.json trail the session; EDGAR detection errored or a print is
overdue; more than NEWS_DEAD_SHARE of news names are unavailable (an expired
key); a continue-on-error step failed.

PUBLIC REPO: counts, dates and ticker symbols (already in tickers.txt) only.
Exception text is reduced to its type (review L7). Never exits non-zero unless
--strict, so it can never cost the price commit.

    python health.py              # write data/health.json
    python health.py --strict     # ...and exit 1 when degraded
"""
import argparse
import datetime as dt
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
TICKERS = os.path.join(HERE, "tickers.txt")

LAG_SHARE = 0.05          # resilience F3: >5% of names behind = a partial fetch outage
NEWS_DEAD_SHARE = 0.5     # most names unavailable = the key or the feed is gone
OPTIONS_WARN_SHARE = 0.9  # review M2: warn at 90% of options_flow's hard cap

# EOD payloads: (file, how its session is read). Written by the 22:00 job and
# its siblings, so each should speak for latest.json's session.
EOD = [
    ("latest.json", lambda j: j.get("session")),
    ("technicals.json", lambda j: (j.get("regime") or {}).get("asof")),
    ("closes.json", lambda j: j.get("asof")),
    ("crowd.json", lambda j: max((r.get("asof") or "" for r in (j.get("tickers") or {}).values()),
                                 default=None) or None),
    ("rotation_members.json", lambda j: j.get("asof")),
    ("rotation_phase.json", lambda j: j.get("asof")),
    ("options_flow.json", lambda j: j.get("as_of")),
    ("earnings_dates.json", lambda j: (j.get("generated_at") or "")[:10] or None),
    ("earnings_detected.json", lambda j: (j.get("generated_at") or "")[:10] or None),
]
# Other payloads the board reads, on their own cadence: reported, not judged
# against the session.
OTHER = ["overnight.json", "news.json", "intraday.json", "insiders_12m.json"]


def _rows(j):
    for k in ("tickers", "names", "rows", "quotes", "dates", "groups"):
        v = j.get(k)
        if isinstance(v, (list, dict)):
            if k == "groups":
                return sum(len(g.get("items") or []) for g in v)
            return len(v)
    return None


def _err_type(text):
    """'KeyError: something' -> 'KeyError'. Never repeats exception text."""
    m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_.]*)", str(text or ""))
    return m.group(1) if m else "error"


def load_tickers(path=TICKERS):
    out = set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            t = line.split("#", 1)[0].strip().upper()
            if t:
                out.add(t)
    return out


def parse_steps(s):
    """'a=success b=failure' -> {'a': 'success', 'b': 'failure'}; junk is ignored."""
    out = {}
    for part in (s or "").split():
        k, sep, v = part.partition("=")
        if sep and k and v:
            out[k] = v
    return out


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            j = json.load(fh)
        return (j, None) if isinstance(j, dict) else (None, "not a JSON object")
    except FileNotFoundError:
        return None, "missing"
    except Exception as e:  # noqa: BLE001
        return None, "unreadable (%s)" % type(e).__name__


def options_size_fail():
    try:
        import options_flow
        return options_flow.SIZE_FAIL
    except Exception:  # noqa: BLE001
        return 300 * 1024


def build(data=DATA, tickers_path=TICKERS, steps_env=None, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    degraded, warnings, payloads = [], [], {}
    docs = {}
    for name in [f for f, _ in EOD] + OTHER:
        path = os.path.join(data, name)
        j, why = _read(path)
        docs[name] = j
        rec = {"present": j is not None}
        if j is None:
            rec["problem"] = why
        else:
            rec["bytes"] = os.path.getsize(path)
            rec["generated_at"] = j.get("generated_at") or j.get("as_of")
            rec["rows"] = _rows(j)
        payloads[name] = rec

    latest = docs.get("latest.json") or {}
    session = latest.get("session")
    for name, asof_of in EOD:
        j, rec = docs[name], payloads[name]
        if j is None:
            degraded.append("%s %s" % (name, rec["problem"]))
            continue
        try:
            rec["asof"] = asof_of(j)
        except Exception:  # noqa: BLE001
            rec["asof"] = None
        if session and rec["asof"] and rec["asof"] < session:
            degraded.append("%s speaks for %s, behind the %s session" % (name, rec["asof"], session))

    # latest.json: names in tickers.txt whose own bar trails the session.
    if latest:
        try:
            covered = load_tickers(tickers_path)
        except OSError:
            covered = set()
        lag = [r["ticker"] for r in latest.get("lagging") or [] if r.get("ticker") in covered]
        n = sum(1 for r in latest.get("tickers") or [] if r.get("ticker") in covered)
        payloads["latest.json"]["errors"] = {"lagging": lag}
        if n and len(lag) / n > LAG_SHARE:
            degraded.append("latest.json: %d of %d covered names trail the %s session"
                            % (len(lag), n, session))

    tech = docs.get("technicals.json") or {}
    if "error" in (tech.get("regime") or {}):
        et = _err_type(tech["regime"]["error"])
        payloads["technicals.json"]["errors"] = {"regime": et}
        degraded.append("technicals.json: the regime layer failed (%s); ladders render nothing" % et)

    for name in ("rotation_members.json", "rotation_phase.json"):
        j = docs.get(name) or {}
        if j.get("errors"):
            payloads[name]["errors"] = {"rows": sorted(j["errors"])}

    det = docs.get("earnings_detected.json") or {}
    c = det.get("counts") or {}
    if det:
        payloads["earnings_detected.json"]["errors"] = {"edgar": c.get("errors", 0),
                                                        "overdue": c.get("overdue", 0)}
        if c.get("errors"):
            degraded.append("earnings_detected.json: %d EDGAR error(s)" % c["errors"])
        if c.get("overdue"):
            degraded.append("earnings_detected.json: %d print(s) overdue" % c["overdue"])
    dates = docs.get("earnings_dates.json") or {}
    if dates.get("errors"):
        payloads["earnings_dates.json"]["errors"] = {"names": sorted(dates["errors"])}

    news = docs.get("news.json") or {}
    if news:
        un = news.get("unavailable") or []
        tot = len(news.get("names") or {}) + len(news.get("empty") or []) + len(un)
        payloads["news.json"]["errors"] = {"unavailable": len(un), "of": tot}
        if tot and len(un) / tot > NEWS_DEAD_SHARE:
            degraded.append("news.json: %d of %d names unavailable (key or feed down?)" % (len(un), tot))

    ovn = docs.get("overnight.json") or {}
    if ovn:
        dead = [i.get("ticker") for g in ovn.get("groups") or [] for i in g.get("items") or []
                if i.get("last") is None]
        payloads["overnight.json"]["errors"] = {"no_quote": dead}

    intr = docs.get("intraday.json") or {}
    if intr.get("errors"):
        payloads["intraday.json"]["errors"] = {"count": len(intr["errors"])}

    ins = docs.get("insiders_12m.json") or {}
    if ins:
        bad = sorted(t for t, v in (ins.get("tickers") or {}).items() if (v or {}).get("status") == "error")
        payloads["insiders_12m.json"]["errors"] = {"fetch_error": bad}

    opt = payloads["options_flow.json"]
    if opt.get("bytes"):
        cap = options_size_fail()
        opt["cap_share"] = round(opt["bytes"] / cap, 3)
        if opt["bytes"] >= OPTIONS_WARN_SHARE * cap:
            per = opt["bytes"] / max(opt.get("rows") or 1, 1)
            warnings.append("options_flow.json is %.0f%% of its %d KB cap (~%d names of headroom)"
                            % (100 * opt["bytes"] / cap, cap // 1024, max(0, int((cap - opt["bytes"]) / per))))

    steps = parse_steps(steps_env if steps_env is not None else os.environ.get("STEP_OUTCOMES"))
    for k, v in sorted(steps.items()):
        if v == "failure":
            degraded.append("step %s failed (continue-on-error; its payload is yesterday's)" % k)

    return {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session": session,
        "status": "degraded" if degraded else "ok",
        "degraded": degraded,
        "warnings": warnings,
        "steps": steps,
        "note": ("Public-safe pipeline health (review M1): counts, dates and ticker symbols "
                 "only. `asof` is the session a payload speaks for; EOD payloads should equal "
                 "`session`. Degraded = something shipped fresh-stamped but short or stale."),
        "payloads": payloads,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Write data/health.json.")
    ap.add_argument("--data", default=DATA)
    ap.add_argument("--tickers", default=TICKERS)
    ap.add_argument("--out", default=os.path.join(DATA, "health.json"))
    ap.add_argument("--strict", action="store_true", help="exit 1 when degraded")
    a = ap.parse_args(argv)
    doc = build(a.data, a.tickers)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
        fh.write("\n")
    print("health: %s (session %s) -> %s" % (doc["status"].upper(), doc["session"], a.out))
    for line in doc["degraded"]:
        print("  DEGRADED  " + line)
    for line in doc["warnings"]:
        print("  warn      " + line)
    return 1 if (a.strict and doc["degraded"]) else 0


if __name__ == "__main__":
    sys.exit(main())
