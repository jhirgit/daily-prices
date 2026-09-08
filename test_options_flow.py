#!/usr/bin/env python3
"""Offline tests for options_flow.py (SPEC-62 phase 1).

Stdlib unittest, NO NETWORK and no yfinance import: every case runs against the
committed synthetic fixture (data/fixtures/options_flow_fixture.json) or against
chains built inline here. The point is the arithmetic that is easy to get wrong
and expensive to get wrong — the three ANDed legs of UNUSUAL, the skew floor, an
open interest of zero, and an IV rank that must stay BLANK until it means
something.

    ./.venv/Scripts/python.exe -m unittest test_options_flow -v
"""

import copy
import datetime as dt
import io
import json
import os
import unittest

import options_flow as of

HERE = os.path.dirname(os.path.abspath(__file__))


def load_fixture():
    with io.open(of.FIXTURE, "r", encoding="utf-8") as fh:
        return json.load(fh)


FX = load_fixture()


def row(ticker, fx=None):
    fx = fx or FX
    return of.summarize_name(
        ticker, fx["chains"][ticker],
        prior=(fx.get("prior") or {}).get(ticker),
        hist=of.hist_for(fx.get("iv_hist"), ticker, fx["chains"][ticker].get("as_of")),
        earnings=fx.get("earnings"))


def contract(**kw):
    c = {"s": "X", "k": 100.0, "v": 0, "oi": 0, "iv": 0.3, "px": 10.0}
    c.update(kw)
    return c


class TestUnusual(unittest.TestCase):
    """vol/OI > 3 AND volume > 500 AND notional > $1M — all three, ANDed."""

    def test_fires_on_the_fixture_contract(self):
        r = row("UNU")
        self.assertEqual(r["n_unusual"], 1)
        self.assertIn("UNUSUAL", r["flags"])

    def test_the_unusual_contract_is_the_one_we_authored(self):
        c = FX["chains"]["UNU"]["expiries"]["2026-09-18"]["calls"][0]
        self.assertTrue(of.is_unusual(c))
        self.assertAlmostEqual(of.vol_oi(c), 10.0)
        self.assertAlmostEqual(of.notional(c), 1_200_000.0)

    def test_near_miss_on_vol_oi(self):
        c = FX["chains"]["UNU"]["expiries"]["2026-09-18"]["calls"][1]
        self.assertAlmostEqual(of.vol_oi(c), 2.5)          # 1000/400, not > 3
        self.assertGreater(c["v"], of.VOL_MIN)
        self.assertGreater(of.notional(c), of.NOTIONAL_MIN)
        self.assertFalse(of.is_unusual(c))

    def test_near_miss_on_volume(self):
        c = FX["chains"]["UNU"]["expiries"]["2026-09-18"]["calls"][2]
        self.assertEqual(c["v"], of.VOL_MIN)               # == 500, not > 500
        self.assertGreater(of.vol_oi(c), of.VOLOI_MIN)
        self.assertGreater(of.notional(c), of.NOTIONAL_MIN)
        self.assertFalse(of.is_unusual(c))

    def test_near_miss_on_notional(self):
        c = FX["chains"]["UNU"]["expiries"]["2026-09-18"]["calls"][3]
        self.assertAlmostEqual(of.notional(c), 999_000.0)  # not > $1M
        self.assertGreater(of.vol_oi(c), of.VOLOI_MIN)
        self.assertGreater(c["v"], of.VOL_MIN)
        self.assertFalse(of.is_unusual(c))

    def test_one_more_cent_tips_the_notional_leg(self):
        c = dict(FX["chains"]["UNU"]["expiries"]["2026-09-18"]["calls"][3])
        c["bid"], c["ask"] = 10.0, 10.02                   # mid 10.01 -> $1,001,000
        self.assertTrue(of.is_unusual(c))

    def test_premium_uses_mid_when_two_sided_else_last(self):
        self.assertAlmostEqual(of.mid_or_last(contract(bid=1.0, ask=2.0, px=9.0)), 1.5)
        self.assertAlmostEqual(of.mid_or_last(contract(bid=0.0, ask=2.0, px=9.0)), 9.0)
        self.assertAlmostEqual(of.mid_or_last(contract(bid=2.0, ask=1.0, px=9.0)), 9.0)
        self.assertIsNone(of.mid_or_last(contract(px=0.0)))


