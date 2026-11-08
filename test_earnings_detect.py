#!/usr/bin/env python3
"""Tests for earnings_detect's window (B-05, 2026-11-07 review).

The failure under test: the dates feed rolls a name to its NEXT quarter before
the 8-K for the quarter just reported reaches EDGAR. Keyed only on the feed's
date, the name left the window - no detection, no OVERDUE, no line at all. The
fix carries every name the previous artifact held unresolved (awaiting, overdue
or error) on its OLD expected date until it is detected or the look-back
passes, and reports a name that ages out unresolved.

No network: EDGAR (`recent_8k`) and the CIK map are stubbed, the dates feed and
the previous artifact are fixtures in a temporary directory, and `date.today()`
is pinned.

Run:  python test_earnings_detect.py
"""

import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import date
from unittest import mock

import earnings_detect as ED
from earnings_detect import build_window

TODAY = date(2026, 11, 6)       # a Friday; AMSC expected Wed 11/04


class FixedDate(date):
    @classmethod
    def today(cls):
        return TODAY


class BuildWindowTests(unittest.TestCase):
    def test_rolled_feed_is_carried_on_the_old_date(self):
        feed = {"AMSC": "2027-02-03", "NVDA": "2026-11-19"}
        prev = {"AMSC": {"expected": "2026-11-04", "status": "awaiting"}}
        win, carried, aged = build_window(feed, prev, TODAY, 10)
        self.assertEqual(win, {"AMSC": "2026-11-04"})
        self.assertEqual(carried, {"AMSC": "2027-02-03"})
        self.assertEqual(aged, [])

    def test_negative_control_without_the_previous_artifact_the_name_is_lost(self):
        # The pre-fix behaviour, reproduced: no previous artifact, no carry.
        win, carried, aged = build_window({"AMSC": "2027-02-03"}, {}, TODAY, 10)
        self.assertEqual(win, {})
        self.assertEqual((carried, aged), ({}, []))

    def test_detected_name_is_not_carried(self):
        prev = {"AMSC": {"expected": "2026-11-04", "status": "detected", "accession": "x"}}
        win, carried, aged = build_window({"AMSC": "2027-02-03"}, prev, TODAY, 10)
        self.assertEqual((win, carried, aged), ({}, {}, []))

    def test_overdue_and_error_are_carried_too(self):
        prev = {"A": {"expected": "2026-10-30", "status": "overdue", "sessions_late": 3},
                "B": {"expected": "2026-11-05", "status": "error", "error": "URLError"}}
        win, carried, _ = build_window({"A": "2027-01-29"}, prev, TODAY, 10)
        self.assertEqual(win, {"A": "2026-10-30", "B": "2026-11-05"})
        self.assertEqual(carried, {"A": "2027-01-29", "B": None})

    def test_feed_date_in_window_wins_over_the_carry(self):
        # The feed moved the date by a day but kept it in the window: use the feed.
        prev = {"AMSC": {"expected": "2026-11-04", "status": "awaiting"}}
        win, carried, _ = build_window({"AMSC": "2026-11-05"}, prev, TODAY, 10)
        self.assertEqual(win, {"AMSC": "2026-11-05"})
        self.assertEqual(carried, {})

    def test_ages_out_after_the_lookback_with_a_line(self):
        prev = {"AMSC": {"expected": "2026-10-26", "status": "overdue", "sessions_late": 8}}
        win, carried, aged = build_window({"AMSC": "2027-02-03"}, prev, TODAY, 10)
        self.assertEqual((win, carried), ({}, {}))
        self.assertEqual(aged, [{"ticker": "AMSC", "expected": "2026-10-26", "last_status": "overdue",
                                 "days": 11, "feed_date": "2027-02-03"}])

    def test_boundary_ten_days_is_still_checked(self):
        prev = {"AMSC": {"expected": "2026-10-27", "status": "overdue"}}
        win, _, aged = build_window({}, prev, TODAY, 10)
        self.assertEqual(win, {"AMSC": "2026-10-27"})
        self.assertEqual(aged, [])

    def test_future_awaiting_name_dropped_by_the_feed_is_not_carried(self):
        # Not yet due, so nothing late to look for: the feed is the authority.
        prev = {"X": {"expected": "2026-11-20", "status": "awaiting"}}
        self.assertEqual(build_window({}, prev, TODAY, 10), ({}, {}, []))


