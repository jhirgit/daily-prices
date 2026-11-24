"""Offline tests for fundamentals.py and valuation.py (no network, no prices.db).

    python -m unittest test_fundamentals
"""
import os
import sqlite3
import tempfile
import unittest

import fundamentals as F
import valuation as V
import news
import options_flow


def fact(start, end, val, filed, accn="a", form="10-Q"):
    d = {"end": end, "val": val, "filed": filed, "accn": accn, "form": form}
    if start:
        d["start"] = start
    return d


def facts_with(concepts, dei=None, tax="us-gaap"):
    f = {tax: {k: {"units": {u: arr}} for k, (u, arr) in concepts.items()}}
    if dei:
        f["dei"] = {"EntityCommonStockSharesOutstanding": {"units": {"shares": dei}}}
    return f


class TTM(unittest.TestCase):
    def setUp(self):
        # FY2025 calendar company: Q1-Q3 reported as 3M and YTD, FY in the 10-K.
        self.rev = [
            fact("2025-01-01", "2025-03-31", 100, "2025-04-30"),
            fact("2025-04-01", "2025-06-30", 110, "2025-07-30"),
            fact("2025-01-01", "2025-06-30", 210, "2025-07-30"),
            fact("2025-07-01", "2025-09-30", 120, "2025-10-30"),
            fact("2025-01-01", "2025-09-30", 330, "2025-10-30"),
            fact("2025-01-01", "2025-12-31", 460, "2026-02-05", form="10-K"),
            fact("2026-01-01", "2026-03-31", 140, "2026-04-30"),
        ]

    def test_q4_derived_from_fy_minus_9m(self):
        P = F.flow_periods(facts_with({"Revenues": ("USD", self.rev)}), "us-gaap", ["Revenues"], "USD")
        Q = F.quarters_from_periods(P)
        self.assertEqual(Q["2025-12-31"]["v"], 130)
        self.assertTrue(Q["2025-12-31"]["derived"])
        self.assertEqual(Q["2025-12-31"]["known"], "2026-02-05")
        self.assertEqual(Q["2025-12-31"]["start"], "2025-10-01")

    def _hubs_da(self):
        # HUBS shape: total D&A tagged YTD only; P&E-only D&A also tagged per quarter
        tot = [fact("2025-01-01", "2025-09-30", 97.7, "2025-11-05"),
               fact("2025-01-01", "2025-12-31", 136.3, "2026-02-11", form="10-K"),
               fact("2026-01-01", "2026-03-31", 40.2, "2026-05-01"),
               fact("2026-01-01", "2026-06-30", 84.6, "2026-08-01"),
               fact("2026-01-01", "2026-09-30", 130.8, "2026-11-04")]
        narrow = [fact("2026-04-01", "2026-06-30", 10.0, "2026-08-01"),
                  fact("2026-07-01", "2026-09-30", 11.3, "2026-11-04")]
        return facts_with({"DepreciationDepletionAndAmortization": ("USD", tot),
                           "DepreciationAndAmortization": ("USD", narrow)})

    NAMES = ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization"]

    def test_da_ttm_comes_from_one_concept(self):
        o = F.OneConcept(self._hubs_da(), "us-gaap", self.NAMES, "USD")
        self.assertAlmostEqual(o.ttm("2026-09-30")[0], 169.4, places=6)   # the cash-flow statements' figure

    def test_mixed_concepts_control(self):
        # control: the period-level fill mixes the two scopes and gives the 100.1 the audit
        # found wrong; OneConcept is what changes it
        P = F.flow_periods(self._hubs_da(), "us-gaap", self.NAMES, "USD")
        self.assertAlmostEqual(F.ttm_at(P, F.quarters_from_periods(P), "2026-09-30")[0], 100.1, places=6)

    def test_one_concept_falls_back_to_the_next_concept(self):
        # a filer with only the narrow concept still gets a D&A line (from that concept alone)
        f = facts_with({"DepreciationAndAmortization": ("USD", [
            fact("2025-01-01", "2025-12-31", 40.0, "2026-02-01", form="10-K"),
            fact("2025-01-01", "2025-09-30", 30.0, "2025-11-01"),
            fact("2026-01-01", "2026-09-30", 33.0, "2026-11-01")])})
        self.assertAlmostEqual(F.OneConcept(f, "us-gaap", self.NAMES, "USD").ttm("2025-12-31")[0], 40.0)

    # -- fix-all 2026-11-23: AMSC's mis-dated stock-pay contexts ---------------------------
    @staticmethod
    def _amsc_sbc(twin=True):
        """AMSC shape (fiscal year from April 1), cut from its filings: the fiscal-Q2 10-Q (q2)
        tags the cash-flow stock pay 2026-01-01..09-30 (and 2025-01-01..09-30) for the six months;
        the fiscal-Q1 10-Q (q1) tags April-June 2026 a second time as October-December 2025."""
        def fq(s, e, v, filed, accn, fp, form="10-Q"):
            d = fact(s, e, v, filed, accn=accn, form=form)
            d["fp"] = fp
            return d
        sbc = [fq("2025-04-01", "2025-09-30", 8292000, "2025-11-05", "q2a", "Q2"),
               fq("2025-04-01", "2025-12-31", 12114000, "2026-02-04", "q3", "Q3"),
               fq("2025-04-01", "2026-03-31", 15869000, "2026-05-27", "k", "FY", form="10-K"),
               fq("2026-04-01", "2026-06-30", 5264000, "2026-08-05", "q1", "Q1"),
               fq("2025-01-01", "2025-09-30", 8292000, "2026-11-04", "q2", "Q2"),
               fq("2026-01-01", "2026-09-30", 11781000, "2026-11-04", "q2", "Q2")]
        alloc = [fq("2025-07-01", "2025-09-30", 3766000, "2026-11-04", "q2", "Q2"),
                 fq("2025-04-01", "2025-09-30", 8292000, "2026-11-04", "q2", "Q2"),
                 fq("2026-07-01", "2026-09-30", 6517000, "2026-11-04", "q2", "Q2"),
                 fq("2025-10-01", "2025-12-31", 3822000, "2026-02-04", "q3", "Q3"),
                 fq("2025-10-01", "2025-12-31", 5264000, "2026-08-05", "q1", "Q1")]
        if twin:
            alloc.append(fq("2026-04-01", "2026-09-30", 11781000, "2026-11-04", "q2", "Q2"))
        return facts_with({"ShareBasedCompensation": ("USD", sbc),
                           "AllocatedShareBasedCompensationExpense": ("USD", alloc)})

    SBC = ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"]

    def test_amsc_misdated_stock_pay_read_on_the_filed_period(self):
        # was 20.8 (6.517 + 5.264 mis-dated + 3.755 + 5.264); the filings give 15.869 + 11.781 - 8.292
        o = F.OneConcept(self._amsc_sbc(), "us-gaap", self.SBC, "USD")
        self.assertAlmostEqual(o.ttm("2026-09-30")[0], 19358000, places=0)
        fx = F.misdated_fixes(self._amsc_sbc(), "us-gaap", self.SBC, "USD")
        self.assertEqual(fx[("ShareBasedCompensation", "q2", "2026-01-01", "2026-09-30")], ("2026-04-01", "2026-09-30"))
        self.assertEqual(fx[("AllocatedShareBasedCompensationExpense", "q1", "2025-10-01", "2025-12-31")],
                         ("2026-04-01", "2026-06-30"))

    def test_misdated_control_no_twin_is_left_as_filed(self):
        # control: without the same amount on a period the filing reports, an odd period is left
        # exactly as filed (a predecessor stub after a fresh start is real data: WOLF, TLN)
        fx = F.misdated_fixes(self._amsc_sbc(twin=False), "us-gaap", self.SBC, "USD")
        self.assertNotIn(("ShareBasedCompensation", "q2", "2026-01-01", "2026-09-30"), fx)

    def test_misdated_control_correct_contexts_untouched(self):
        # control: a filer whose contexts are right gets no fixes at all
        rows = [dict(x, fp=fp) for x, fp in zip(self.rev, ["Q1", "Q2", "Q2", "Q3", "Q3", "FY", "Q1"])]
        f = facts_with({"Revenues": ("USD", rows)})
        self.assertEqual(F.misdated_fixes(f, "us-gaap", ["Revenues"], "USD"), {})

    def test_proxy_statement_facts_are_ignored(self):
        # HUBS: the 2026 proxy's pay-versus-performance table re-tags FY2023 net income as first
        # reported (-176.3), after the 10-K that restated it (-164.5)
        ni = [fact("2023-01-01", "2023-12-31", -176.3, "2024-02-14", form="10-K"),
              fact("2023-01-01", "2023-12-31", -164.5, "2026-02-11", form="10-K"),
              fact("2023-01-01", "2023-12-31", -176.3, "2026-04-27", form="DEF 14A")]
        P = F.flow_periods(facts_with({"NetIncomeLoss": ("USD", ni)}), "us-gaap", ["NetIncomeLoss"], "USD")
        self.assertEqual(P[("2023-01-01", "2023-12-31")]["v"], -164.5)

    def test_cash_flow_quarters_from_ytd_only(self):
        # 10-Q cash flows are YTD only: Q2 = 6M - Q1, Q3 = 9M - 6M
        cfo = [fact("2025-01-01", "2025-03-31", 10, "2025-04-30"),
               fact("2025-01-01", "2025-06-30", 25, "2025-07-30"),
               fact("2025-01-01", "2025-09-30", 45, "2025-10-30")]
        Q = F.quarters_from_periods(F.flow_periods(facts_with({"X": ("USD", cfo)}), "us-gaap", ["X"], "USD"))
        self.assertEqual((Q["2025-06-30"]["v"], Q["2025-09-30"]["v"]), (15, 20))

    def test_ttm_fy_and_rolling(self):
        P = F.flow_periods(facts_with({"Revenues": ("USD", self.rev)}), "us-gaap", ["Revenues"], "USD")
        Q = F.quarters_from_periods(P)
        self.assertEqual(F.ttm_at(P, Q, "2025-12-31")[:3:2], (460, "FY"))
        v, known, how = F.ttm_at(P, Q, "2026-03-31")
        self.assertEqual((v, how), (110 + 120 + 130 + 140, "4Q"))
        self.assertEqual(known, "2026-04-30")
        self.assertIsNone(F.ttm_at(P, Q, "2025-09-30"))   # no year-ago quarters

    def test_restatement_latest_value_first_date(self):
        rev = [fact("2025-04-01", "2025-06-30", 110, "2025-07-30"),
               fact("2025-04-01", "2025-06-30", 105, "2026-07-30")]   # restated a year later
        P = F.flow_periods(facts_with({"Revenues": ("USD", rev)}), "us-gaap", ["Revenues"], "USD")
        p = P[("2025-04-01", "2025-06-30")]
        self.assertEqual((p["v"], p["first"], p["last"]), (105, "2025-07-30", "2026-07-30"))

    def test_concept_priority_fills_gaps_only(self):
        f = facts_with({"Revenues": ("USD", [fact("2025-01-01", "2025-03-31", 100, "2025-04-30")]),
                        "SalesRevenueNet": ("USD", [fact("2025-01-01", "2025-03-31", 999, "2025-04-30"),
                                                    fact("2024-10-01", "2024-12-31", 90, "2025-02-01")])})
        P = F.flow_periods(f, "us-gaap", ["Revenues", "SalesRevenueNet"], "USD")
        self.assertEqual(P[("2025-01-01", "2025-03-31")]["v"], 100)
        self.assertEqual(P[("2024-10-01", "2024-12-31")]["v"], 90)

    def test_currency_prefers_reporting_unit_over_convenience_usd(self):
        f = facts_with({"Revenue": ("TWD", [fact("2023-01-01", "2023-12-31", 1, "x"),
                                            fact("2024-01-01", "2024-12-31", 2, "y")])}, tax="ifrs-full")
        f["ifrs-full"]["Revenue"]["units"]["USD"] = [fact("2024-01-01", "2024-12-31", 3, "y")]
        self.assertEqual(F.pick_currency(f, "ifrs-full"), "TWD")