class TestZeroOpenInterest(unittest.TestCase):
    """SPEC-62 s9: oi = 0 everywhere yields voloi_max null — never 0, never inf."""

    def test_vol_oi_is_none_not_inf(self):
        self.assertIsNone(of.vol_oi(contract(v=900, oi=0)))
        self.assertIsNone(of.vol_oi(contract(v=900, oi=None)))

    def test_name_level(self):
        r = row("ZERO")
        self.assertIsNone(r["voloi_max"])
        self.assertIsNone(r["voloi_max_c"])
        self.assertIsNone(r["pc_oi"])
        self.assertEqual(r["n_unusual"], 0)


class TestSkew(unittest.TestCase):
    def test_put_heavy(self):
        r = row("SKEWP")
        self.assertAlmostEqual(r["pc_vol"], 2.0)
        self.assertIn("SKEW", r["flags"])

    def test_call_heavy(self):
        r = row("SKEWC")
        self.assertAlmostEqual(r["pc_vol"], 0.30)
        self.assertIn("SKEW", r["flags"])

    def test_volume_floor_suppresses_the_flag(self):
        r = row("QUIET")
        self.assertAlmostEqual(r["pc_vol"], 2.0)           # ratio would flag
        self.assertEqual(r["cv"] + r["pv"], 900)           # but volume <= 1,000
        self.assertNotIn("SKEW", r["flags"])

    def test_skews_recompute_from_the_published_aggregates(self):
        for tk in ("SKEWP", "SKEWC", "UNU"):
            r = row(tk)
            self.assertAlmostEqual(r["pv"] / float(r["cv"]), r["pc_vol"], places=4)
            self.assertAlmostEqual(r["poi"] / float(r["coi"]), r["pc_oi"], places=4)


class TestOpenInterestDelta(unittest.TestCase):
    def test_aggregate_delta_against_the_prior_snapshot(self):
        r = row("UNU")
        self.assertEqual(r["coi"], 1200)
        self.assertEqual(r["poi"], 1350)
        self.assertEqual(r["d_coi"], 200)                  # 1200 - 1000
        self.assertEqual(r["d_poi"], -50)                  # 1350 - 1400

    def test_per_contract_delta_on_the_carried_contracts(self):
        r = row("UNU")
        by_sym = {t["s"]: t for t in r["top"]}
        self.assertEqual(by_sym["UNU260918C00105000"]["d_oi"], 40)    # 100 - 60
        self.assertEqual(by_sym["UNU260918C00106000"]["d_oi"], 20)    # 400 - 380
        self.assertIsNone(by_sym["UNU260918C00107000"]["d_oi"])       # not carried

    def test_no_prior_means_null_not_zero(self):
        r = row("KINK")
        self.assertIsNone(r["d_coi"])
        self.assertIsNone(r["d_poi"])
        self.assertTrue(all(t["d_oi"] is None for t in r["top"]))

    def test_oi_top_is_the_state_carried_forward(self):
        r = row("UNU")
        self.assertLessEqual(len(r["oi_top"]), of.OI_TOP_N)
        vals = list(r["oi_top"].values())
        self.assertEqual(vals, sorted(vals, reverse=True))
        self.assertEqual(r["oi_top"]["UNU260918P00095000"], 1000)

    def test_top5_is_by_premium_notional_and_reproduces(self):
        r = row("UNU")
        self.assertEqual(len(r["top"]), of.TOP_N)
        self.assertEqual([t["n"] for t in r["top"]],
                         sorted([t["n"] for t in r["top"]], reverse=True))
        self.assertEqual(r["top"][0]["s"], "UNU260918C00107000")      # 500*100*30 = $1.5M
        for t in r["top"]:
            self.assertAlmostEqual(t["n"], t["v"] * 100.0 * t["px"], delta=1.0)


