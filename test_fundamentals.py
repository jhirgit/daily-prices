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


if __name__ == "__main__":
    unittest.main()