class Shares(unittest.TestCase):
    def test_classes_summed_within_a_filing(self):
        dei = [fact(None, "2026-10-20", 5.8e9, "2026-10-29", accn="q3"),
               fact(None, "2026-10-20", 0.86e9, "2026-10-29", accn="q3"),
               fact(None, "2026-10-20", 5.6e9, "2026-10-29", accn="q3")]
        obs = F.share_observations(facts_with({}, dei=dei))
        self.assertEqual(len(obs), 1)
        self.assertAlmostEqual(obs[0]["v"], 12.26e9)

    def test_one_class_cover_replaced_by_balance_sheet(self):
        dei = [fact(None, "2026-10-20", 2.2e9, "2026-10-29", accn="q3")]
        cso = [fact(None, "2026-09-30", 2.57e9, "2026-10-29", accn="q3")]
        obs = F.share_observations(facts_with({"CommonStockSharesOutstanding": ("shares", cso)}, dei=dei))
        self.assertEqual(obs[0]["v"], 2.57e9)
        self.assertIn("CommonStockSharesOutstanding", obs[0]["src"])

    def test_weighted_diluted_fallback(self):
        wa = [fact("2026-07-01", "2026-09-30", 2.6e9, "2026-10-29", accn="q3"),
              fact("2026-01-01", "2026-09-30", 2.61e9, "2026-10-29", accn="q3")]
        obs = F.share_observations(facts_with({"WeightedAverageNumberOfDilutedSharesOutstanding": ("shares", wa)}))
        self.assertEqual(obs[0]["v"], 2.6e9)          # the quarter, not the 9M average

    def _obs(self, vals):
        return [{"end": "2024-%02d-01" % (i + 1), "filed": "2024-%02d-15" % (i + 1), "v": v, "src": "dei cover"}
                for i, v in enumerate(vals)]

    def test_split_confirmed_by_restatement(self):
        obs = self._obs([2.45e9, 2.46e9, 24.5e9, 24.4e9])
        restated = [(10.0, "2024-01-15", "2024-03-15")]     # an earlier period re-reported x10
        s = F.detect_splits(obs, restated)
        self.assertEqual([(x["after"], x["ratio"]) for x in s], [("2024-03-15", 10.0)])
        F.split_adjust(obs, s)
        self.assertAlmostEqual(obs[0]["adj"], 24.5e9)
        self.assertAlmostEqual(obs[3]["adj"], 24.4e9)

    def test_unrestated_doubling_is_not_a_split(self):
        # an IPO or a share-financed merger doubles the count but restates nothing
        obs = self._obs([240e6, 480e6, 490e6])
        self.assertEqual(F.detect_splits(obs, []), [])
        self.assertEqual(obs[1].get("unconfirmed_step"), 2.0)

    def test_three_for_two_merger_never_a_split(self):
        obs = self._obs([800e6, 1200e6])
        self.assertEqual(F.detect_splits(obs, [(1.5, "2024-01-15", "2024-02-15")]), [])

    def test_reverse_split(self):
        obs = self._obs([10e9, 1e9])
        s = F.detect_splits(obs, [(0.1, "2024-01-15", "2024-02-15")])
        self.assertEqual(s[0]["ratio"], 0.1)

    # declared splits (2026-11-20): SNDK 8-for-1 effective 2026-11-19, no filing shows it yet
    def _sndk(self, extra=()):
        return [{"end": "2026-04-24", "filed": "2026-05-01", "v": 148.09e6, "src": "dei cover"},
                {"end": "2026-08-07", "filed": "2026-08-17", "v": 146.419e6, "src": "dei cover"},
                {"end": "2026-10-26", "filed": "2026-10-30", "v": 142.289e6, "src": "dei cover"}] + list(extra)

    def test_declared_split_rescales_counts_before_the_effective_date(self):
        obs = self._sndk()
        det = F.detect_splits(obs, [])
        decl = F.declared_splits("SNDK", det)
        self.assertEqual([(s["after"], s["ratio"], s["declared"]) for s in decl], [("2026-11-19", 8.0, True)])
        F.split_adjust(obs, det + decl)
        self.assertAlmostEqual(obs[-1]["adj"], 142.289e6 * 8)
        self.assertFalse(any(o.get("glitch") for o in obs))

    def test_declared_split_control_other_ticker_untouched(self):
        obs = self._sndk()
        self.assertEqual(F.declared_splits("MU", F.detect_splits(obs, [])), [])
        F.split_adjust(obs, F.detect_splits(obs, []))
        self.assertAlmostEqual(obs[-1]["adj"], 142.289e6)

    def test_declared_split_leaves_a_post_split_count_alone(self):
        # the next 10-Q's cover count, as of a date after the split, before detection confirms it
        obs = self._sndk([{"end": "2027-01-23", "filed": "2027-02-01", "v": 1138.3e6, "src": "dei cover"}])
        det = F.detect_splits(obs, [])                      # no restatement yet: not detected
        self.assertEqual(det, [])
        F.split_adjust(obs, det + F.declared_splits("SNDK", det))
        self.assertAlmostEqual(obs[-1]["adj"], 1138.3e6)    # new basis, not x8 again
        self.assertAlmostEqual(obs[-2]["adj"], 142.289e6 * 8)
        self.assertFalse(any(o.get("glitch") for o in obs))

    def test_declared_split_retires_once_the_filings_show_it(self):
        obs = self._sndk([{"end": "2027-01-23", "filed": "2027-02-01", "v": 1138.3e6, "src": "dei cover"}])
        det = F.detect_splits(obs, [(8.0, "2026-05-01", "2027-02-01")])   # restated x8: detected
        self.assertEqual([(s["after"], s["ratio"]) for s in det], [("2027-02-01", 8.0)])
        self.assertEqual(F.declared_splits("SNDK", det), [])               # nothing applied twice
        F.split_adjust(obs, det)
        self.assertAlmostEqual(obs[0]["adj"], 148.09e6 * 8)
        self.assertAlmostEqual(obs[-1]["adj"], 1138.3e6)

    def test_misscaled_filing_dropped(self):
        obs = self._obs([13.3e6, 13.2e6, 13.3e6, 13.3e3])   # one filing tagged in thousands
        F.split_adjust(obs, F.detect_splits(obs, []))
        self.assertTrue(obs[3].get("glitch"))
        self.assertFalse(any(o.get("glitch") for o in obs[:3]))


