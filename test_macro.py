#!/usr/bin/env python3
"""Tests for macro.py / macro_reads.py (build M1). Offline: every response comes from data/fixtures/macro/
(seeded from recon-2's saved samples) or is built in the test; a request the router does not know fails.

Covers, per adapter: the fixture parses, and a "source changed shape" response is an error -- never a zero.
And the six silent breaks recon-2 found in 2025-26, each of which must now be loud:
  1. Eurostat/ECB HICP keys that stopped at 2025-12 (empty answer = error; a key whose data end = stale)
  2. October 2025 CPI / household survey not collected (change left blank with the reason, never interpolated)
  3. the Daily Treasury Statement customs category renamed (no regex match = error)
  4. the Chicago Fed's own NFCI file froze at 2026-04-24 (a series that stops advancing = stale)
  5. Atlanta Fed file URLs moved (HTTP 404 = error naming the cause)
  6. generic ZQ=F pointed at January 2027 (contract symbols generated from the date; ZQ=F never used)
plus: staleness judged against the release calendar (the six series a days-elapsed test mis-flagged), store:false
series never fetched or written, and the payload text passes the public repo's guard words and the board's
no-advice-words rule.

Run:  python test_macro.py
"""
import datetime as dt
import io
import json
import os
import re
import sys
import unittest
import urllib.error
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "data", "fixtures", "macro")
sys.path.insert(0, HERE)
import macro as M          # noqa: E402
import macro_reads as R    # noqa: E402
import test_guard_words    # noqa: E402

TODAY = dt.date(2026, 11, 20)


def fx(name):
    with open(os.path.join(FIX, name), "rb") as fh:
        return fh.read()


def ctx_for(router, today=TODAY):
    return M.Ctx(M.Http(offline=router, pace=0), today, keys={})


def one(body):
    return lambda url: body


