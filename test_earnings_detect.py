#!/usr/bin/env python3
"""Tests for earnings_detect.scan_submissions() / files_8k() -- the pure part of the
overdue check, on fixture EDGAR submissions payloads (no network).

Why (2026-11-04): AEM, KGC and AGI are SEC filers (they have a CIK) that file 6-K and
40-F, never an 8-K, so "no Item 2.02 8-K" said nothing about their prints and four
names went OVERDUE and failed the job. A name that never files an 8-K must be listed
as a non-8-K filer, while a US filer with no earnings 8-K must still be able to go
overdue.

Run:  python test_earnings_detect.py
"""

import unittest

import earnings_detect
from earnings_detect import files_8k, scan_submissions


def _sub(rows):
    """A submissions payload: rows of (form, filingDate, items)."""
    return {"filings": {"recent": {
        "form": [r[0] for r in rows],
        "filingDate": [r[1] for r in rows],
        "accessionNumber": ["0000000000-26-%06d" % i for i in range(len(rows))],
        "primaryDocument": ["doc%d.htm" % i for i in range(len(rows))],
        "reportDate": ["" for _ in rows],
        "items": [r[2] for r in rows],
    }}}


FOREIGN = _sub([("6-K", "2026-10-29", ""), ("40-F", "2026-03-28", ""), ("6-K", "2026-07-30", "")])
US_PRINTED = _sub([("8-K", "2026-10-29", "2.02,9.01"), ("10-Q", "2026-10-30", "")])
US_NO_PRINT = _sub([("8-K", "2026-10-20", "5.02"), ("10-Q", "2026-08-05", "")])


class FilesEightK(unittest.TestCase):
    def test_foreign_private_issuer_is_not_an_8k_filer(self):
        self.assertFalse(files_8k(FOREIGN))
        self.assertEqual(scan_submissions(FOREIGN, "2026-10-26", 1), (None, False))

    def test_us_print_is_detected(self):
        hit, eight_k = scan_submissions(US_PRINTED, "2026-10-26", 123)
        self.assertTrue(eight_k)
        self.assertEqual(hit["form"], "8-K")
        self.assertEqual(hit["filed"], "2026-10-29")
        self.assertIn("2.02", hit["items"])

    def test_us_filer_without_earnings_8k_can_still_go_overdue(self):
        hit, eight_k = scan_submissions(US_NO_PRINT, "2026-10-26", 123)
        self.assertIsNone(hit)
        self.assertTrue(eight_k)   # an 8-K filer: missing Item 2.02 IS a signal

    def test_amended_8k_counts_as_an_8k_filer(self):
        self.assertTrue(files_8k(_sub([("8-K/A", "2026-09-01", "2.02")])))

    def test_recent_8k_keeps_its_contract(self):
        orig = earnings_detect.get
        earnings_detect.get = lambda url: US_PRINTED
        try:
            self.assertEqual(earnings_detect.recent_8k(123, "2026-10-26")["filed"], "2026-10-29")
            earnings_detect.get = lambda url: FOREIGN
            self.assertIsNone(earnings_detect.recent_8k(1, "2026-10-26"))
        finally:
            earnings_detect.get = orig


if __name__ == "__main__":
    unittest.main()