class TestIvRank(unittest.TestCase):
    """Below 60 sessions the rank is BLANK. A rank over three weeks of history is
    a number that means nothing, and shipping it is worse than shipping a null."""

    def test_boundaries(self):
        h = [float(i % 37) + 10.0 for i in range(400)]
        self.assertEqual(of.iv_rank(h[:58], 20.0)[3], "null")          # 58 + 1 = 59
        self.assertEqual(of.iv_rank(h[:59], 20.0)[3], "provisional")   # 59 + 1 = 60
        self.assertEqual(of.iv_rank(h[:250], 20.0)[3], "provisional")  # 251
        self.assertEqual(of.iv_rank(h[:251], 20.0)[3], "full")         # 252
        self.assertEqual(of.iv_rank(h[:300], 20.0)[3], "full")

    def test_null_state_returns_no_numbers(self):
        rank, pct, n, state = of.iv_rank([10.0] * 10, 20.0)
        self.assertIsNone(rank)
        self.assertIsNone(pct)
        self.assertEqual(n, 11)
        self.assertEqual(state, "null")

    def test_missing_iv30_is_null_whatever_the_history(self):
        rank, pct, n, state = of.iv_rank([10.0] * 400, None)
        self.assertEqual(state, "null")
        self.assertIsNone(rank)

    def test_rank_arithmetic_includes_today(self):
        rank, pct, n, state = of.iv_rank([10.0] * 100 + [30.0] * 151, 20.0)
        self.assertEqual(n, 252)
        self.assertAlmostEqual(rank, 0.5)                  # (20-10)/(30-10)
        self.assertAlmostEqual(pct, 100 / 252.0)

    def test_flat_history_gives_no_rank_rather_than_a_divide_by_zero(self):
        rank, pct, n, state = of.iv_rank([25.0] * 260, 25.0)
        self.assertIsNone(rank)
        self.assertEqual(state, "full")

    def test_fixture_states(self):
        self.assertEqual(row("IVNULL")["iv_state"], "null")
        self.assertEqual(row("IVPROV")["iv_state"], "provisional")
        self.assertEqual(row("IVFULL")["iv_state"], "full")

    def test_rank_flags_only_once_non_provisional(self):
        prov, full, low = row("IVPROV"), row("IVFULL"), row("IVLOW")
        self.assertAlmostEqual(prov["iv_rank"], 1.0)
        self.assertNotIn("IV-HIGH", prov["flags"])         # provisional never flags
        self.assertIn("IV-HIGH", full["flags"])
        self.assertIn("IV-LOW", low["flags"])

    def test_null_rank_renders_as_nothing_not_zero(self):
        r = row("IVNULL")
        self.assertIsNone(r["iv_rank"])
        self.assertIsNone(r["iv_pct"])
        self.assertEqual(r["iv_rank_n"], 11)


class TestTermStructure(unittest.TestCase):
    def test_interpolation_is_linear_between_bracketing_expiries(self):
        pts = [(14, 0.60), (42, 0.40), (77, 0.38)]
        self.assertAlmostEqual(of.interp_term(pts, 30), 0.60 + (0.40 - 0.60) * (16 / 28.0))
        self.assertAlmostEqual(of.interp_term(pts, 60), 0.40 + (0.38 - 0.40) * (18 / 35.0))

    def test_flat_outside_the_listed_term(self):
        pts = [(40, 0.50), (70, 0.45)]
        self.assertAlmostEqual(of.interp_term(pts, 30), 0.50)
        self.assertAlmostEqual(of.interp_term(pts, 90), 0.45)
        self.assertIsNone(of.interp_term([(40, None)], 30))

    def test_atm_iv_averages_the_nearest_call_and_put(self):
        calls = [contract(k=95.0, iv=0.5), contract(k=101.0, iv=0.3)]
        puts = [contract(k=99.0, iv=0.4)]
        self.assertAlmostEqual(of.atm_iv(calls, puts, 100.0), 0.35)
        self.assertIsNone(of.atm_iv(calls, puts, None))

    def test_kink_flags_and_names_a_known_print(self):
        r = row("KINK")
        self.assertAlmostEqual(r["iv30"], 48.57, places=2)
        self.assertAlmostEqual(r["iv60"], 38.97, places=2)
        self.assertAlmostEqual(r["kink"], 9.60, places=2)
        self.assertIn("EVENT", r["flags"])
        self.assertEqual(r["kink_event"]["date"], "2026-09-15")

    def test_kink_without_a_known_print_is_still_flagged_but_unnamed(self):
        r = row("KINKX")
        self.assertIn("EVENT", r["flags"])
        self.assertIsNone(r["kink_event"])

    def test_small_kink_does_not_flag(self):
        r = row("UNU")
        self.assertLess(r["kink"], of.KINK_PTS)
        self.assertNotIn("EVENT", r["flags"])

    def test_earnings_window_accepts_both_feed_shapes(self):
        new = {"T": {"date": "2026-09-15", "estimated": True}}
        old = {"T": "2026-09-15"}
        for feed in (new, old):
            hit = of.earnings_in_window(feed, "T", "2026-09-04", "2026-10-04")
            self.assertEqual(hit["date"], "2026-09-15")
        self.assertIsNone(of.earnings_in_window(new, "T", "2026-09-04", "2026-09-10"))
        self.assertIsNone(of.earnings_in_window(new, "T", "2026-09-15", "2026-10-04"))
        self.assertIsNone(of.earnings_in_window(new, "OTHER", "2026-09-04", "2026-10-04"))