class Universe(unittest.TestCase):
    def test_classify(self):
        self.assertIsNotNone(F.classify("^GSPC", "", "", set()))
        self.assertIsNotNone(F.classify("EURUSD=X", "", "", set()))
        self.assertIsNotNone(F.classify("SMH", "semiconductors", "Broad / thematic / crypto ETFs", set()))
        self.assertIsNotNone(F.classify("XLK", "", "", {"XLK"}))
        self.assertIsNone(F.classify("NVDA", "Nvidia", "Semiconductors: compute", set()))

    def test_gold_pinned_to_barrick(self):
        self.assertEqual(F.CIK_OVERRIDE["GOLD"], "0000756894")

    def test_fx_symbols_skipped_by_news_and_options(self):
        self.assertIsNotNone(news.skip_reason("TWDUSD=X"))
        self.assertEqual(options_flow.structural_skip("EURUSD=X"), "fx")


class NoLookAhead(unittest.TestCase):
    def setUp(self):
        self.rec = {"kind": "operating", "currency": "USD", "adr_ratio": 1.0,
                    "quarters": [{"end": "2025-12-31", "known": "2026-02-05", "rev": 400.0, "ni": 40.0, "oi": 60.0,
                                  "ebitda": 80.0, "fcf": 30.0, "cash": 10.0, "debt": 50.0, "equity": 100.0},
                                 {"end": "2026-03-31", "known": "2026-04-30", "rev": 500.0, "ni": 50.0, "oi": 70.0,
                                  "ebitda": 100.0, "fcf": 40.0, "cash": 20.0, "debt": 40.0, "equity": 120.0}],
                    "shares": [{"filed": "2026-02-05", "adj": 10.0}, {"filed": "2026-04-30", "adj": 12.0}]}
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE daily_prices (ticker TEXT, date TEXT, close REAL)")
        for d in ("2026-02-04", "2026-02-05", "2026-04-29", "2026-04-30"):
            con.execute("INSERT INTO daily_prices VALUES ('X', ?, 100.0)", (d,))
        con.commit()
        self.con = con

    def tearDown(self):
        self.con.close()
        os.unlink(self.db)

    def test_quarter_applies_only_from_its_filing_date(self):
        d, s, info = V.series_for("X", self.rec, self.con, {})
        self.assertEqual(d, ["2026-02-05", "2026-04-29", "2026-04-30"])   # nothing before the first filing
        # 04-29: still the Dec quarter and the Feb share count -> mcap 1000, P/S 2.5
        self.assertAlmostEqual(s["ps"][1], 1000 / 400.0)
        # 04-30: the March quarter and its share count -> mcap 1200, P/S 2.4
        self.assertAlmostEqual(s["ps"][2], 1200 / 500.0)
        self.assertAlmostEqual(s["ev_ebitda"][2], (1200 + 40 - 20) / 100.0)

    def test_fx_pending_is_said(self):
        rec = dict(self.rec, currency="TWD")
        d, s, info = V.series_for("X", rec, self.con, {})
        self.assertIn("FX history pending", info["why"])


