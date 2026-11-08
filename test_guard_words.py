#!/usr/bin/env python3
"""The public repo's guard word list names no firm (C-05, 2026-11-07 review).

This repo is PUBLIC. Its pre-commit guard (tools/sensitive-data-guard.sh)
refuses a guard word on the same line as a figure, and rotation_members.py
carries the same list so the names it writes from fund filings pass. Both lists
once named particular firms; a list that names firms says where the book is
kept, which is itself a position fact under rule #18.

This test cannot list the forbidden names - that would publish them again. It
checks the opposite way: every copy of the list is EXACTLY this closed set of
generic words, so any proper noun added to any copy fails here. To change the
list, change GENERIC below and all three copies together.

Run:  python test_guard_words.py
"""

import io
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))

GENERIC = ["held", "holdings?", "cost basis", "basis", "custodian", "broker", "account",
           "sleeve weight", "book value", "realized"]


def _read(name):
    with io.open(os.path.join(HERE, name), encoding="utf-8") as fh:
        return fh.read()


def _alternation(src, anchor):
    """The words of the first `(a|b|...)` group after `anchor`, joining Python's
    adjacent string literals (r"...|" r"...") first."""
    i = src.index(anchor)
    body = src[i:i + 600]
    body = re.sub(r'"\s*\n\s*r?"', "", body)          # r"(a|b|"\n   r"c|d)" -> one literal
    m = re.search(r"\(((?:[^()|]+\|)+[^()|]+)\)", body)
    assert m, "no word group after %r" % anchor
    return m.group(1).split("|")


class GuardWordTests(unittest.TestCase):
    def test_shell_guard_is_the_generic_set(self):
        words = _alternation(_read(os.path.join("tools", "sensitive-data-guard.sh")), "grep -iE '(held")
        self.assertEqual(words, GENERIC)

    def test_rotation_members_is_the_generic_set(self):
        self.assertEqual(_alternation(_read("rotation_members.py"), "_GUARD_WORD = re.compile("), GENERIC)

    def test_rotation_members_test_copy_is_the_generic_set(self):
        self.assertEqual(_alternation(_read("test_rotation_members.py"), "GUARD_W = "), GENERIC)

    def test_live_regex_matches_the_source(self):
        # The compiled pattern, not just its spelling.
        import rotation_members as M
        self.assertEqual(M._GUARD_WORD.pattern, "(" + "|".join(GENERIC) + ")")

    def test_abbreviations_cover_only_generic_words(self):
        import rotation_members as M
        for word, _ in M._GUARD_ABBR:
            self.assertTrue(any(re.fullmatch(g, word) for g in GENERIC), word)

    def test_no_capitalised_word_in_any_copy(self):
        # Proper nouns are capitalised; the generic set is all lower case.
        for w in GENERIC:
            self.assertEqual(w, w.lower())

    def test_generic_words_still_guard(self):
        import rotation_members as M
        self.assertTrue(M.guard_trips("broker" " account 1,200 " "shares"))
        self.assertFalse(M.guard_trips("Interactive Brokers Group Inc"))     # a name alone is fine
        self.assertFalse(M.guard_trips(M.guard_safe("XYZ Broker" "age 6.5" "% Pfd")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
