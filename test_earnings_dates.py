#!/usr/bin/env python3
"""Tests for earnings_dates.parse_calendar_events() -- the pure parser that pulls
(date, isEarningsDateEstimate) out of a raw quoteSummary `calendarEvents`
payload (SPEC-71 follow-up: the producer patch that stops going through
`Ticker.calendar`, which drops the estimate flag).

No network: fixture payloads only, shaped exactly like
`result["quoteSummary"]["result"][0]["calendarEvents"]` for two symbols
observed with the flag both true and false (see tools/earn_calendar.js header
and tools/SPEC-71-earnings-date-provenance.md in the jr-dash repo).

Run:  python test_earnings_dates.py
"""

import unittest
from datetime import datetime

import earnings_dates
from earnings_dates import parse_calendar_events

# Epoch seconds for 2026-11-05 00:00:00 local -- exact value doesn't matter,
# only that fromtimestamp(...).date() round-trips to a real date.
_NOV_5_2026 = datetime(2026, 11, 5).timestamp()
_DEC_1_2026 = datetime(2026, 12, 1).timestamp()


def _payload(earnings_date, is_estimate):
    """A raw quoteSummary payload shaped like yfinance's own _fetch() return."""
    return {
        "quoteSummary": {
            "result": [
                {
                    "calendarEvents": {
                        "earnings": {
                            "earningsDate": [earnings_date],
                            "earningsAverage": 1.23,
                            "isEarningsDateEstimate": is_estimate,
                        }
                    }
                }
            ]
        }
    }


class ParseCalendarEventsTests(unittest.TestCase):
    def test_estimate_true(self):
        # e.g. IREN 2026-11-05: "isEarningsDateEstimate true" per SPEC-71.
        date_str, estimated = parse_calendar_events(_payload(_NOV_5_2026, True))
        self.assertEqual(date_str, "2026-11-05")
        self.assertIs(estimated, True)

    def test_estimate_false(self):
        # e.g. MU: a vendor-sourced date, not company-confirmed either way.
        date_str, estimated = parse_calendar_events(_payload(_DEC_1_2026, False))
        self.assertEqual(date_str, "2026-12-01")
        self.assertIs(estimated, False)
        # The caveat this whole feature exists to enforce: False must never be
        # read as "confirmed" -- it is merely "not Yahoo's own projection".
        self.assertIsNot(estimated, "confirmed")

    def test_multiple_dates_picks_soonest(self):
        payload = _payload(_NOV_5_2026, True)
        payload["quoteSummary"]["result"][0]["calendarEvents"]["earnings"]["earningsDate"] = [
            _DEC_1_2026,
            _NOV_5_2026,
        ]
        date_str, estimated = parse_calendar_events(payload)
        self.assertEqual(date_str, "2026-11-05")
        self.assertIs(estimated, True)

    def test_missing_estimate_key_returns_none(self):
        payload = _payload(_NOV_5_2026, False)
        del payload["quoteSummary"]["result"][0]["calendarEvents"]["earnings"]["isEarningsDateEstimate"]
        date_str, estimated = parse_calendar_events(payload)
        self.assertEqual(date_str, "2026-11-05")
        self.assertIsNone(estimated)

    def test_no_earnings_block(self):
        payload = {"quoteSummary": {"result": [{"calendarEvents": {}}]}}
        date_str, estimated = parse_calendar_events(payload)
        self.assertIsNone(date_str)
        self.assertIsNone(estimated)

    def test_malformed_payload_returns_none_none(self):
        for bad in (None, {}, {"quoteSummary": {}}, {"quoteSummary": {"result": []}}):
            date_str, estimated = parse_calendar_events(bad)
            self.assertIsNone(date_str)
            self.assertIsNone(estimated)


class FallsBackWhenThePrivateApiIsGone(unittest.TestCase):
    """next_date() reaches `Ticker._quote._fetch`, two underscore-private
    attributes. requirements.txt pins yfinance for exactly that reason, but a
    pin is only half a guard: if the private path ever disappears the job must
    lose the `estimated` hint, not all 222 dates. These monkeypatch yf.Ticker,
    so there is no network here either."""

    def setUp(self):
        self._real = earnings_dates.yf.Ticker

    def tearDown(self):
        earnings_dates.yf.Ticker = self._real

    def _install(self, ticker):
        earnings_dates.yf.Ticker = lambda tk: ticker

    def test_private_path_is_preferred_and_keeps_the_estimate_flag(self):
        class Quote:
            def _fetch(self, modules):
                return _payload(_NOV_5_2026, True)

        class Ticker:
            _quote = Quote()
            calendar = {"Earnings Date": ["2099-01-01"]}   # must NOT be consulted

        self._install(Ticker())
        self.assertEqual(earnings_dates.next_date("XYZ"), ("2026-11-05", True))

    def test_missing_private_attribute_falls_back_to_ticker_calendar(self):
        class Ticker:                      # no _quote at all -- a yfinance rename
            calendar = {"Earnings Date": ["2026-11-05"]}

        self._install(Ticker())
        date_str, estimated = earnings_dates.next_date("XYZ")
        self.assertEqual(date_str, "2026-11-05")
        self.assertIsNone(estimated, "the public property drops the hint; null, never False")

    def test_renamed_fetch_method_also_falls_back(self):
        class Quote:
            pass                           # _fetch renamed away

        class Ticker:
            _quote = Quote()
            calendar = {"Earnings Date": ["2026-12-01", "2026-11-05"]}

        self._install(Ticker())
        # soonest wins, same rule as the private path
        self.assertEqual(earnings_dates.next_date("XYZ"), ("2026-11-05", None))

    def test_fallback_with_no_calendar_returns_none_none(self):
        for cal in (None, {}, {"Earnings Date": []}, {"Earnings Date": None}):
            class Ticker:
                calendar = cal

            self._install(Ticker())
            self.assertEqual(earnings_dates.next_date("XYZ"), (None, None), repr(cal))

    def test_fallback_accepts_a_bare_scalar_date(self):
        class Ticker:
            calendar = {"earningsDate": "2026-11-05"}      # not a list

        self._install(Ticker())
        self.assertEqual(earnings_dates.next_date("XYZ"), ("2026-11-05", None))


if __name__ == "__main__":
    unittest.main()