class Multiples(unittest.TestCase):
    def test_bank_gets_no_ev_metrics(self):
        m = V.multiples({"rev": 100.0, "ni": 10.0, "equity": 50.0, "debt": 999.0, "cash": 1.0}, 200.0, "bank")
        self.assertEqual(m["pe"], 20.0)
        self.assertEqual(m["pb"], 4.0)
        self.assertNotIn("ev_ebitda", m)

    def test_loss_and_near_zero_denominators(self):
        m = V.multiples({"rev": 100.0, "ni": -5.0, "ebitda": 0.5, "oi": 1.0, "fcf": 2.0}, 1000.0, "operating")
        self.assertIsNone(m["pe"])
        self.assertIsNone(m["ev_ebitda"])       # 2000x is not meaningful
        self.assertEqual(V.why_none("pe", {"ni": -5.0}, "operating"), "a loss (TTM net income is negative)")
        self.assertIn("near zero", V.why_none("ev_ebitda", {"ebitda": 0.5, "debt": 0.0, "cash": 0.0}, "operating"))


class Debt(unittest.TestCase):
    """Coordinator review 2026-11-04: ORCL had been valued with debt = 0. Debt is never
    zero-filled; each shape below is a real filer's tagging pattern."""

    def resolve(self, concepts, end):
        f = facts_with({k: ("USD", v) for k, v in concepts.items()})
        return F.debt_resolver(f, "us-gaap", "USD", "X")(end)

    def test_orcl_shape_notes_payable(self):
        r = self.resolve({"LongTermNotesPayable": [fact(None, "2026-08-31", 122342e6, "2026-09-11")],
                          "NotesPayableCurrent": [fact(None, "2026-08-31", 7625e6, "2026-09-11")],
                          "CommercialPaper": [fact(None, "2026-08-31", 1000e6, "2026-09-11")]}, "2026-08-31")
        self.assertAlmostEqual(r["v"], 130967e6)
        self.assertIn("LongTermNotesPayable", r["src"])
        self.assertIn("CommercialPaper", r["src"])
        self.assertFalse(r.get("stale"))

    def test_crwv_shape_tag_switch_midyear(self):
        tags = {"LongTermDebtNoncurrent": [fact(None, "2026-03-31", 17312e6, "2026-05-10")],
                "LongTermDebtCurrent": [fact(None, "2026-03-31", 7547e6, "2026-05-10")],
                "DebtInstrumentCarryingAmount": [fact(None, "2026-06-30", 35551e6, "2026-08-12")]}
        q1, q2 = self.resolve(tags, "2026-03-31"), self.resolve(tags, "2026-06-30")
        self.assertAlmostEqual(q1["v"], 24859e6)
        self.assertAlmostEqual(q2["v"], 35551e6)
        self.assertEqual(q2["src"], "DebtInstrumentCarryingAmount")

    def test_cat_shape_no_tag_this_period_is_na(self):
        # tagged once, long ago, then only custom extensions: n/a, never zero
        r = self.resolve({"LongTermDebtNoncurrent": [fact(None, "2015-12-31", 25000e6, "2016-02-15")]}, "2026-06-30")
        self.assertIsNone(r["v"])
        self.assertIn("debt not tagged in XBRL", r["why"])

    def test_truly_debt_free_is_zero_with_label(self):
        r = self.resolve({"Revenues": [fact("2026-01-01", "2026-03-31", 1, "x")]}, "2026-06-30")
        self.assertEqual(r["v"], 0.0)
        self.assertIn("no debt reported", r["src"])

    def test_latest_tagged_debt_zero_is_zero(self):
        r = self.resolve({"LongTermDebt": [fact(None, "2025-12-31", 0, "2026-02-01")]}, "2026-06-30")
        self.assertEqual(r["v"], 0.0)

    def test_earlier_balance_sheet_is_used_labelled_stale(self):
        r = self.resolve({"LongTermDebt": [fact(None, "2026-03-31", 500e6, "2026-05-01")]}, "2026-06-30")
        self.assertEqual((r["v"], r["asof"], r.get("stale")), (500e6, "2026-03-31", True))

    # -- one borrowing counted once (process audit 2026-11-22) ---------------------
    def test_lite_shape_double_tag_counted_once(self):
        # LITE 10-Q 2026-09-26: the current converts tagged as BOTH LongTermDebtCurrent and
        # ShortTermBorrowings in one filing. Was 2 x 1,360.3 + 39.3 = 2,759.9.
        a = "0001628280-26-069615"
        r = self.resolve({"LongTermDebtNoncurrent": [fact(None, "2026-09-26", 39.3e6, "2026-11-03", accn=a)],
                          "LongTermDebtCurrent": [fact(None, "2026-09-26", 1360.3e6, "2026-11-03", accn=a)],
                          "ShortTermBorrowings": [fact(None, "2026-09-26", 1360.3e6, "2026-11-03", accn=a)]},
                         "2026-09-26")
        self.assertAlmostEqual(r["v"], 1399.6e6)
        self.assertIn("counted once", r["src"])

    def test_same_amount_in_different_filings_is_two_borrowings(self):
        # control: equal amounts that no single filing reports together are not a double tag
        r = self.resolve({"LongTermDebtNoncurrent": [fact(None, "2026-09-30", 500e6, "2026-11-03", accn="a1")],
                          "LongTermDebtCurrent": [fact(None, "2026-09-30", 100e6, "2026-11-03", accn="a1")],
                          "ShortTermBorrowings": [fact(None, "2026-09-30", 100e6, "2026-11-20", accn="a2")]},
                         "2026-09-30")
        self.assertAlmostEqual(r["v"], 700e6)
        self.assertNotIn("counted once", r["src"])

    # -- fix-all 2026-11-23: the two shapes the first fix missed ---------------------
    def test_be_shape_rounding_in_one_filing_counted_once(self):
        # BE 10-K 2024-12-31: the 2025 converts are LongTermDebtCurrent 114,385,000 and, in the
        # debt note's $0.1M table, ShortTermBorrowings 114,400,000. LongTermDebt (total) is
        # 1,128,792,000 = noncurrent + current portion, so the short-term tag is the same notes.
        a = "0001628280-25-008747"
        r = self.resolve({"LongTermDebtNoncurrent": [fact(None, "2024-12-31", 1014407000, "2025-02-27", accn=a)],
                          "LongTermDebtCurrent": [fact(None, "2024-12-31", 114385000, "2025-02-27", accn=a)],
                          "ShortTermBorrowings": [fact(None, "2024-12-31", 114400000, "2025-02-27", accn=a)]},
                         "2024-12-31")
        self.assertAlmostEqual(r["v"], 1128792000)
        self.assertIn("to rounding", r["src"])

    def test_rounding_control_amounts_that_round_apart_both_count(self):
        # control: 4.0M against 3.6M in one filing is two borrowings, whatever the unit
        a = "acc-1"
        r = self.resolve({"LongTermDebtNoncurrent": [fact(None, "2026-09-30", 500e6, "2026-11-03", accn=a)],
                          "LongTermDebtCurrent": [fact(None, "2026-09-30", 3.6e6, "2026-11-03", accn=a)],
                          "ShortTermBorrowings": [fact(None, "2026-09-30", 4.0e6, "2026-11-03", accn=a)]},
                         "2026-09-30")
        self.assertAlmostEqual(r["v"], 507.6e6)
        self.assertNotIn("counted once", r["src"])

    def test_rounded_equal_unit_rules(self):
        self.assertTrue(F.rounded_equal(114400000, 114385000))     # to the $0.1M
        self.assertTrue(F.rounded_equal(4000000, 3959000))         # 4,000,000 read to the $0.1M
        self.assertTrue(F.rounded_equal(3000000, 2950000))         # half up
        self.assertFalse(F.rounded_equal(1000000, 600000))         # a $1M unit is 100% of the amount
        self.assertFalse(F.rounded_equal(1200000000, 1195000000))  # units stop at $1M
        self.assertFalse(F.rounded_equal(114385000, 114386000))    # same unit, different figures

    def test_aosl_shape_relabelled_between_filings_counted_once(self):
        # AOSL 2026-06-30: the 10-K tags the current part of its loans ShortTermBorrowings, the next
        # 10-Q's comparative column tags the same 3,094,000 LongTermDebtCurrent; both filings show
        # LongTermDebtNoncurrent 537,000, and DebtLongtermAndShorttermCombinedAmount is 3,631,000.
        k, q = "0001628280-26-059261", "0001628280-26-071659"
        r = self.resolve({"LongTermDebtNoncurrent": [fact(None, "2026-06-30", 537000, "2026-08-20", accn=k, form="10-K"),
                                                     fact(None, "2026-06-30", 537000, "2026-11-05", accn=q)],
                          "ShortTermBorrowings": [fact(None, "2026-06-30", 3094000, "2026-08-20", accn=k, form="10-K")],
                          "LongTermDebtCurrent": [fact(None, "2026-06-30", 3094000, "2026-11-05", accn=q)],
                          "DebtLongtermAndShorttermCombinedAmount":
                              [fact(None, "2026-06-30", 3631000, "2026-08-20", accn=k, form="10-K")]}, "2026-06-30")
        self.assertAlmostEqual(r["v"], 3631000)
        self.assertIn("relabelled between filings", r["src"])

    def test_relabel_control_a_total_of_the_doubled_sum_keeps_both(self):
        # control: same shape, but a filing's total debt is the sum WITH both amounts
        k, q = "k1", "q1"
        r = self.resolve({"LongTermDebtNoncurrent": [fact(None, "2026-06-30", 537000, "2026-08-20", accn=k, form="10-K"),
                                                     fact(None, "2026-06-30", 537000, "2026-11-05", accn=q)],
                          "ShortTermBorrowings": [fact(None, "2026-06-30", 3094000, "2026-08-20", accn=k, form="10-K")],
                          "LongTermDebtCurrent": [fact(None, "2026-06-30", 3094000, "2026-11-05", accn=q)],
                          "DebtLongtermAndShorttermCombinedAmount":
                              [fact(None, "2026-06-30", 6725000, "2026-08-20", accn=k, form="10-K")]}, "2026-06-30")
        self.assertAlmostEqual(r["v"], 6725000)

    def test_relabel_control_a_filing_showing_both_apart_keeps_both(self):
        # control: one filing shows the two tags at different amounts: two lines, never relabelled
        k, q = "k1", "q1"
        r = self.resolve({"LongTermDebtNoncurrent": [fact(None, "2026-06-30", 500e6, "2026-08-20", accn=k, form="10-K"),
                                                     fact(None, "2026-06-30", 500e6, "2026-11-05", accn=q)],
                          "ShortTermBorrowings": [fact(None, "2026-06-30", 30e6, "2026-08-20", accn=k, form="10-K"),
                                                  fact(None, "2026-06-30", 30e6, "2026-11-05", accn=q)],
                          "LongTermDebtCurrent": [fact(None, "2026-06-30", 20e6, "2026-08-20", accn=k, form="10-K"),
                                                  fact(None, "2026-06-30", 30e6, "2026-11-06", accn="q2")]},
                         "2026-06-30")
        self.assertAlmostEqual(r["v"], 560e6)
        self.assertNotIn("counted once", r["src"])

    def test_relabel_control_qcom_shape_paper_not_inside_the_total(self):
        # control: QCOM 2016-09-25. LongTermDebt 10,008 (notes only) and CommercialPaper 1,749 in the
        # 10-K; later filings tag the same paper DebtCurrent. The total does not hold the paper, so
        # it is a real second borrowing: 11,757, never 10,008.
        k, q = "0001234452-16-000552", "0001234452-17-000154"
        r = self.resolve({"LongTermDebt": [fact(None, "2016-09-25", 10008e6, "2016-11-02", accn=k, form="10-K"),
                                           fact(None, "2016-09-25", 10008e6, "2017-07-19", accn=q)],
                          "CommercialPaper": [fact(None, "2016-09-25", 1749e6, "2016-11-02", accn=k, form="10-K")],
                          "DebtCurrent": [fact(None, "2016-09-25", 1749e6, "2017-07-19", accn=q)]}, "2016-09-25")
        self.assertAlmostEqual(r["v"], 11757e6)

    def test_once_rule_is_stated(self):
        self.assertIn("counted once", F.DEBT_ONCE_RULE)
        self.assertIn("never collapsed", F.DEBT_ONCE_RULE)

    def test_different_amounts_same_filing_both_count(self):
        # control: the usual shape (AMZN-like) - current portion and separate short-term debt
        r = self.resolve({"LongTermDebtNoncurrent": [fact(None, "2026-09-30", 500e6, "2026-11-03")],
                          "LongTermDebtCurrent": [fact(None, "2026-09-30", 100e6, "2026-11-03")],
                          "ShortTermBorrowings": [fact(None, "2026-09-30", 40e6, "2026-11-03")]}, "2026-09-30")
        self.assertAlmostEqual(r["v"], 640e6)

    def test_equal_noncurrent_and_current_are_two_amounts(self):
        # control: an amortizing loan with equal current and noncurrent parts is not collapsed
        r = self.resolve({"LongTermDebtNoncurrent": [fact(None, "2026-09-30", 75e6, "2026-11-03")],
                          "LongTermDebtCurrent": [fact(None, "2026-09-30", 75e6, "2026-11-03")]}, "2026-09-30")
        self.assertAlmostEqual(r["v"], 150e6)

    def test_total_debt_holds_its_current_maturities(self):
        # LongTermDebt (total) + a ShortTermBorrowings fact equal to LongTermDebtCurrent in
        # the same filing: the current maturities are already inside the total
        r = self.resolve({"LongTermDebt": [fact(None, "2026-09-30", 900e6, "2026-11-03")],
                          "LongTermDebtCurrent": [fact(None, "2026-09-30", 200e6, "2026-11-03")],
                          "ShortTermBorrowings": [fact(None, "2026-09-30", 200e6, "2026-11-03")]}, "2026-09-30")
        self.assertAlmostEqual(r["v"], 900e6)

    def test_debt_current_contains_commercial_paper(self):
        # INTC / LLY shape: DebtCurrent is short-term debt INCLUDING commercial paper
        r = self.resolve({"LongTermDebtNoncurrent": [fact(None, "2025-06-28", 46000e6, "2025-08-01")],
                          "DebtCurrent": [fact(None, "2025-06-28", 4757e6, "2025-08-01")],
                          "CommercialPaper": [fact(None, "2025-06-28", 2000e6, "2025-08-01")]}, "2025-06-28")
        self.assertAlmostEqual(r["v"], 50757e6)
        self.assertIn("inside DebtCurrent", r["src"])

    def test_commercial_paper_larger_than_debt_current_still_counts(self):
        # control: a DebtCurrent smaller than the paper cannot contain it - both stay
        r = self.resolve({"LongTermDebtNoncurrent": [fact(None, "2025-06-28", 1000e6, "2025-08-01")],
                          "DebtCurrent": [fact(None, "2025-06-28", 50e6, "2025-08-01")],
                          "CommercialPaper": [fact(None, "2025-06-28", 300e6, "2025-08-01")]}, "2025-06-28")
        self.assertAlmostEqual(r["v"], 1350e6)

    def test_ifrs_total_current_replaces_its_part(self):
        f = facts_with({"LongtermBorrowings": ("EUR", [fact(None, "2025-12-31", 1800e6, "2026-03-01")]),
                        "CurrentPortionOfLongtermBorrowings": ("EUR", [fact(None, "2025-12-31", 150e6, "2026-03-01")]),
                        "CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings":
                            ("EUR", [fact(None, "2025-12-31", 240e6, "2026-03-01", accn="b")])}, tax="ifrs-full")
        r = F.debt_resolver(f, "ifrs-full", "EUR", "X")("2025-12-31")
        self.assertAlmostEqual(r["v"], 2040e6)
        self.assertIn("counted once", r["src"])

    def test_unknown_debt_means_no_ev(self):
        m = V.multiples({"rev": 100.0, "ni": 10.0, "ebitda": 20.0, "oi": 15.0, "fcf": 5.0, "cash": 1.0,
                         "debt_why": "debt not tagged"}, 1000.0, "operating")
        self.assertIsNone(m["ev_ebitda"])
        self.assertIsNone(m["ev_s"])
        self.assertEqual(m["pe"], 100.0)               # equity multiples still stand
        self.assertEqual(V.why_none("ev_ebitda", {"debt_why": "debt not tagged in XBRL for this balance sheet"},
                                    "operating"), "debt not tagged in XBRL for this balance sheet")

    def test_partial_inputs_never_flag(self):
        self.assertIn("ev_ebitda", V.partial_inputs({"end": "2026-06-30", "debt_asof": "2026-03-31"}))
        self.assertIn("ev_ebitda", V.partial_inputs({"da_partial": True}))
        m = V.metric_flags({"now": 5.0, "n": 1260, "pct": 1.0, "z": -3.0, "prem": -0.6, "partial": "x"}, "operating")
        self.assertEqual((m["own"], m["peer"]), (None, None))