class MainEndToEndTests(unittest.TestCase):
    """The whole run, with a rolled feed and an 8-K that lands a day late."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dates = os.path.join(self.tmp, "earnings_dates.json")
        self.out = os.path.join(self.tmp, "earnings_detected.json")
        self._write(self.dates, {"dates": {"AMSC": {"date": "2027-02-03", "estimated": True},
                                           "MU": {"date": "2026-12-17", "estimated": False}}})
        self._write(self.out, {"names": {"AMSC": {"expected": "2026-11-04", "status": "awaiting"},
                                         "OLD": {"expected": "2026-10-20", "status": "overdue",
                                                 "sessions_late": 9}}})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, p, doc):
        with io.open(p, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

    def _run(self, hit):
        calls = []

        def fake_8k(cik, since):
            calls.append((cik, since))
            return hit

        with mock.patch.object(ED, "DATES", self.dates), \
             mock.patch.object(ED, "date", FixedDate), \
             mock.patch.object(ED, "cik_map", lambda refresh=False: {"AMSC": "0000880807", "OLD": "1"}), \
             mock.patch.object(ED, "recent_8k", fake_8k), \
             mock.patch.object(ED.time, "sleep", lambda s: None), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = ED.main(["--out", self.out])
        with io.open(self.out, encoding="utf-8") as fh:
            return rc, json.load(fh), calls, out.getvalue()

    def test_late_8k_after_a_feed_roll_is_detected(self):
        hit = {"form": "8-K", "filed": "2026-11-05", "period": "2026-09-30",
               "accession": "0000880807-26-000099", "items": ["2.02", "9.01"], "url": "u"}
        rc, doc, calls, text = self._run(hit)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [("0000880807", "2026-11-02")])   # asked on the OLD date
        row = doc["names"]["AMSC"]
        self.assertEqual(row["status"], "detected")
        self.assertEqual(row["expected"], "2026-11-04")
        self.assertTrue(row["carried"])
        self.assertEqual(row["feed_date"], "2027-02-03")
        self.assertEqual(doc["counts"]["carried"], 1)
        self.assertIn("CARRIED  AMSC", text)
        # The name that passed the look-back unresolved is reported, not dropped quietly.
        self.assertEqual([r["ticker"] for r in doc["aged_out"]], ["OLD"])
        self.assertEqual(doc["counts"]["aged_out"], 1)
        self.assertIn("AGED OUT OLD", text)
        self.assertNotIn("OLD", doc["names"])

    def test_still_missing_8k_after_a_roll_stays_awaiting_and_carries_again(self):
        rc, doc, _, _ = self._run(None)
        self.assertEqual(rc, 0)                                  # 2 sessions late = GRACE, not yet overdue
        self.assertEqual(doc["names"]["AMSC"]["status"], "awaiting")
        # Tomorrow's run reads this artifact: the name is still in it to carry.
        win, carried, _ = build_window({"AMSC": "2027-02-03"}, doc["names"], date(2026, 11, 9), 10)
        self.assertEqual(win, {"AMSC": "2026-11-04"})

    def test_carried_name_goes_overdue_as_data_and_the_run_still_exits_0(self):
        # Review F-01 (2026-11-08): an overdue name is DATA — in the artifact and on the
        # printed line — not a job failure. Until then this run exited 1, the close job
        # went red 13 runs out of 14, and the alert issue stayed open for weeks.
        global TODAY
        saved, TODAY = TODAY, date(2026, 11, 9)                  # Monday: 3 sessions after 11/04
        try:
            rc, doc, _, text = self._run(None)
        finally:
            TODAY = saved
        self.assertEqual(rc, 0)
        self.assertEqual(doc["names"]["AMSC"]["status"], "overdue")
        self.assertEqual(doc["counts"]["overdue"], 1)
        self.assertIn("OVERDUE  AMSC", text)                      # the per-name line
        self.assertIn("OVERDUE - 1 covered name", text)           # the summary line
        self.assertNotIn("FAIL", text)

    def test_every_lookup_erroring_still_fails_the_run(self):
        # The one detector failure that stays loud: EDGAR answered nothing for every name,
        # which is silence dressed as a clean run (the negative control for F-01).
        def boom(cik, since):
            raise ConnectionError("edgar down")
        with mock.patch.object(ED, "DATES", self.dates), \
             mock.patch.object(ED, "date", FixedDate), \
             mock.patch.object(ED, "cik_map", lambda refresh=False: {"AMSC": "0000880807", "OLD": "1"}), \
             mock.patch.object(ED, "recent_8k", boom), \
             mock.patch.object(ED.time, "sleep", lambda s: None), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = ED.main(["--out", self.out])
        with io.open(self.out, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(rc, 1)
        self.assertEqual(doc["names"]["AMSC"]["status"], "error")
        self.assertEqual(doc["counts"]["errors"], doc["counts"]["checked"])
        self.assertIn("FAIL - every EDGAR lookup errored", out.getvalue())

    def test_one_error_among_several_names_is_carried_not_failed(self):
        # A single EDGAR hiccup is carried to tomorrow's run (CARRY_STATUSES), as before.
        self._write(self.dates, {"dates": {"AMSC": {"date": "2027-02-03", "estimated": True},
                                           "MU": {"date": "2026-11-05", "estimated": False}}})
        def flaky(cik, since):
            if cik == "0000880807":
                raise ConnectionError("one bad lookup")
            return {"form": "8-K", "filed": "2026-11-05", "period": "2026-09-30",
                    "accession": "0000723125-26-000001", "items": ["2.02"], "url": "u"}
        with mock.patch.object(ED, "DATES", self.dates), \
             mock.patch.object(ED, "date", FixedDate), \
             mock.patch.object(ED, "cik_map", lambda refresh=False: {"AMSC": "0000880807", "MU": "0000723125", "OLD": "1"}), \
             mock.patch.object(ED, "recent_8k", flaky), \
             mock.patch.object(ED.time, "sleep", lambda s: None), \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            rc = ED.main(["--out", self.out])
        with io.open(self.out, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(rc, 0)
        self.assertEqual(doc["names"]["AMSC"]["status"], "error")
        self.assertEqual(doc["names"]["MU"]["status"], "detected")


if __name__ == "__main__":
    unittest.main(verbosity=2)
