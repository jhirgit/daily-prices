#!/usr/bin/env python3
"""GROUP_PROXY: one list, in the right order (B-13, 2026-11-07 review).

tickers.txt's "# --- ... ---" headers pick each name's benchmark: the first
pattern in GROUP_PROXY that matches the header wins. `ai compute|datacenter` was
checked before `power|energy`, so the "Datacenter power / energy" header took
SMH (semiconductors) and those names' reactions and relative strength were
measured against the wrong cohort.

Two files carry the list (technicals.py for relative strength,
earnings_reactions.py for post-report reactions); they must be byte-identical,
or a name's two benchmarks disagree. This test reads the SOURCE text of both,
so a change to one without the other fails here.

Run:  python test_group_proxy.py
"""

import io
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def _block(name):
    with io.open(os.path.join(HERE, name), encoding="utf-8") as fh:
        src = fh.read().replace("\r\n", "\n")
    m = re.search(r"^GROUP_PROXY = \[\n.*?^\]\n", src, re.M | re.S)
    assert m, "no GROUP_PROXY block in %s" % name
    return m.group(0)


class GroupProxyTests(unittest.TestCase):
    def test_two_copies_are_byte_identical(self):
        self.assertEqual(_block("technicals.py"), _block("earnings_reactions.py"))

    def test_power_is_checked_before_datacenter(self):
        import earnings_reactions as E
        pats = [p for p, _ in E.GROUP_PROXY]
        self.assertLess(pats.index(r"power|energy"), pats.index(r"ai compute|datacenter"))

    def test_datacenter_power_header_benchmarks_on_spy(self):
        import earnings_reactions as E
        import technicals as T
        for mod in (E, T):
            groups = mod.load_groups()
            power = {tk: g for tk, g in groups.items() if g["group"] == "Datacenter power / energy"}
            self.assertTrue(power, "header not found in tickers.txt")
            self.assertEqual({g["proxy"] for g in power.values()}, {"SPY"}, mod.__name__)

    def test_other_headers_unchanged(self):
        # Negative control: the reorder moves that one header and nothing else.
        import earnings_reactions as E
        groups = E.load_groups()
        by_header = {}
        for g in groups.values():
            by_header.setdefault(g["group"], set()).add(g["proxy"])
        self.assertEqual(by_header.get("AI compute / datacenter operators"), {"SMH"})
        self.assertEqual(by_header.get("Semiconductors: compute, design, analog/power"), {"SMH"})
        self.assertEqual(by_header.get("Clean energy / precision industrials (coverage init 8/23/26)"), {"SPY"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
