#!/usr/bin/env python3
"""
Tests for news.py.

No network: every case drives `build()` with a fake fetcher, so what is under test is
the part that can be wrong quietly -- the 36h window, the url dedupe, newest-first
ordering, the empty/unavailable split, and the rule that indices, futures and crypto
pairs are never requested at all.

Run:  python test_news.py
"""

import datetime as dt
import sys

import news as N

FAILS = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}" +
          ("" if ok else f", want {want!r}"))
    if not ok:
        FAILS.append(name)


def check_true(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{(' -- ' + detail) if detail else ''}")
    if not cond:
        FAILS.append(name)


NOW = dt.datetime(2026, 9, 11, 11, 40, 0, tzinfo=dt.timezone.utc)
NOW_TS = int(NOW.timestamp())
H = 3600


def item(hours_ago, url, headline="h", source="Reuters", summary="s"):
    return {"datetime": NOW_TS - int(hours_ago * H), "url": url,
            "headline": headline, "source": source, "summary": summary}


class Fake:
    """A fetcher with a canned reply per symbol; raises what it is told to raise."""

    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def __call__(self, symbol, frm, to):
        self.calls.append((symbol, frm, to))
        r = self.replies.get(symbol, [])
        if isinstance(r, Exception):
            raise r
        if callable(r):
            return r(len([c for c in self.calls if c[0] == symbol]))
        return r


# --------------------------------------------------------------------------
# select(): window, dedupe, order, cap
# --------------------------------------------------------------------------
print("select()")

_win = N.select([item(1, "a"), item(35.9, "b"), item(36.1, "c"), item(400, "d")], NOW_TS)
check("keeps only the last 36h", [i["url"] for i in _win], ["a", "b"])

_ord = N.select([item(10, "old"), item(1, "new"), item(5, "mid")], NOW_TS)
check("newest first", [i["url"] for i in _ord], ["new", "mid", "old"])

_dup = N.select([item(9, "same", headline="syndicated, stale"),
                 item(2, "same", headline="syndicated, fresh"),
                 item(3, "other")], NOW_TS)
check("dedupes on url", [i["url"] for i in _dup], ["same", "other"])
check("dedupe keeps the newest copy", _dup[0]["headline"], "syndicated, fresh")

_cap = N.select([item(i * 0.1, "u%d" % i) for i in range(40)], NOW_TS, cap=25)
check("caps per name", len(_cap), 25)
check("the cap keeps the newest, not the first", _cap[0]["url"], "u0")

_junk = N.select([item(1, ""), {"datetime": 0, "url": "z"}, {"url": "y"},
                  {"datetime": "bad", "url": "x"}, "not a dict", item(1, "ok")], NOW_TS)
check("drops items with no url or no usable timestamp",
      [i["url"] for i in _junk], ["ok"])

_fields = N.select([item(2, "https://x/1", headline=" Guidance cut ", source=" Reuters ",
                         summary=" body ")], NOW_TS)[0]
check("timestamp rendered as ISO Z", _fields["t"], N.iso(NOW_TS - 2 * H))
check("fields trimmed", (_fields["headline"], _fields["source"], _fields["summary"]),
      ("Guidance cut", "Reuters", "body"))
check("field set is exactly the missive's", sorted(_fields),
      ["headline", "source", "summary", "t", "url"])

# --------------------------------------------------------------------------
# skip rule: symbols Finnhub cannot serve are never requested
# --------------------------------------------------------------------------
print("\nskip rule")

for _sym in ("^GSPC", "^VIX", "GC=F", "BZ=F", "BTC-USD", "ETH-USD"):
    check_true(f"{_sym} is skipped", N.skip_reason(_sym) is not None)
for _sym in ("NVDA", "BRK-B", "000660.KS", "6981.T", "IFNNY", "VOLV-B.ST"):
    check_true(f"{_sym} is attempted", N.skip_reason(_sym) is None)

_f = Fake({"NVDA": [item(1, "n1")]})
_p = N.build(["^GSPC", "NVDA", "GC=F", "BTC-USD"], _f, now=NOW)
check("skipped symbols cost no call", [c[0] for c in _f.calls], ["NVDA"])
check("skipped symbols are unavailable with a reason",
      [u["ticker"] for u in _p["unavailable"]], ["^GSPC", "GC=F", "BTC-USD"])
check_true("the reason says why, not just that",
           all(u["why"] for u in _p["unavailable"]),
           str(_p["unavailable"]))

# --------------------------------------------------------------------------
# build(): the empty / unavailable split, and the payload shape
# --------------------------------------------------------------------------
print("\nbuild()")

_f = Fake({
    "NVDA": [item(1, "n1"), item(30, "n2"), item(99, "stale")],
    "AMD": [],                                   # served, nothing in window
    "ASML": [item(99, "stale")],                 # served, all of it too old
    "000660.KS": N.NewsError("HTTP 403"),        # the API cannot serve it
    "MU": N.NewsError("URLError: timed out"),    # a transient failure
})
_p = N.build(["NVDA", "AMD", "ASML", "000660.KS", "MU", "^VIX"], _f, now=NOW)

check("names carries only names WITH news", sorted(_p["names"]), ["NVDA"])
check("names are not given empty arrays", [n for n, v in _p["names"].items() if not v], [])
check("served-with-nothing goes to empty", _p["empty"], ["AMD", "ASML"])
check("could-not-serve goes to unavailable",
      [(u["ticker"], u["why"]) for u in _p["unavailable"]],
      [("000660.KS", "HTTP 403"), ("MU", "URLError: timed out"),
       ("^VIX", "index, not a company symbol")])
check("count is items, not names", _p["count"], 2)
check("window and source are stated", (_p["window_h"], _p["source"]),
      (36, "finnhub company-news"))
check("generated_at is ISO Z", _p["generated_at"], "2026-09-11T11:40:00Z")
check("payload keys", sorted(_p),
      ["count", "empty", "generated_at", "names", "source", "unavailable", "window_h"])
check_true("a per-symbol failure does not abort the run",
           "NVDA" in _p["names"] and len(_f.calls) == 5)

check("asks Finnhub for D-2..D", N.window_dates(NOW), ("2026-09-09", "2026-09-11"))
check("every call uses that window", sorted({c[1:] for c in _f.calls}),
      [("2026-09-09", "2026-09-11")])

# --------------------------------------------------------------------------
# rate limiting: pacing between calls, one retry on 429
# --------------------------------------------------------------------------
print("\nrate limiting")

_slept = []
_f = Fake({"NVDA": [item(1, "n1")], "AMD": [item(1, "a1")], "MU": [item(1, "m1")]})
N.build(["NVDA", "AMD", "MU"], _f, now=NOW, sleep_s=1.05, sleep=_slept.append)
check("paces between calls, not before the first", _slept, [1.05, 1.05])

_slept = []
_f = Fake({"NVDA": lambda n: (_ for _ in ()).throw(N.NewsError("HTTP 429")) if n == 1
                             else [item(1, "n1")]})
_p = N.build(["NVDA"], _f, now=NOW, sleep=_slept.append)
check("429 is retried once", len(_f.calls), 2)
check("after waiting out the minute bucket", _slept, [N.RETRY_AFTER_S])
check("and the retry's items are kept", list(_p["names"]), ["NVDA"])

_slept = []
_f = Fake({"NVDA": N.NewsError("HTTP 429")})
_p = N.build(["NVDA"], _f, now=NOW, sleep=_slept.append)
check("a second 429 is recorded, not retried again", len(_f.calls), 2)
check("and lands in unavailable", [u["ticker"] for u in _p["unavailable"]], ["NVDA"])

print("\n" + ("-" * 60))
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all tests passed")
