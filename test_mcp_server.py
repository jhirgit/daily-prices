#!/usr/bin/env python3
"""
mcp_server.py hardening: the SQL deadline, the compare_performance cap and the
fail-closed auth check at startup. Offline; a temp DB stands in for prices.db.

When the `mcp` package isn't installed (it lives in requirements-mcp.txt, not
requirements.txt) a minimal FastMCP stand-in is registered so the module imports.

Run:  python test_mcp_server.py
"""

import os
import sqlite3
import sys
import tempfile
import time
import types

FAILS = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}" + ("" if ok else f", want {want!r}"))
    if not ok:
        FAILS.append(name)


def _stub_mcp():
    class _Settings:
        host = "127.0.0.1"
        port = 8000
        streamable_http_path = "/mcp"

    class FastMCP:
        def __init__(self, name):
            self.name = name
            self.settings = _Settings()
            self.ran = None

        def tool(self):
            return lambda fn: fn

        def run(self, transport):
            self.ran = transport

    fastmcp = types.ModuleType("mcp.server.fastmcp")
    fastmcp.FastMCP = FastMCP
    for name in ("mcp", "mcp.server"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["mcp.server.fastmcp"] = fastmcp


try:
    import mcp.server.fastmcp  # noqa: F401
except ImportError:
    _stub_mcp()

import mcp_server as M


def run_expect_error(fn, *a):
    try:
        fn(*a)
    except Exception as exc:  # noqa: BLE001 - the type and text are what we check
        return exc
    return None


def test_deadline(tmp_path):
    print("SQL deadline")
    start = len(FAILS)
    db = os.path.join(str(tmp_path), "prices.db")
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE daily_prices (ticker TEXT, date TEXT, close REAL)")
        c.executemany("INSERT INTO daily_prices VALUES (?, ?, ?)",
                      [("AAA", "2026-09-01", 10.0), ("AAA", "2026-09-02", 11.0)])
    c.close()
    M.DB_PATH = db

    # Positive control: an ordinary query still answers.
    check("plain select works", M.run_sql("SELECT COUNT(*) AS n FROM daily_prices"), [{"n": 2}])

    M.QUERY_TIMEOUT_S = 0.3
    runaway = ("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
               "SELECT MAX(x) FROM c")
    t0 = time.monotonic()
    exc = run_expect_error(M.run_sql, runaway)
    elapsed = time.monotonic() - t0
    check("runaway query raises ValueError", type(exc).__name__, "ValueError")
    check("error names the time limit", "time limit" in str(exc), True)
    check("aborted near the deadline (<3s)", elapsed < 3.0, True)
    M.QUERY_TIMEOUT_S = 5.0

    check("compare_performance under cap", [r["ticker"] for r in M.compare_performance(["aaa"])], ["AAA"])
    exc = run_expect_error(M.compare_performance, ["T%d" % i for i in range(M.MAX_COMPARE_TICKERS + 1)])
    check("compare_performance over cap rejected", type(exc).__name__, "ValueError")
    assert len(FAILS) == start, FAILS[start:]


def test_startup():
    print("startup auth check")
    start = len(FAILS)
    saved = M.AUTH_TOKEN
    try:
        M.AUTH_TOKEN = ""
        check("no token, 0.0.0.0 refused", M._startup_error("streamable-http", "0.0.0.0", False) is not None, True)
        check("no token, sse public refused", M._startup_error("sse", "::", False) is not None, True)
        check("no token, loopback ok", M._startup_error("streamable-http", "127.0.0.1", False), None)
        check("no token, localhost ok", M._startup_error("streamable-http", "localhost", False), None)
        check("no token, explicit opt-in ok", M._startup_error("streamable-http", "0.0.0.0", True), None)
        check("stdio unaffected", M._startup_error("stdio", "0.0.0.0", False), None)

        M.mcp.ran = None
        code = None
        try:
            M.main(["--host", "0.0.0.0"])
        except SystemExit as e:
            code = e.code
        check("main exits 2 without token", code, 2)
        check("main never started serving", getattr(M.mcp, "ran", None), None)

        M.AUTH_TOKEN = "abcdEFGH1234_-xyz"
        check("token + streamable-http ok", M._startup_error("streamable-http", "0.0.0.0", False), None)
        check("token + sse refused", M._startup_error("sse", "0.0.0.0", False) is not None, True)
        M.AUTH_TOKEN = "bad/token"
        check("non-URL-safe token refused", M._startup_error("streamable-http", "0.0.0.0", False) is not None, True)
    finally:
        M.AUTH_TOKEN = saved
    assert len(FAILS) == start, FAILS[start:]


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        test_deadline(tmp)
    test_startup()
    print(f"\n{'FAIL' if FAILS else 'OK'}: {len(FAILS)} failure(s)")
    sys.exit(1 if FAILS else 0)