class TestExpiryChoice(unittest.TestCase):
    def test_third_friday(self):
        self.assertEqual(of.third_friday(2026, 9), dt.date(2026, 9, 18))
        self.assertEqual(of.third_friday(2026, 10), dt.date(2026, 10, 16))
        self.assertTrue(of.is_monthly("2026-11-20"))
        self.assertFalse(of.is_monthly("2026-09-11"))

    def test_prefers_monthlies(self):
        exps = ["2026-09-09", "2026-09-11", "2026-09-18", "2026-09-25",
                "2026-10-16", "2026-11-20", "2026-12-18"]
        self.assertEqual(of.pick_expiries(exps, 3, dt.date(2026, 9, 5)),
                         ["2026-09-18", "2026-10-16", "2026-11-20"])

    def test_falls_back_to_the_nearest_three_when_monthlies_are_short(self):
        exps = ["2026-09-09", "2026-09-11", "2026-09-25", "2026-10-16"]
        self.assertEqual(of.pick_expiries(exps, 3, dt.date(2026, 9, 5)),
                         ["2026-09-09", "2026-09-11", "2026-09-25"])

    def test_expired_dates_are_dropped(self):
        exps = ["2026-08-21", "2026-09-18", "2026-10-16", "2026-11-20"]
        self.assertNotIn("2026-08-21", of.pick_expiries(exps, 3, dt.date(2026, 9, 5)))


class TestUniverse(unittest.TestCase):
    def test_structural_skips(self):
        self.assertEqual(of.structural_skip("^GSPC"), "index")
        self.assertEqual(of.structural_skip("CL=F"), "futures")
        self.assertEqual(of.structural_skip("BTC-USD"), "crypto")
        self.assertIsNone(of.structural_skip("NVDA"))
        self.assertIsNone(of.structural_skip("005930.KS"))   # probed, not assumed

    def test_tickers_file_parses(self):
        ts = of.load_tickers()
        self.assertGreater(len(ts), 150)
        self.assertIn("NVDA", ts)
        self.assertTrue(all("#" not in t and t.strip() == t for t in ts))