class FreeCashFlow(unittest.TestCase):
    def _company(self, extra):
        q = lambda s, e, v, f: fact(s, e, v, f)
        base = {"Revenues": [q("2025-01-01", "2025-12-31", 4000e6, "2026-02-01")],
                "NetCashProvidedByUsedInOperatingActivities": [q("2025-01-01", "2025-12-31", 1000e6, "2026-02-01")],
                "PaymentsToAcquirePropertyPlantAndEquipment": [q("2025-01-01", "2025-12-31", 50e6, "2026-02-01")]}
        base.update(extra)
        cf = {"entityName": "X", "facts": facts_with({k: ("USD", v) for k, v in base.items()})}
        return F.build_company(cf, "X")["quarters"][-1]

    def test_capitalized_software_is_capex(self):
        r = self._company({"PaymentsToDevelopSoftware": [fact("2025-01-01", "2025-12-31", 100e6, "2026-02-01")]})
        self.assertAlmostEqual(r["capex"], 150e6)
        self.assertAlmostEqual(r["fcf"], 850e6)
        self.assertTrue(r["capex_incl_software"])

    def test_the_quarters_own_cash_flow_is_ytd_minus_the_prior_ytd(self):
        # 10-Qs report cash flow year to date; the fourth quarter is the year minus nine months.
        q = lambda s, e, v: fact(s, e, v, "2026-02-01")
        r = self._company({
            "Revenues": [q("2025-01-01", "2025-09-30", 2900e6), q("2025-01-01", "2025-12-31", 4000e6)],
            "NetCashProvidedByUsedInOperatingActivities": [q("2025-01-01", "2025-09-30", 1100e6), q("2025-01-01", "2025-12-31", 1000e6)],
            "PaymentsToAcquirePropertyPlantAndEquipment": [q("2025-01-01", "2025-09-30", 20e6), q("2025-01-01", "2025-12-31", 50e6)],
            "PaymentsToDevelopSoftware": [q("2025-01-01", "2025-09-30", 60e6), q("2025-01-01", "2025-12-31", 100e6)]})
        self.assertAlmostEqual(r["cfo_q"], -100e6)          # the year was positive, the quarter was not
        self.assertAlmostEqual(r["capex_q"], 70e6)          # 30 of plant + 40 of capitalized software
        self.assertAlmostEqual(r["fcf_q"], -170e6)
        self.assertAlmostEqual(r["fcf"], 850e6)             # the TTM line is unchanged

    def test_cfo_mostly_customer_advances_marks_fcf_partial(self):
        r = self._company({"IncreaseDecreaseInContractWithCustomerLiability":
                           [fact("2025-01-01", "2025-12-31", 900e6, "2026-02-01")]})
        self.assertEqual(r["cfo_advances"], 0.9)
        p = V.partial_inputs(r)
        self.assertIn("p_fcf", p)
        self.assertIn("customer advances", p["p_fcf"])

    def test_ordinary_cfo_is_not_partial(self):
        r = self._company({"IncreaseDecreaseInContractWithCustomerLiability":
                           [fact("2025-01-01", "2025-12-31", 100e6, "2026-02-01")]})
        self.assertNotIn("cfo_advances", r)