def xlsx(sheets):
    """A minimal .xlsx: {sheet name: [[cell, ...], ...]} with inline strings."""
    buf = io.BytesIO()
    z = zipfile.ZipFile(buf, "w")
    wb = ['<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>']
    rels = ['<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
    for i, (name, rows) in enumerate(sheets.items(), 1):
        wb.append('<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (name, i, i))
        rels.append('<Relationship Id="rId%d" Target="worksheets/sheet%d.xml"/>' % (i, i))
        x = ['<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']
        for r, row in enumerate(rows, 1):
            x.append('<row r="%d">' % r)
            for c, v in enumerate(row):
                ref = "%s%d" % (chr(65 + c), r)
                if v is None:
                    continue
                if isinstance(v, (int, float)):
                    x.append('<c r="%s"><v>%s</v></c>' % (ref, v))
                else:
                    x.append('<c r="%s" t="inlineStr"><is><t>%s</t></is></c>' % (ref, v))
            x.append('</row>')
        x.append('</sheetData></worksheet>')
        z.writestr("xl/worksheets/sheet%d.xml" % i, "".join(x))
    wb.append('</sheets></workbook>')
    rels.append('</Relationships>')
    z.writestr("xl/workbook.xml", "".join(wb))
    z.writestr("xl/_rels/workbook.xml.rels", "".join(rels))
    z.close()
    return buf.getvalue()


CAT = M.load_catalogue()
BY_ID = {e["id"]: e for e in CAT["series"]}


def entry(i, **over):
    e = dict(BY_ID[i])
    e.update(over)
    return e


class Catalogue(unittest.TestCase):
    def test_loads_and_is_consistent(self):
        self.assertGreater(len(CAT["series"]), 100)
        for v in CAT["views"]:
            for t in v["tests"]:
                if not t["series"].startswith("@"):
                    self.assertIn(t["series"], BY_ID, v["id"])
                self.assertIn(t["op"], (">", "<"))
                # with '>' the confirm line sits above the refute line, with '<' below it
                if t["op"] == ">":
                    self.assertGreaterEqual(t["confirm"], t["refute"], t["label"])
                else:
                    self.assertLessEqual(t["confirm"], t["refute"], t["label"])
        self.assertEqual(len(CAT["views"]), 7)
        self.assertEqual(len(CAT["reads"]), 8)

    def test_malformed_catalogue_is_the_only_hard_failure(self):
        bad = os.path.join(HERE, "data", "fixtures", "macro", "_bad_catalogue.json")
        with open(bad, "w", encoding="utf-8") as fh:
            json.dump({"series": [{"id": "x"}]}, fh)
        try:
            self.assertEqual(M.main(["--catalogue", bad, "--out-dir", FIX, "--offline", FIX]), 2)
        finally:
            os.remove(bad)


class Fred(unittest.TestCase):
    def test_single_frequency_csv(self):
        got = M.parse_fredgraph(fx("fred_monthly.csv"), ["CPIAUCSL", "CPILFESL"])
        core = dict(got["CPILFESL"])
        self.assertIsNone(core["2025-10-01"])                     # the shutdown month is blank, kept as None
        self.assertAlmostEqual(core["2025-09-01"], 330.418)

    def test_mixed_frequency_zip(self):
        got = M.parse_fredgraph(fx("fred_mixed.zip"), ["PAYEMS", "UNRATE", "ICSA"])
        self.assertTrue(got["PAYEMS"] and got["ICSA"])

    def test_shape_change_is_an_error(self):
        with self.assertRaises(M.SourceError):
            M.parse_fredgraph(b"<html><body>Service unavailable</body></html>", ["PAYEMS"])
        with self.assertRaises(M.SourceError):                     # a renamed id: no column comes back
            M.parse_fredgraph(b"observation_date,OTHER\n2026-01-01,1\n", ["PAYEMS"])

    def test_batch_that_drops_ids_refetches_them_alone(self):
        calls = []

        def router(url):
            calls.append(url)
            if "," in url.split("id=")[1].split("&")[0]:
                return b"observation_date,UNRATE\n2026-09-01,4.2\n2026-10-01,4.1\n"   # the batch carried one of two
            return b"observation_date,PAYEMS\n2026-09-01,159128\n2026-10-01,159293\n"
        c = ctx_for(router)
        c.fred_prefetch([entry("unrate"), entry("payems")])
        self.assertEqual(M.a_fred(entry("payems"), c)[-1], ("2026-10", 159293.0))
        self.assertEqual(len(calls), 2)


class FiscalData(unittest.TestCase):
    def test_customs_monthly(self):
        obs = M.a_fiscaldata(entry("customs_refunds"), ctx_for(one(fx("fd_mts9_customs.json"))))
        self.assertEqual(obs[-1][0], "2026-10")
        self.assertAlmostEqual(obs[-1][1], 6734.0446, places=3)

    def test_tga(self):
        obs = M.a_fiscaldata(entry("tga"), ctx_for(one(fx("fd_dts_cash.json"))))
        self.assertEqual(obs[-1], ("2026-11-19", 892310.0))

    def test_interest_sums_categories(self):
        obs = M.a_fiscaldata(entry("interest_public"), ctx_for(one(fx("fd_interest_expense.json"))))
        self.assertGreater(obs[-1][1], 50000)

    def test_dts_customs_matched_by_regex(self):
        obs = M.a_fiscaldata(entry("customs_daily"), ctx_for(one(fx("fd_dts_deposits.json"))))
        self.assertTrue(obs)

    def test_silent_break_3_dts_category_renamed(self):
        d = json.loads(fx("fd_dts_deposits.json"))
        d["data"] = [r for r in d["data"] if "ustoms" not in r["transaction_catg"]]
        with self.assertRaisesRegex(M.SourceError, "renamed"):
            M.a_fiscaldata(entry("customs_daily"), ctx_for(one(json.dumps(d).encode())))

    def test_shape_change_is_an_error(self):
        with self.assertRaisesRegex(M.SourceError, "shape"):
            M.a_fiscaldata(entry("tga"), ctx_for(one(b'{"rows": []}')))
        with self.assertRaisesRegex(M.SourceError, "no field"):
            M.a_fiscaldata(entry("tga"), ctx_for(one(b'{"data": [{"record_date": "2026-11-19", "balance": "1"}]}')))


class Eurostat(unittest.TestCase):
    def test_parses(self):
        obs = M.a_eurostat(entry("ea_unemp"), ctx_for(one(fx("es_unemp_ea21.json"))))
        self.assertTrue(obs and obs[-1][1] is not None)

    def test_silent_break_1_empty_answer_is_an_error_not_a_zero(self):
        j = json.loads(fx("es_unemp_ea21.json"))
        j["value"] = {}
        with self.assertRaisesRegex(M.SourceError, "no observations"):
            M.a_eurostat(entry("ea_unemp"), ctx_for(one(json.dumps(j).encode())))

    def test_shape_change_is_an_error(self):
        with self.assertRaisesRegex(M.SourceError, "shape"):
            M.a_eurostat(entry("ea_unemp"), ctx_for(one(b'{"label": "x", "value": {"0": 1}}')))


class Sdmx(unittest.TestCase):
    def test_ecb_bis_oecd(self):
        self.assertTrue(M.a_sdmx_csv(entry("estr"), ctx_for(one(fx("ecb_estr.csv")))))
        self.assertEqual(M.a_sdmx_csv(entry("bis_reer_us"), ctx_for(one(fx("bis_eer.csv"))))[-1], ("2026-10", 108.89))
        self.assertTrue(M.a_sdmx_csv(entry("oecd_cli_chn"), ctx_for(one(fx("oecd_cli.csv")))))

    def test_shape_change_is_an_error(self):
        with self.assertRaisesRegex(M.SourceError, "TIME_PERIOD"):
            M.a_sdmx_csv(entry("estr"), ctx_for(one(b"<html>No results found</html>")))

    def test_silent_break_1_a_key_whose_data_end_reads_stale(self):
        # the dead ECB HICP key ICP/M.U2.N.000000.4.ANR: answers, but its data stop at 2025-12
        body = b"KEY,TIME_PERIOD,OBS_VALUE\nICP.M.U2.N.000000.4.ANR,2025-11,2.1\nICP.M.U2.N.000000.4.ANR,2025-12,2.0\n"
        e = entry("ea_hicp", source="sdmx_csv")
        obs = M.a_sdmx_csv(e, ctx_for(one(body)))
        rec = M.make_record(e, obs, TODAY, CAT, {}, {})
        self.assertEqual(rec["status"], "stale")
        self.assertIn("2025-12", rec["reason"])


class Cleveland(unittest.TestCase):
    def test_parses_and_names_the_month(self):
        e = entry("nowcast_core_pce")
        obs = M.a_cleveland(e, ctx_for(one(fx("cle_nowcast_month.json"))))
        self.assertEqual(len(obs), 1)
        self.assertEqual(e["_extra"]["nowcast_month"], "2026-11")

    def test_missing_series_name_is_an_error(self):
        j = json.loads(fx("cle_nowcast_month.json"))
        for ds in j[-1]["dataset"]:
            ds["seriesname"] = ds["seriesname"].replace("Core PCE", "Core PCE (new)")
        with self.assertRaisesRegex(M.SourceError, "no series"):
            M.a_cleveland(entry("nowcast_core_pce"), ctx_for(one(json.dumps(j).encode())))

    def test_shape_change_is_an_error(self):
        with self.assertRaisesRegex(M.SourceError, "shape"):
            M.a_cleveland(entry("nowcast_cpi"), ctx_for(one(b'{"data": []}')))


class Atlanta(unittest.TestCase):
    TAYLOR = {"FOMCTaylor99UR": [[None, "Effective fed funds rate, lagged 1-quarter"],
                                 [None, "FedFundsRateLag1", None, None, None, None, None, "FOMCTaylor99UR:  pi=Core PCE"],
                                 [46249.0, 3.63, 1.18, 2.0, 0.03, 3.05, 46249.0, 4.796, 3.67],
                                 [46341.0, 3.67, 1.25, 2.0, 0.03, 3.27, 46341.0, 5.1925, "#N/A"]]}

    def test_taylor(self):
        obs = M.a_atlanta(entry("atl_taylor"), ctx_for(one(xlsx(self.TAYLOR))))
        self.assertEqual(obs[-1], ("2026-Q4", 5.1925))

    def test_wage_tracker_column_by_header(self):
        book = xlsx({"data_overall": [["Sources: ..."], [None, "Services", "Overall"], [46266.0, 4.1, 3.9], [None, None, None]]})
        self.assertEqual(M.a_atlanta(entry("atl_wgt"), ctx_for(one(book)))[-1], ("2026-09", 3.9))

    def test_layout_change_is_an_error(self):
        t = {"FOMCTaylor99UR": [r[:] for r in self.TAYLOR["FOMCTaylor99UR"]]}
        t["FOMCTaylor99UR"][1][7] = "SomethingElse"
        with self.assertRaisesRegex(M.SourceError, "layout"):
            M.a_atlanta(entry("atl_taylor"), ctx_for(one(xlsx(t))))
        with self.assertRaisesRegex(M.SourceError, "no sheet"):
            M.a_atlanta(entry("atl_wgt"), ctx_for(one(xlsx({"renamed": [[1]]}))))

    def test_silent_break_5_moved_file_is_a_loud_404(self):
        orig = M.urllib.request.urlopen

        def gone(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
        M.urllib.request.urlopen = gone
        try:
            h = M.Http(pace=0)
            with self.assertRaisesRegex(M.SourceError, "404.*moved"):
                M.a_atlanta(entry("atl_wgt"), M.Ctx(h, TODAY, keys={}))
        finally:
            M.urllib.request.urlopen = orig

    def test_bot_wall_is_not_retried(self):
        orig, n = M.urllib.request.urlopen, []

        def wall(req, timeout=None):
            n.append(1)
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)
        M.urllib.request.urlopen = wall
        try:
            with self.assertRaisesRegex(M.SourceError, "not worked around"):
                M.Http(pace=0).get("https://example.org/x")
            self.assertEqual(len(n), 1)
        finally:
            M.urllib.request.urlopen = orig


class Census(unittest.TestCase):
    def test_data_center_column(self):
        obs = M.a_census(entry("dc_construction"), ctx_for(one(fx("census_c30_privsatime.xlsx"))))
        self.assertEqual(obs[-1], ("2026-09", 96462.0))

    def test_layout_change_is_an_error(self):
        book = xlsx({"Private SA": [["Date", "Total", "Data centre"], ["Sep-26p", 1.0, 2.0]]})
        with self.assertRaisesRegex(M.SourceError, "layout"):
            M.a_census(entry("dc_construction"), ctx_for(one(book)))
        with self.assertRaisesRegex(M.SourceError, "xlsx"):
            M.a_census(entry("dc_construction"), ctx_for(one(b"<html>maintenance</html>")))


class Futures(unittest.TestCase):
    def test_silent_break_6_contract_months_from_the_date(self):
        syms = M.zq_symbols(TODAY)
        self.assertEqual(syms[0], ("2026-11", "ZQX26.CBT"))
        self.assertEqual(syms[2], ("2027-01", "ZQF27.CBT"))
        self.assertEqual(len(syms), 13)
        self.assertNotIn("ZQ=F", [s for _, s in syms])
        with open(os.path.join(HERE, "macro.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotRegex(re.sub(r"(?m)#.*$|\"\"\"[\s\S]*?\"\"\"", "", src), r"['\"]ZQ=F['\"]")

    def test_strip_and_failures(self):
        ok = M.fed_path(TODAY, quote=lambda s: ("2026-11-20", 96.0), pause=0)
        self.assertEqual(ok["status"], "ok")
        self.assertEqual(ok["contracts"][0]["implied"], 4.0)

        def dead(s):
            raise RuntimeError("no bars")
        bad = M.fed_path(TODAY, quote=dead, pause=0, attempts=1)
        self.assertEqual(bad["status"], "error")
        old = M.fed_path(TODAY, quote=lambda s: ("2026-11-02", 96.0), pause=0)
        self.assertEqual(old["status"], "stale")


class Changes(unittest.TestCase):
    def test_silent_break_2_october_2025_gap(self):
        obs = [("2025-%02d" % m, 320.0 + m) for m in range(1, 13)] + [("2026-%02d" % m, 330.0 + m) for m in range(1, 11)]
        obs = [(p, None if p == "2025-10" else v) for p, v in obs]
        f, notes = M.compute_changes(entry("cpiaucsl"), obs, CAT["gaps"])
        self.assertIsNone(f["yoy"])
        self.assertTrue(any("not collected" in n and "never interpolated" in n for n in notes))
        self.assertIsNotNone(f["ann3m"])            # Jul-Oct 2026 needs no 2025 month
        u = [(p, None if p == "2025-10" else 4.0) for p, _ in obs]
        f, notes = M.compute_changes(entry("unrate"), u, CAT["gaps"])
        self.assertIsNone(f["yoy"])
        self.assertTrue(any("household survey" in n for n in notes))

    def test_flow_sums_and_sign(self):
        obs = [("2025-%02d" % m, -100.0) for m in range(1, 13)] + [("2026-%02d" % m, -150.0) for m in range(1, 13)]
        f, notes = M.compute_changes(entry("mtsds133fms"), obs, {})
        self.assertEqual(f["extra"]["sum12"], -1800.0)
        self.assertEqual(f["extra"]["sum12_chg"], -600.0)
        self.assertIsNone(f["extra"]["sum12_yoy"])
        self.assertIsNone(f["yoy"])


class Staleness(unittest.TestCase):
    def setUp(self):
        events, _ = M.build_calendar(CAT, M.Http(offline=lambda u: fx("bea_schedule.json") if "bea.gov" in u else fx("fiscaldata_calendar.json"), pace=0), TODAY)
        self.dates = M.release_dates(events)

    def test_calendar_not_days_decides(self):
        # 2026-11-20: PCE (Sep), core capex orders and data-centre construction look old by days elapsed,
        # but their next releases have not happened yet -- ok, judged by the calendar.
        for sid, asof in [("pcepilfe", "2026-09"), ("pcetrim12m159sfrbdal", "2026-09"), ("dc_construction", "2026-09"), ("gdp", "2026-Q3")]:
            st, why, rel, nxt, how = M.judge(entry(sid), asof, TODAY, CAT["releases"][entry(sid)["release"]], self.dates)
            self.assertEqual((st, how), ("ok", "calendar"), (sid, why))
        st, *_ = M.judge(entry("pcepilfe"), "2026-09", TODAY, CAT["releases"]["bea_pio"], self.dates)
        self.assertEqual(st, "ok")

    def test_missed_release_is_stale(self):
        later = dt.date(2026, 11, 27)            # BEA released October PCE on 2026-11-25; we still hold September
        st, why, *_ = M.judge(entry("pcepilfe"), "2026-09", later, CAT["releases"]["bea_pio"], self.dates)
        self.assertEqual(st, "stale")
        self.assertIn("2026-11-25", why)
        self.assertIn("2026-10", why)

    def test_gdp_estimates_do_not_count_as_new_quarters(self):
        dec = dt.date(2026, 12, 28)              # second and third Q3 estimates passed; Q4's advance is in January
        st, *_ = M.judge(entry("gdp"), "2026-Q3", dec, CAT["releases"]["bea_gdp"], self.dates)
        self.assertEqual(st, "ok")

    def test_silent_break_4_frozen_series_is_stale(self):
        obs = [("2026-04-%02d" % d, -0.4) for d in (3, 10, 17, 24)]
        rec = M.make_record(entry("nfci"), obs, TODAY, CAT, self.dates, {})
        self.assertEqual((rec["status"], rec["judged_by"]), ("stale", "days"))
        self.assertIn("2026-04-24", rec["reason"])

    def test_calendar_sources_degrade_loudly(self):
        events, src = M.build_calendar(CAT, M.Http(offline=lambda u: b'{"renamed": {}}', pace=0), TODAY)
        self.assertEqual(src["bea"]["status"], "error")
        self.assertEqual(src["fiscaldata"]["status"], "error")


class EndToEnd(unittest.TestCase):
    """A small catalogue through build(): one series per adapter, plus a store:false series that must never be fetched."""

    def router(self, url):
        self.urls.append(url)
        if "fredgraph" in url:
            return fx("fred_monthly.csv")
        if "bea.gov" in url:
            return fx("bea_schedule.json")
        if "services/calendar/release" in url:
            return fx("fiscaldata_calendar.json")
        if "mts_table_4" in url:
            return fx("fd_mts9_customs.json")
        if "stats.bis.org" in url:
            return fx("bis_eer.csv")
        if "clevelandfed" in url:
            return fx("cle_nowcast_month.json")
        if "census.gov" in url:
            return fx("census_c30_privsatime.xlsx")
        raise M.SourceError("offline: no fixture for %s" % url)

    def test_build(self):
        self.urls = []
        keep = {"cpiaucsl", "cpilfesl", "medcpim158sfrbcle", "customs_refunds", "bis_reer_us", "nowcast_core_pce", "dc_construction", "mich"}
        cat = dict(CAT, series=[e for e in CAT["series"] if e["id"] in keep])
        out = os.path.join(FIX, "_out")
        os.makedirs(out, exist_ok=True)
        try:
            macro, hist, rel = M.build(cat, M.Http(offline=self.router, pace=0), TODAY, out_dir=out,
                                       quote=lambda s: ("2026-11-20", 96.0), log=lambda *a: None)
        finally:
            for f in os.listdir(out):
                os.remove(os.path.join(out, f))
            os.rmdir(out)
        self.assertEqual(macro["series"]["cpiaucsl"]["status"], "ok")
        self.assertIsNone(macro["series"]["cpiaucsl"]["yoy"])
        self.assertEqual(set(macro["reads"]), {d["id"] for d in CAT["reads"]})
        self.assertEqual(len(macro["views"]), 7)
        # store:false: not fetched, not in any payload
        self.assertNotIn("mich", macro["series"])
        self.assertNotIn("mich", hist["series"])
        self.assertIn("mich", macro["not_published"]["ids"])
        self.assertFalse(any("MICH" in u for u in self.urls))
        blob = json.dumps([macro, hist, rel])
        for e in CAT["series"]:
            if not e["store"]:
                self.assertNotIn('"%s"' % e["id"], json.dumps([macro["series"], hist]))
        self.payload_text_rules(macro)

    def payload_text_rules(self, macro):
        # 1. the public repo's guard: a guard word on the same line as a figure (tools/sensitive-data-guard.sh)
        word = re.compile("(" + "|".join(test_guard_words.GENERIC) + ")", re.I)
        fig = re.compile(r"\$[0-9]|[0-9][0-9,.]*\s*(shares|%)|[0-9][0-9,.]*[kKmM]\s|[0-9][0-9,.]*[kKmM]$")
        for line in json.dumps(macro, indent=1, ensure_ascii=False).splitlines():
            self.assertFalse(word.search(line) and fig.search(line), line)
        # 2. the board's rendered-text rule: no advice words
        advice = re.compile(r"\b(buy|sell|should|will|likely|target|recommend|hold|trim|add)\b", re.I)
        texts = []
        for r in macro["series"].values():
            texts += [r["name"], r["what"]] + (r.get("notes") or [])
        for d in macro["reads"].values():
            texts += [d["title"], d["formula"], d["limits"], d.get("unit") or ""] + [x["label"] for x in d.get("rows") or []]
        for v in macro["views"]:
            texts += [v["title"], v["claim"], v["side_text"], v["line"], v.get("note") or "", v.get("missing") or ""] + [t["label"] for t in v["tests"]]
        for t in texts:
            self.assertIsNone(advice.search(t or ""), t)


class CatalogueText(unittest.TestCase):
    def test_every_label_in_the_catalogue(self):
        advice = re.compile(r"\b(buy|sell|should|will|likely|target|recommend|hold|trim|add)\b", re.I)
        word = re.compile("(" + "|".join(test_guard_words.GENERIC) + ")", re.I)
        for e in CAT["series"]:
            for t in (e["name"], e["what"]):
                self.assertIsNone(advice.search(t), t)
                self.assertIsNone(word.search(t), t)
        for v in CAT["views"]:
            for t in [v["title"], v["claim"], v.get("note") or "", v.get("missing") or ""] + [x["label"] for x in v["tests"]]:
                self.assertIsNone(advice.search(t), t)
        for d in CAT["reads"]:
            for t in (d["title"], d["formula"], d["limits"]):
                self.assertIsNone(advice.search(t), t)


class Views(unittest.TestCase):
    def test_side_rule(self):
        self.assertEqual(R.side_of(["with", "with", "neither"]), "with")
        self.assertEqual(R.side_of(["with", "neither"]), "lean_with")
        self.assertEqual(R.side_of(["with", "against"]), "mixed")
        self.assertEqual(R.side_of(["neither", "neither"]), "neutral")
        self.assertEqual(R.side_of(["against", "no data", "no data"]), "insufficient")
        self.assertEqual(R.side_of(["against", "against", "with", "against"]), "against")

    def test_thresholds(self):
        t = {"op": "<", "confirm": 0.0, "refute": 10.0}
        self.assertEqual([R.sits(t, x) for x in (-1, 5, 11, None)], ["with", "neither", "against", "no data"])

    def test_error_series_never_places_a_view(self):
        series = {"t5yifr": {"value": 3.0, "status": "error", "asof": "2026-11-01", "name": "x"}}
        out = R.compute_views(CAT, series, {})
        fd = next(v for v in out if v["id"] == "fiscal_dominance")
        self.assertEqual(fd["tests"][0]["sits"], "no data")


class HealthRow(unittest.TestCase):
    def test_health_reports_macro(self):
        import shutil
        import tempfile
        import health as H
        d = tempfile.mkdtemp(prefix="health_macro_")
        try:
            with open(os.path.join(d, "latest.json"), "w", encoding="utf-8") as fh:
                json.dump({"session": "2026-11-20", "tickers": []}, fh)
            h = H.build(d, os.path.join(HERE, "tickers.txt"), steps_env="macro=success")
            self.assertIn("macro.json missing", h["degraded"])            # the step ran, the file is not there
            with open(os.path.join(d, "macro.json"), "w", encoding="utf-8") as fh:
                json.dump({"asof": "2026-11-19", "counts": {"ok": 1, "error": 1},
                           "degraded": ["core series dgs2: error"], "series": {"dgs2": {"status": "error"}, "unrate": {"status": "ok"}}}, fh)
            h = H.build(d, os.path.join(HERE, "tickers.txt"), steps_env="")
            self.assertEqual(h["payloads"]["macro.json"]["errors"]["not_ok"], ["dgs2"])
            self.assertIn("macro.json: core series dgs2: error", h["degraded"])
            self.assertIn("macro.json speaks for 2026-11-19, behind the 2026-11-20 session", h["degraded"])
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