class TestSizeGuard(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(of.size_verdict(of.SIZE_WARN), "ok")
        self.assertEqual(of.size_verdict(of.SIZE_WARN + 1), "warn")
        self.assertEqual(of.size_verdict(of.SIZE_FAIL), "warn")
        self.assertEqual(of.size_verdict(of.SIZE_FAIL + 1), "fail")

    def test_per_name_row_stays_inside_the_measured_budget(self):
        """A row is summary + oi_top(12) + top(5). Measured live on 2026-09-08:
        ~1,270 B median across 195 names. SPEC-62 s5's 850 B estimate is not
        reachable with the ten-field `top` entry the same section prescribes, so
        the bound pinned here is the MEASURED one — this test exists to catch a
        row that suddenly doubles, not to re-litigate the estimate."""
        names = {tk: row(tk) for tk in FX["chains"]}
        payload = of.assemble(names, {}, list(names), FX["as_of"], 1.0)
        med, mx, avg = of.per_name_bytes(payload)
        self.assertLess(med, 1400)
        self.assertLess(mx, 1700)

    def test_the_hard_cap_still_has_headroom_at_the_measured_row_size(self):
        """195 optionable names x 1,270 B = ~250 KB: warns, does not fail. The
        cap is reached around 236 names."""
        self.assertEqual(of.size_verdict(195 * 1270), "warn")
        self.assertEqual(of.size_verdict(236 * 1270), "warn")
        self.assertEqual(of.size_verdict(250 * 1270), "fail")

    def test_payload_carries_no_iv_history(self):
        """252 floats/name projects to ~342 KB — inside the payload that is an
        outright breach of the 300 KB cap, so it lives in its own artifact."""
        names = {tk: row(tk) for tk in FX["chains"]}
        payload = of.assemble(names, {}, list(names), FX["as_of"], 1.0)
        blob = of.dump(payload)
        self.assertNotIn('"iv_hist"', blob)
        self.assertNotIn('"sessions"', blob)
        self.assertEqual(payload["iv_history"], "options_iv_hist.json")


class TestIvHistoryFile(unittest.TestCase):
    def test_appends_against_a_shared_axis(self):
        prev = {"cap": 252, "sessions": ["2026-09-03"], "names": {"A": [10.0]}}
        nxt = of.update_iv_hist(prev, "2026-09-04", {"A": 11.0, "B": 22.0})
        self.assertEqual(nxt["sessions"], ["2026-09-03", "2026-09-04"])
        self.assertEqual(nxt["names"]["A"], [10.0, 11.0])
        self.assertEqual(nxt["names"]["B"], [None, 22.0])       # new name padded

    def test_a_missing_name_gets_a_null_not_a_gap(self):
        prev = {"cap": 252, "sessions": ["2026-09-03"], "names": {"A": [10.0], "B": [20.0]}}
        nxt = of.update_iv_hist(prev, "2026-09-04", {"A": 11.0})
        self.assertEqual(nxt["names"]["B"], [20.0, None])
        self.assertTrue(all(len(v) == len(nxt["sessions"]) for v in nxt["names"].values()))

    def test_rerunning_the_same_session_replaces_rather_than_double_counts(self):
        prev = {"cap": 252, "sessions": ["2026-09-03"], "names": {"A": [10.0]}}
        once = of.update_iv_hist(prev, "2026-09-04", {"A": 11.0})
        twice = of.update_iv_hist(once, "2026-09-04", {"A": 12.0})
        self.assertEqual(twice["sessions"], ["2026-09-03", "2026-09-04"])
        self.assertEqual(twice["names"]["A"], [10.0, 12.0])

    def test_hist_for_drops_a_same_session_tail(self):
        h = {"sessions": ["2026-09-03", "2026-09-04"], "names": {"A": [10.0, 11.0]}}
        self.assertEqual(of.hist_for(h, "A", "2026-09-04"), [10.0])
        self.assertEqual(of.hist_for(h, "A", "2026-09-05"), [10.0, 11.0])
        self.assertEqual(of.hist_for(h, "MISSING", "2026-09-05"), [])

    def test_self_caps(self):
        prev = {"cap": 4, "sessions": ["d%d" % i for i in range(4)],
                "names": {"A": [1.0, 2.0, 3.0, 4.0]}}
        nxt = of.update_iv_hist(prev, "d4", {"A": 5.0}, cap=4)
        self.assertEqual(nxt["sessions"], ["d1", "d2", "d3", "d4"])
        self.assertEqual(nxt["names"]["A"], [2.0, 3.0, 4.0, 5.0])

    def test_a_name_that_falls_out_of_the_window_stops_being_carried(self):
        prev = {"cap": 2, "sessions": ["d0", "d1"], "names": {"GONE": [1.0, None]}}
        nxt = of.update_iv_hist(prev, "d2", {}, cap=2)
        self.assertNotIn("GONE", nxt["names"])


class TestParityFixture(unittest.TestCase):
    def test_verify_passes(self):
        self.assertEqual(of.verify(), 0)

    def test_fixture_is_synthetic(self):
        self.assertIn("SYNTHETIC", FX["source"])

    def test_drift_is_actually_caught(self):
        """The guard has to fail when a number moves, or --verify proves nothing."""
        import tempfile
        bad = copy.deepcopy(FX)
        bad["expected"]["UNU"]["n_unusual"] = 99
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "fx.json")
            with io.open(p, "w", encoding="utf-8") as fh:
                json.dump(bad, fh)
            self.assertEqual(of.verify(p), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