class HistoryDropsUnknownDebt(NoLookAhead):
    def test_history_point_with_unknown_debt_is_dropped(self):
        self.rec["quarters"][0].pop("debt")
        self.rec["quarters"][0]["debt_why"] = "debt not tagged"
        d, s, info = V.series_for("X", self.rec, self.con, {})
        self.assertIsNone(s["ev_ebitda"][0])          # the Dec-quarter sessions: no EV, not debt = 0
        self.assertIsNotNone(s["ps"][0])
        self.assertIsNotNone(s["ev_ebitda"][2])


class Flags(unittest.TestCase):
    def rv(self, own, prem, holds=True):
        M = {}
        for k in ("pe", "ev_s", "ev_ebitda", "p_fcf"):
            pct = {"cheap": 5.0, "rich": 95.0, None: 50.0}[own]
            M[k] = V.metric_flags({"now": 10.0, "n": 1260, "pct": pct, "z": 0.0, "med": 15.0, "window": "5-year history",
                                   "peer_med": 10.0 / (1 + prem), "prem": prem}, "operating")
        return {"kind": "operating", "metrics": M, "growth": {"rev_g": 0.1, "om_chg": 1.0, "holds": holds}}

    def test_opportunity(self):
        f, _ = V.name_flags("X", self.rv("cheap", -0.4), ["A", "B"])
        self.assertEqual(f[0]["kind"], "OPPORTUNITY")

    def test_value_trap(self):
        f, _ = V.name_flags("X", self.rv("cheap", -0.4, holds=False), ["A", "B"])
        self.assertEqual(f[0]["kind"], "VALUE-TRAP WATCH")

    def test_stretched(self):
        f, _ = V.name_flags("X", self.rv("rich", 0.5), ["A", "B"])
        self.assertEqual(f[0]["kind"], "STRETCHED")

    def test_dislocation_own_only(self):
        f, _ = V.name_flags("X", self.rv("cheap", 0.0), ["A", "B"])
        self.assertEqual(f[0]["kind"], "DISLOCATION")

    def test_quiet(self):
        f, _ = V.name_flags("X", self.rv(None, 0.1), ["A", "B"])
        self.assertEqual(f, [])

    def test_words_are_states_not_instructions(self):
        for own, prem, holds in (("cheap", -0.4, True), ("cheap", -0.4, False), ("rich", 0.5, True), ("cheap", 0.0, True)):
            for fl in V.name_flags("X", self.rv(own, prem, holds), ["A", "B"])[0]:
                low = fl["text"].lower()
                for w in (" buy", " sell", "should", " will ", "recommend", "target"):
                    self.assertNotIn(w, low)

    def test_stale_names_never_flag(self):
        rv = self.rv("cheap", -0.4)
        rv["stale"] = True
        self.assertEqual(V.name_flags("X", rv, ["A", "B"])[0], [])

    def test_peer_gap_alone_is_not_a_flag(self):
        # Jake 2026-11-04 ("Ship it"): a gap to peers alone never reaches a flag line or
        # the cross-name list; it stays on the metric as information.
        for prem in (-0.7, 0.9):
            rv = self.rv(None, prem)
            self.assertEqual(V.name_flags("X", rv, ["A", "B"])[0], [])
            self.assertEqual(rv["metrics"]["ev_ebitda"]["peer"], "cheap" if prem < 0 else "rich")
            self.assertEqual(rv["metrics"]["ev_ebitda"]["prem"], prem)

    def test_flag_kinds_are_the_four(self):
        self.assertEqual(set(V.KIND_ORDER), {"OPPORTUNITY", "VALUE-TRAP WATCH", "STRETCHED", "DISLOCATION"})
        self.assertNotIn("peer_gap_dislocation", V.THRESHOLDS)
        for own, prem, holds in (("cheap", -0.4, True), ("cheap", -0.4, False), ("rich", 0.5, True),
                                 ("cheap", 0.0, True), (None, -0.8, True), (None, 0.8, True)):
            for fl in V.name_flags("X", self.rv(own, prem, holds), ["A", "B"])[0]:
                self.assertIn(fl["kind"], V.FLAG_KINDS)


class DebtOverride(unittest.TestCase):
    """Hand-entered debt from the filing (CAT, HUBS, ZS; 2026-11-04)."""

    def setUp(self):
        self._saved = F.DEBT_OVERRIDE
        F.DEBT_OVERRIDE = dict(self._saved, X={"2026-09-30": {"v": 700.0, "form": "10-Q", "accession": "0000000000-26-000001",
                                                             "items": [("Short-term borrowings", 200.0), ("Long-term debt", 500.0)]}})

    def tearDown(self):
        F.DEBT_OVERRIDE = self._saved

    def resolve(self, end):
        f = facts_with({"LongTermDebtNoncurrent": ("USD", [fact(None, "2026-06-30", 300e6, "2026-08-01")])})
        return F.debt_resolver(f, "us-gaap", "USD", "X")(end)

    def test_override_at_its_own_date_is_used_and_labelled(self):
        r = self.resolve("2026-09-30")
        self.assertAlmostEqual(r["v"], 700e6)
        self.assertTrue(r["src"].startswith("filing (hand-entered)"))
        self.assertIn("0000000000-26-000001", r["src"])
        self.assertFalse(r.get("stale"))
        # at its own date it is not partial: it may flag
        self.assertEqual(V.partial_inputs({"end": "2026-09-30", "debt": 700e6, "debt_src": r["src"]}), {})

    def test_override_at_a_later_date_is_partial(self):
        r = self.resolve("2026-12-31")
        self.assertEqual((r["v"], r["asof"], r.get("stale")), (700e6, "2026-09-30", True))
        self.assertIn("ev_ebitda", V.partial_inputs({"end": "2026-12-31", "debt_asof": r["asof"]}))

    def test_tag_dates_without_override_still_use_tags(self):
        self.assertAlmostEqual(self.resolve("2026-06-30")["v"], 300e6)

    def test_entries_are_cited_and_sum_to_their_items(self):
        for tk in ("CAT", "HUBS", "ZS"):
            self.assertIn(tk, self._saved)
            for end, o in self._saved[tk].items():
                self.assertRegex(end, r"^\d{4}-\d{2}-\d{2}$")
                self.assertRegex(o["accession"], r"^\d{10}-\d{2}-\d{6}$")
                self.assertTrue(o["form"].startswith(("10-Q", "10-K")))
                self.assertTrue(o["items"])
                self.assertAlmostEqual(o["v"], sum(v for _, v in o["items"]), places=3)

    def test_cat_includes_current_portion_of_long_term_debt(self):
        o = self._saved["CAT"]["2025-12-31"]
        self.assertTrue(any("due within one year" in n for n, _ in o["items"]))
        self.assertGreater(o["v"], 36210.0)   # the XBRL figure that left it out

    def test_hubs_zero_is_cited_not_defaulted(self):
        for o in self._saved["HUBS"].values():
            self.assertEqual(o["v"], 0.0)
            self.assertTrue(o["accession"] and o["items"][0][0])


class GoldPeers(unittest.TestCase):
    ROYALTY = {"FNV", "WPM", "RGLD", "TFPM"}
    MINERS = {"NEM", "GOLD", "AU", "AEM", "KGC", "AGI", "HL", "PAAS", "AG", "CDE", "EXK", "SSRM"}

    def test_royalty_and_miners_never_mix(self):
        ov = V.load_peers()["overrides"]
        for grp, other in ((self.ROYALTY, self.MINERS), (self.MINERS, self.ROYALTY)):
            for tk in grp:
                self.assertIn(tk, ov, tk)
                ps = set(ov[tk]["peers"])
                self.assertTrue(ps and ps <= grp - {tk}, (tk, ps))
                self.assertFalse(ps & other)
                self.assertTrue(ov[tk].get("why"))


if __name__ == "__main__":
    unittest.main()
