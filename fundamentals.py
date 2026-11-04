"""Quarterly TTM fundamentals from SEC companyfacts -> data/fundamentals.json.

jr-dash: the Name tab's "Valuation multiples" section (SPEC-78 amendment, 2026-11-04).
valuation.py is the only consumer; the board reads valuation.json, never this file.

What it does, per name with a CIK (data/cik_map.json, SEC's own ticker map):
  * pulls companyfacts (sec_facts.py: cached, <= 5 req/s, SEC User-Agent);
  * builds QUARTERLY values for every flow concept: a 3-month fact as reported, or a
    derived one -- a longer period minus the period with the SAME start that ends ~3
    months earlier (Q4 = FY - 9M, Q3 = 9M - 6M, Q2 = 6M - Q1). Cash-flow statements
    are YTD-only in 10-Qs, so this is how CFO / capex / SBC / D&A get quarters at all;
  * TTM at each quarter end = the fiscal-year fact when the quarter closes a fiscal
    year, else the sum of four consecutive quarters;
  * restatements: the same (start, end) reported by several filings keeps the value
    from the LATEST filing (as-restated), and the date it became known is the FIRST
    filing that carried it. That date is what valuation.py keys "no look-ahead" on;
  * balance sheet (instants): cash + short-term marketable securities, total debt
    (long-term noncurrent + current portion + commercial paper / short-term borrowings;
    operating leases EXCLUDED), stockholders' equity;
  * shares outstanding as ORIGINALLY reported by each filing (point-in-time), from the
    dei cover (classes summed within a filing) or us-gaap CommonStockSharesOutstanding,
    split-adjusted to today's basis because daily-prices' closes are split-adjusted.

Honest n/a, never a fake number: banks and insurers (EBITDA / EV are not meaningful;
they get P/E and P/B only), ETFs / trusts / sleeves, foreign listings with no SEC
filings, and names whose filings carry no revenue line.

    python fundamentals.py                 # all names, network (cache reused < 20h)
    python fundamentals.py --offline       # cache only
    python fundamentals.py --tickers GOOGL,NVDA --stdout
"""
import argparse
import datetime as dt
import json
import os
import re
import sys

import sec_facts

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT = os.path.join(DATA, "fundamentals.json")
KEEP_QUARTERS = 48            # 12 years of quarter ends: 10y of history + the TTM lag

# ---- period shapes (days) ---------------------------------------------------------
Q_MIN, Q_MAX = 80, 100        # a quarter (13 or 14 weeks, or a calendar quarter)
FY_MIN, FY_MAX = 350, 380     # a fiscal year (52/53 weeks or calendar)

# ---- concepts, in priority order (first one that has the period wins) ----------
FLOW = {
    "rev": {"us-gaap": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                        "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet",
                        "SalesRevenueGoodsNet", "SalesRevenueServicesNet", "RevenuesNetOfInterestExpense",
                        "RevenueMineralSales"],
            "ifrs-full": ["Revenue", "RevenueFromContractsWithCustomers"]},
    "cogs": {"us-gaap": ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold",
                         "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization"],
             "ifrs-full": ["CostOfSales"]},
    "gp": {"us-gaap": ["GrossProfit"], "ifrs-full": ["GrossProfit"]},
    "oi": {"us-gaap": ["OperatingIncomeLoss"], "ifrs-full": ["ProfitLossFromOperatingActivities"]},
    "ni": {"us-gaap": ["NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic", "ProfitLoss"],
           "ifrs-full": ["ProfitLossAttributableToOwnersOfParent", "ProfitLoss"]},
    "eps": {"us-gaap": ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"],
            "ifrs-full": ["DilutedEarningsLossPerShare", "BasicAndDilutedEarningsLossPerShare"]},
    "cfo": {"us-gaap": ["NetCashProvidedByUsedInOperatingActivities",
                        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
            "ifrs-full": ["CashFlowsFromUsedInOperatingActivities"]},
    "capex": {"us-gaap": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
                          "PaymentsForCapitalImprovements", "PaymentsToAcquireOtherPropertyPlantAndEquipment"],
              "ifrs-full": ["PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
                            "PurchaseOfPropertyPlantAndEquipment",
                            "PurchaseOfPropertyPlantAndEquipmentIntangibleAssetsOtherThanGoodwillInvestmentPropertyAndOtherNoncurrentAssets"]},
    # Capitalized software is capital spending (HUBS: 2/3 of its capex); added to capex
    # whenever tagged, so FCF is not overstated for software companies.
    "capsw": {"us-gaap": ["PaymentsToDevelopSoftware", "PaymentsForSoftware"], "ifrs-full": []},
    # Customer advances: a CFO that is mostly a rise in contract liabilities (GEV
    # 2026: +$19.5B vs $16.7B CFO over nine months) is not recurring free cash flow.
    "dcl": {"us-gaap": ["IncreaseDecreaseInContractWithCustomerLiability", "IncreaseDecreaseInCustomerAdvances"],
            "ifrs-full": []},
    # EBIT proxy for filers that tag no operating-income line: pretax income + interest
    "pretax": {"us-gaap": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                           "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
                           "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic"],
               "ifrs-full": ["ProfitLossBeforeTax"]},
    "intexp": {"us-gaap": ["InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt"],
               "ifrs-full": ["FinanceCosts", "InterestExpense"]},
    "sbc": {"us-gaap": ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"],
            "ifrs-full": ["AdjustmentsForSharebasedPayments", "ExpenseFromSharebasedPaymentTransactionsWithEmployees"]},
}
# D&A: one combined tag, or depreciation + amortization of intangibles. Chosen PER
# COMPANY (whichever covers the latest quarter) and recorded in `da_method`.
DA_COMBINED = {"us-gaap": ["DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet",
                           "DepreciationAndAmortization"],
               "ifrs-full": ["DepreciationAndAmortisationExpense",
                             "DepreciationAmortisationAndImpairmentLossReversalOfImpairmentLossRecognisedInProfitOrLoss"]}
# OtherDepreciationAndAmortization is the cash-flow "depreciation and amortization" line
# of filers that report acquisition-intangible amortization on its own line (AMD,
# MRVL, OKTA), so it sits with depreciation and the intangibles line is added to it.
DA_DEP = {"us-gaap": ["Depreciation", "DepreciationNonproduction", "OtherDepreciationAndAmortization"],
          "ifrs-full": ["DepreciationPropertyPlantAndEquipment", "DepreciationExpense"]}
DA_AMORT = {"us-gaap": ["AmortizationOfIntangibleAssets"],
            "ifrs-full": ["AmortisationIntangibleAssetsOtherThanGoodwill", "AmortisationExpense"]}

INSTANT = {
    # one combined tag first; otherwise cash + the first short-term investment tag
    "cash_combined": {"us-gaap": ["CashCashEquivalentsAndShortTermInvestments"], "ifrs-full": []},
    "cash": {"us-gaap": ["CashAndCashEquivalentsAtCarryingValue",
                         "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
             "ifrs-full": ["CashAndCashEquivalents"]},
    "sti": {"us-gaap": ["MarketableSecuritiesCurrent", "ShortTermInvestments", "DebtSecuritiesCurrent",
                        "AvailableForSaleSecuritiesDebtSecuritiesCurrent", "AvailableForSaleSecuritiesCurrent"],
            "ifrs-full": ["CurrentInvestments", "ShorttermDepositsNotClassifiedAsCashEquivalents",
                          "OtherCurrentFinancialAssets"]},
    "equity": {"us-gaap": ["StockholdersEquity",
                           "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
               "ifrs-full": ["EquityAttributableToOwnersOfParent", "Equity"]},
}
# ---- debt: an explicit fallback chain, best first, per balance-sheet date --------
# Each method is (label, noncurrent-or-total tags, current tags added when present,
# includes_short_term). A method applies at a date only if its first group has a
# value AT that date; the tag actually used is recorded in `debt_src`. Commercial
# paper / short-term borrowings are added on top unless the method already includes
# short-term debt. Operating leases are never debt here.
# THE RULE (coordinator review, 2026-11-04, after ORCL valued with debt = 0): debt is
# NEVER defaulted to zero. Zero only when the filer has never tagged any debt concept,
# or its latest tagged debt is itself zero ("no debt reported"); otherwise the EV
# metrics are n/a with "debt not tagged in XBRL".
DEBT_CHAIN = {
    "us-gaap": [
        ("LongTermDebtNoncurrent + LongTermDebtCurrent",
         ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"],
         ["LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent", "DebtCurrent"], False),
        ("LongTermDebt (total, includes current maturities)", ["LongTermDebt"], [], False),
        ("LongTermNotesPayable + NotesPayableCurrent", ["LongTermNotesPayable", "SeniorLongTermNotes"],
         ["NotesPayableCurrent"], False),
        ("DebtLongtermAndShorttermCombinedAmount", ["DebtLongtermAndShorttermCombinedAmount"], [], True),
        ("ConvertibleDebt / ConvertibleNotesPayable", ["ConvertibleDebtNoncurrent", "ConvertibleNotesPayableNoncurrent",
                                                       "ConvertibleLongTermNotesPayable", "ConvertibleNotesPayable"],
         ["ConvertibleDebtCurrent", "ConvertibleNotesPayableCurrent"], False),
        ("DebtInstrumentCarryingAmount", ["DebtInstrumentCarryingAmount"], [], False),
        # NOT a method: LineOfCredit / short-term borrowings alone. They are one
        # instrument, and a filer whose notes moved to custom tags would read as its
        # revolver (DLR: $708M of an ~$18B balance sheet).
    ],
    "ifrs-full": [
        ("NoncurrentBorrowings + current portion",
         ["NoncurrentPortionOfNoncurrentBorrowings", "LongtermBorrowings", "NoncurrentBorrowings"],
         ["CurrentPortionOfNoncurrentBorrowings", "CurrentPortionOfLongtermBorrowings"], False),
        ("Borrowings (total)", ["Borrowings"], [], True),
    ],
}
DEBT_ST = {"us-gaap": ["CommercialPaper", "ShortTermBorrowings", "OtherShortTermBorrowings"],
           "ifrs-full": ["ShorttermBorrowings", "CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings"]}
# A debt value from an earlier balance sheet is used (labelled, partial) only if it
# is at most this old relative to the balance sheet the cash comes from.
DEBT_ASOF_MAX_DAYS = 200
# Hand values read from the filing itself, for filers whose XBRL debt tags miss part
# of the borrowings (segmented or custom tags) or stop at an older balance sheet.
# Each entry: {end: {"v": $M, "form", "accession", "items": [(line, $M), ...]}}.
# Total debt = every borrowing on the balance sheet (short-term, current portion of
# long-term, long-term, convertibles) at carrying amount; leases excluded, as in
# DEBT_CHAIN. "v" must equal the sum of "items" (a test checks it). An override is
# labelled "filing (hand-entered)" and is used at its own balance-sheet date only; at
# any later date it is an earlier balance sheet (debt_asof): EV multiples partial,
# never flagged. Read 2026-11-04 (inv-valuation2) from EDGAR; a guessed number here
# is worse than n/a.
DEBT_OVERRIDE = {
    # CAT: XBRL gave LongTermDebtNoncurrent + ShortTermBorrowings = 36,210 at 2025-12-31,
    # which leaves out "Long-term debt due within one year" (7,120); the override
    # supersedes it. Consolidated, so Financial Products (Cat Financial) debt is in.
    "CAT": {
        "2026-09-30": {"v": 45201.0, "form": "10-Q", "accession": "0000018230-26-000057", "items": [
            ("Short-term borrowings: Financial Products", 4855.0),
            ("Long-term debt due within one year: Machinery, Power & Energy", 36.0),
            ("Long-term debt due within one year: Financial Products", 8144.0),
            ("Long-term debt due after one year: Machinery, Power & Energy", 10614.0),
            ("Long-term debt due after one year: Financial Products", 21552.0)]},
        "2025-12-31": {"v": 43330.0, "form": "10-Q (comparative column)", "accession": "0000018230-26-000057", "items": [
            ("Short-term borrowings: Financial Products", 5514.0),
            ("Long-term debt due within one year: Machinery, Power & Energy", 35.0),
            ("Long-term debt due within one year: Financial Products", 7085.0),
            ("Long-term debt due after one year: Machinery, Power & Energy", 10678.0),
            ("Long-term debt due after one year: Financial Products", 20018.0)]},
    },
    # HUBS: no borrowing line on the balance sheet; the 2025 convertible notes were
    # repaid in June 2025 and Note 10 says "no outstanding borrowings" under the
    # $500M revolving credit facility at June 30, 2026. Genuinely zero.
    "HUBS": {
        "2026-06-30": {"v": 0.0, "form": "10-Q", "accession": "0001193125-26-335232", "items": [
            ("no borrowing line on the balance sheet; revolving credit facility undrawn (Note 10)", 0.0)]},
        "2025-12-31": {"v": 0.0, "form": "10-Q (comparative column)", "accession": "0001193125-26-335232", "items": [
            ("no borrowing line on the balance sheet", 0.0)]},
    },
    # ZS: the 2028 convertible senior notes are its only borrowing (principal
    # 1,725.0 less issuance costs and a fair-value hedge adjustment); no revolver or CP.
    "ZS": {
        "2026-07-31": {"v": 1696.355, "form": "10-K", "accession": "0001713683-26-000157", "items": [
            ("Convertible senior notes (2028 Notes, net carrying amount)", 1696.355)]},
        "2025-07-31": {"v": 1700.727, "form": "10-K (comparative column)", "accession": "0001713683-26-000157", "items": [
            ("Convertible senior notes (2028 Notes, net carrying amount)", 1700.727)]},
    },
}


def override_src(tk, end, o):
    return "filing (hand-entered): %s %s balance sheet %s, accession %s: %s" % (
        tk, o["form"], end, o["accession"], " + ".join("%s %s" % (n, format(v, ",.1f")) for n, v in o["items"]))

# Tags whose presence marks a balance sheet where EV and EBITDA are not meaningful.
BANK_TAGS = ["Deposits", "InterestBearingDepositLiabilities"]
INSURER_TAGS = ["PremiumsEarnedNet", "LiabilityForFuturePolicyBenefits"]

# ADR / ADS ratio: ordinary shares per US-listed share, for 20-F filers whose cover
# share count is in ordinary shares. 1 unless listed here. Sources: each depositary's
# published ratio (TSMC 1 ADS = 5 common; Himax 1 ADS = 2 ordinary).
ADR_RATIO = {"TSM": 5.0, "HIMX": 2.0}

# Split detection: consecutive point-in-time share counts whose ratio sits within
# SPLIT_TOL of one of these is a split (forward or reverse), not dilution or buybacks.
# 3-for-2 is deliberately absent: on this universe every 1.5x step in the share count
# was a share-financed merger (Newmont/Goldcorp 2019, Newmont/Newcrest 2023, Lam/Novellus
# 2012), and treating a merger as a split would rescale history by the deal.
SPLIT_RATIOS = [2, 3, 4, 5, 6, 8, 10, 15, 20, 25, 30, 40, 50]
SPLIT_TOL = 0.03
# A "split" undone by its inverse within this many days is a mis-scaled filing, not a
# split: both observations between are dropped.
GLITCH_DAYS = 400
# Financials older than this (days since the latest period end) are reported but kept
# out of flags: a 20-F filer's annual statements go stale between filings.
STALE_DAYS = 400

# ETF / trust / index sleeves in tickers.txt: a section header or a line comment with
# one of these words marks a fund, and so does every rotation-ladder row symbol.
FUND_WORDS = re.compile(r"\bETFs?\b|\bfund\b|\btrust\b|SPDR|iShares|Invesco|Vanguard|Sprott|Global X|"
                        r"twin of|sector \(|ladder sled|\bARK\b", re.I)
NON_EQUITY = re.compile(r"^\^|=F$|=X$|-USD$")
# Board symbols whose SEC ticker-map entry is a DIFFERENT company. Explicit, with the
# reason, so the collision is visible rather than silently valued.
#   GOLD: SEC's map gives GOLD to Gold.com, Inc. (ex-A-Mark) since Barrick moved its NYSE
#   line to B in 2025. The board's GOLD is Barrick (tickers.txt "Barrick"; the prices.db
#   series tracks Barrick), so it is pinned to Barrick Mining Corp's own CIK (verified on
#   EDGAR 2026-11-04: entityName BARRICK MINING CORP, ifrs-full, 40-F). The board ticker
#   is not renamed here - that is Jake's call.
CIK_OVERRIDE = {
    "GOLD": "0000756894",
}
HAND_NA = {}


def _d(s):
    return dt.date.fromisoformat(s)


def _days(a, b):
    return (_d(b) - _d(a)).days


# ---- universe ------------------------------------------------------------------
def read_tickers(path=os.path.join(HERE, "tickers.txt")):
    """[(ticker, comment, section_header)] in file order."""
    out, header = [], ""
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            if s.startswith("#"):
                m = re.match(r"#\s*---\s*(.+?)\s*---", s)
                if m:
                    header = m.group(1)
                continue
            tk, _, com = s.partition("#")
            tk = tk.strip()
            if tk:
                out.append((tk, com.strip(), header))
    return out


def fund_symbols():
    try:
        with open(os.path.join(DATA, "rotation_members.json"), encoding="utf-8") as f:
            return set(json.load(f).get("rows", {}).keys())
    except (OSError, ValueError):
        return set()


def classify(tk, comment, header, funds):
    """None for an operating company, else the honest reason it has no multiples."""
    if NON_EQUITY.search(tk):
        return "index, future, FX pair or crypto - no company financials"
    if tk in funds or FUND_WORDS.search(comment or "") or re.search(r"\bETFs?\b", header or ""):
        return "ETF / trust / sleeve - a fund has no company financials"
    return None


# ---- fact plumbing -------------------------------------------------------------
def _units(concept_obj, currency=None):
    """The fact list for one concept: the reporting-currency unit, else the first."""
    units = concept_obj.get("units", {})
    if currency and currency in units:
        return currency, units[currency]
    for u in ("USD", "USD/shares", "shares"):
        if u in units:
            return u, units[u]
    for u, arr in units.items():
        return u, arr
    return None, []


def flow_periods(facts, taxonomy, names, currency):
    """{(start, end): {"v", "first", "last", "concept"}} across the concepts in priority
    order. Within one concept the latest-filed value wins (restatement), and the first
    filing date is kept as when the period became known. A lower-priority concept fills
    only periods the higher ones lack."""
    out = {}
    tax = facts.get(taxonomy, {})
    for name in names:
        c = tax.get(name)
        if not c:
            continue
        _, arr = _units(c, currency)
        mine = {}
        for x in arr:
            s, e = x.get("start"), x.get("end")
            if not s or not e or x.get("val") is None:
                continue
            k = (s, e)
            cur = mine.get(k)
            filed = x.get("filed") or "9999-12-31"
            if cur is None:
                mine[k] = {"v": float(x["val"]), "first": filed, "last": filed, "concept": name}
            else:
                if filed >= cur["last"]:
                    cur["v"], cur["last"] = float(x["val"]), filed
                if filed < cur["first"]:
                    cur["first"] = filed
        for k, v in mine.items():
            if k not in out:
                out[k] = v
    return out


def instant_points(facts, taxonomy, names, currency):
    """{end: {"v", "first", "last", "concept"}} for balance-sheet instants; same
    restatement rule as flow_periods."""
    out = {}
    tax = facts.get(taxonomy, {})
    for name in names:
        c = tax.get(name)
        if not c:
            continue
        _, arr = _units(c, currency)
        mine = {}
        for x in arr:
            e = x.get("end")
            if not e or x.get("start") or x.get("val") is None:
                continue
            filed = x.get("filed") or "9999-12-31"
            cur = mine.get(e)
            if cur is None:
                mine[e] = {"v": float(x["val"]), "first": filed, "last": filed, "concept": name}
            else:
                if filed >= cur["last"]:
                    cur["v"], cur["last"] = float(x["val"]), filed
                if filed < cur["first"]:
                    cur["first"] = filed
        for e, v in mine.items():
            if e not in out:
                out[e] = v
    return out


def quarters_from_periods(periods):
    """{end: {"v", "start", "known", "derived", "concept"}} -- one value per quarter end.

    A reported 3-month fact wins; otherwise a quarter is DERIVED as a longer period
    minus the period with the same start ending one quarter earlier (Q4 = FY - 9M).
    `known` is the first date both pieces were public."""
    q = {}
    for (s, e), v in periods.items():
        n = _days(s, e)
        if Q_MIN <= n <= Q_MAX:
            q[e] = {"v": v["v"], "start": s, "known": v["first"], "derived": False, "concept": v["concept"]}
    by_start = {}
    for (s, e), v in periods.items():
        by_start.setdefault(s, []).append((e, v))
    for s, lst in by_start.items():
        lst.sort()
        for i, (e, v) in enumerate(lst):
            if _days(s, e) <= Q_MAX:
                continue
            for e2, v2 in lst[:i]:
                gap = _days(e2, e)
                if Q_MIN <= gap <= Q_MAX:
                    if e not in q:
                        qs = (_d(e2) + dt.timedelta(days=1)).isoformat()
                        q[e] = {"v": v["v"] - v2["v"], "start": qs, "known": max(v["first"], v2["first"]),
                                "derived": True, "concept": v["concept"]}
                    break
    return q


def ttm_at(periods, quarters, end):
    """(value, known, how) for the twelve months ending at `end`, or None."""
    for (s, e), v in periods.items():
        if e == end and FY_MIN <= _days(s, e) <= FY_MAX:
            return v["v"], v["first"], "FY"
    if end not in quarters:
        return None
    chain, cur = [quarters[end]], end
    for _ in range(3):
        prev = [e for e in quarters if Q_MIN <= _days(e, cur) <= Q_MAX]
        if not prev:
            return None
        cur = max(prev)
        chain.append(quarters[cur])
    return sum(c["v"] for c in chain), max(c["known"] for c in chain), "4Q"


def run_back(quarters, end, n=4):
    """The consecutive quarters ending at `end`, newest first, at most n."""
    if end not in quarters:
        return []
    out, cur = [quarters[end]], end
    while len(out) < n:
        prev = [e for e in quarters if Q_MIN <= _days(e, cur) <= Q_MAX]
        if not prev:
            break
        cur = max(prev)
        out.append(quarters[cur])
    return out


def _rev_units(facts, taxonomy):
    """{unit: (latest end, fact count)} over the revenue concepts of a taxonomy."""
    out = {}
    tax = facts.get(taxonomy, {})
    for name in FLOW["rev"].get(taxonomy, []):
        c = tax.get(name)
        if not c:
            continue
        for u, arr in c.get("units", {}).items():
            le = max((x.get("end") or "" for x in arr), default="")
            pe, pn = out.get(u, ("", 0))
            out[u] = (max(pe, le), pn + len(arr))
    return out


def pick_taxonomy(facts):
    """The taxonomy whose revenue line is most recent (AEM and HIMX filed us-gaap
    years ago and ifrs-full since)."""
    best, key = None, ("", 0)
    for tax in ("us-gaap", "ifrs-full"):
        if tax not in facts:
            continue
        u = _rev_units(facts, tax)
        k = max(u.values()) if u else ("", 0)
        if best is None or k > key:
            best, key = tax, k
    return best


def pick_currency(facts, taxonomy):
    """The reporting currency: the revenue unit with the latest period, and on a tie
    the one with the most facts -- a 20-F's USD "convenience translation" covers one
    year, the reporting currency covers all of them (TSMC: TWD, not USD)."""
    u = _rev_units(facts, taxonomy)
    if not u:
        return "USD"
    return max(u, key=lambda k: (u[k][0], u[k][1], k == "USD"))


# ---- shares --------------------------------------------------------------------
def share_observations(facts):
    """Point-in-time share counts: [{end, filed, v, src}], one per filing.

    dei:EntityCommonStockSharesOutstanding (the cover, dated a few weeks after the
    period) is summed across classes within a filing. us-gaap CommonStockSharesOutstanding
    (the balance sheet) fills filings with no cover count, and REPLACES a cover count
    that is below 90% of it -- a cover that tagged only one class non-dimensionally."""
    by_accn = {}
    dei = facts.get("dei", {}).get("EntityCommonStockSharesOutstanding")
    if dei:
        for x in dei.get("units", {}).get("shares", []):
            if x.get("val") is None:
                continue
            k = x.get("accn")
            ent = by_accn.setdefault(k, {"filed": x.get("filed"), "dei": {}, "gaap": None})
            ent["dei"].setdefault(x["end"], set()).add(float(x["val"]))
    gaap = facts.get("us-gaap", {}).get("CommonStockSharesOutstanding")
    if gaap:
        for x in gaap.get("units", {}).get("shares", []):
            if x.get("val") is None:
                continue
            k = x.get("accn")
            ent = by_accn.setdefault(k, {"filed": x.get("filed"), "dei": {}, "gaap": None})
            # the filing's OWN balance-sheet date is its latest instant
            if ent["gaap"] is None or x["end"] > ent["gaap"][0]:
                ent["gaap"] = (x["end"], float(x["val"]))
    # Third source: the filing's weighted-average DILUTED count for its latest period
    # (shortest duration on a tie). Every multi-class filer whose cover is dimensional
    # (META, DDOG, NET, NVO...) still tags this one without a dimension.
    for tax, name in (("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
                      ("ifrs-full", "AdjustedWeightedAverageShares"),
                      ("us-gaap", "WeightedAverageNumberOfShareOutstandingBasicAndDiluted")):
        wa = facts.get(tax, {}).get(name)
        if not wa:
            continue
        for x in wa.get("units", {}).get("shares", []):
            if not x.get("val") or not x.get("start"):
                continue
            k = x.get("accn")
            ent = by_accn.setdefault(k, {"filed": x.get("filed"), "dei": {}, "gaap": None})
            key = (x["end"], -_days(x["start"], x["end"]))
            if ent.get("wa") is None or key > ent["wa"][0]:
                ent["wa"] = (key, x["end"], float(x["val"]))
    obs = []
    for accn, ent in by_accn.items():
        d_end, d_val = None, None
        if ent["dei"]:
            d_end = max(ent["dei"])
            d_val = sum(ent["dei"][d_end])
        g = ent["gaap"]
        if g is not None and not g[1]:
            g = None
        if d_val and (g is None or d_val >= 0.9 * g[1]):
            obs.append({"end": d_end, "filed": ent["filed"], "v": d_val, "src": "dei cover"})
        elif g is not None:
            obs.append({"end": g[0], "filed": ent["filed"], "v": g[1], "src": "us-gaap CommonStockSharesOutstanding"})
        elif ent.get("wa"):
            obs.append({"end": ent["wa"][1], "filed": ent["filed"], "v": ent["wa"][2],
                        "src": "weighted-average diluted (no outstanding count tagged)"})
    obs.sort(key=lambda o: (o["filed"] or "", o["end"]))
    # one observation per filing date, the latest instant
    dedup = {}
    for o in obs:
        dedup[o["filed"]] = o
    return [dedup[k] for k in sorted(dedup)]


RESTATE_CONCEPTS = [("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding", 1),
                    ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic", 1),
                    ("us-gaap", "CommonStockSharesOutstanding", 1),
                    ("ifrs-full", "AdjustedWeightedAverageShares", 1),
                    ("ifrs-full", "WeightedAverageShares", 1),
                    ("us-gaap", "EarningsPerShareDiluted", -1),
                    ("ifrs-full", "DilutedEarningsLossPerShare", -1)]


def restatements(facts):
    """[(ratio, first_filed, restating_filed)]: every period whose share count (or
    EPS, inverted) was later re-reported scaled by a split-like ratio. A real split is
    restated into every comparative period; a merger, an IPO or a buyback never is."""
    out = []
    for tax, name, sign in RESTATE_CONCEPTS:
        c = facts.get(tax, {}).get(name)
        if not c:
            continue
        for unit, arr in c.get("units", {}).items():
            per = {}
            for x in arr:
                if x.get("val") in (None, 0):
                    continue
                per.setdefault((x.get("start"), x["end"]), []).append((x.get("filed") or "", float(x["val"])))
            for lst in per.values():
                lst.sort()
                f0, v0 = lst[0]
                for f1, v1 in lst[1:]:
                    if f1 > f0 and v0 and v1 / v0 > 0:
                        out.append(((v1 / v0) ** sign, f0, f1))
    return out


def _nice(r):
    inv = r < 1
    rr = 1 / r if inv else r
    for nice in SPLIT_RATIOS:
        if abs(rr / nice - 1) <= SPLIT_TOL:
            return (1.0 / nice) if inv else float(nice)
    return None


def detect_splits(obs, restated=None):
    """[{"after": filed_date_of_first_post_split_obs, "ratio": r}] from consecutive
    point-in-time counts. r > 1 forward split, r < 1 reverse. When `restated` is given
    (restatements(facts)), a step is a split only if some period first reported
    BEFORE the step was later re-reported scaled by the same ratio."""
    out = []
    for a, b in zip(obs, obs[1:]):
        if not a["v"] or not b["v"]:
            continue
        nice = _nice(b["v"] / a["v"])
        if nice is None:
            continue
        if restated is not None:
            ok = any(abs(r / nice - 1) <= SPLIT_TOL and f0 < b["filed"] and f1 >= a["filed"]
                     for r, f0, f1 in restated)
            if not ok:
                b["unconfirmed_step"] = nice
                continue
        out.append({"after": b["filed"], "ratio": nice, "between": [a["end"], b["end"]]})
    # A step undone by its inverse within GLITCH_DAYS is one mis-scaled filing (Wells
    # Fargo's 2023 cover read half its count): drop both steps and mark the filings
    # between them so split_adjust leaves them out.
    keep, i = [], 0
    while i < len(out):
        s = out[i]
        if i + 1 < len(out):
            t = out[i + 1]
            if abs(s["ratio"] * t["ratio"] - 1) < 0.05 and _days(s["after"], t["after"]) <= GLITCH_DAYS:
                for o in obs:
                    if s["after"] <= (o["filed"] or "") < t["after"]:
                        o["glitch"] = True
                i += 2
                continue
        keep.append(s)
        i += 1
    return keep


def split_adjust(obs, splits):
    """Each observation restated to today's share basis (the basis of the
    split-adjusted closes in prices.db). An adjusted count more than 3x away from
    the median of its neighbours (two either side) is a mis-scaled filing, marked
    `glitch` and left out."""
    for o in obs:
        f = 1.0
        for s in splits:
            if (o["filed"] or "") < s["after"]:
                f *= s["ratio"]
        o["adj"] = o["v"] * f
        o["factor"] = f
    for i, o in enumerate(obs):
        near = sorted((abs(j - i), x["adj"]) for j, x in enumerate(obs) if j != i and not x.get("glitch"))[:4]
        nb = sorted(v for _, v in near)
        if len(nb) >= 2:
            med = nb[len(nb) // 2]
            if med and not (1 / 3.0 <= o["adj"] / med <= 3.0):
                o["glitch"] = True
    return obs


def debt_resolver(facts, taxonomy, ccy, tk=None):
    """A function end -> {"v", "src", "asof", "stale"} or {"v": None, "why"}.

    Walks DEBT_CHAIN at the balance-sheet date; failing that, the newest earlier
    date (<= DEBT_ASOF_MAX_DAYS) where any method applies, labelled stale. Zero only
    for a filer with no debt concept ever tagged, or whose newest tagged debt is 0."""
    chain = DEBT_CHAIN.get(taxonomy, [])
    names = set(DEBT_ST.get(taxonomy, []))
    for _, a, b, _ in chain:
        names.update(a)
        names.update(b)
    pts = {n: instant_points(facts, taxonomy, [n], ccy) for n in names}
    st = DEBT_ST.get(taxonomy, [])
    over = DEBT_OVERRIDE.get(tk or "", {})

    def first(tags, end):
        for t in tags:
            p = pts.get(t, {}).get(end)
            if p is not None:
                return p["v"], t
        return None

    def at(end):
        if end in over:
            o = over[end]
            return {"v": o["v"] * 1e6, "src": override_src(tk, end, o), "asof": end, "override": True}
        for label, base, cur, incl_st in chain:
            b = first(base, end)
            if b is None:
                continue
            v, used = b[0], [b[1]]
            c = first(cur, end)
            if c is not None:
                v += c[0]
                used.append(c[1])
            if not incl_st:
                s = first(st, end)
                if s is not None:
                    v += s[0]
                    used.append(s[1])
            return {"v": v, "src": " + ".join(used), "asof": end}
        return None

    # override dates join the walk-back, so a later balance sheet can fall back to a
    # hand-entered one (labelled debt_asof: partial, never flagged)
    all_dates = sorted({e for p in pts.values() for e in p} | set(over))

    def resolve(end):
        r = at(end)
        if r:
            return r
        for e in reversed([d for d in all_dates if d < end]):
            if _days(e, end) > DEBT_ASOF_MAX_DAYS:
                break
            r = at(e)
            if r:
                r["stale"] = True
                return r
        earlier = [d for d in all_dates if d <= end]
        if not all_dates:
            return {"v": 0.0, "src": "no debt reported (no debt concept ever tagged)", "asof": end, "zero": True}
        if not earlier:
            return {"v": None, "why": "debt not tagged in XBRL before %s" % end}
        last = earlier[-1]
        # Debt-free only if EVERY debt concept this filer ever used reads zero at its
        # own newest fact: one zero DebtCurrent beside convertibles parked in a custom
        # tag (DDOG) is not a debt-free balance sheet.
        newest = [p[max(e for e in p if e <= end)]["v"] for p in pts.values() if any(e <= end for e in p)]
        if newest and all(v == 0 for v in newest):
            return {"v": 0.0, "src": "no debt reported (every debt tag it has used reads zero, latest %s)" % last,
                    "asof": end, "zero": True}
        return {"v": None, "why": "debt not tagged in XBRL for this balance sheet (last tagged %s)" % last}
    return resolve


# ---- one company ---------------------------------------------------------------
def build_company(cf, tk, adr_ratio=None):
    facts = cf.get("facts", {})
    taxonomy = pick_taxonomy(facts) or ("us-gaap" if "us-gaap" in facts else ("ifrs-full" if "ifrs-full" in facts else None))
    rec = {"entity": cf.get("entityName"), "taxonomy": taxonomy}
    if taxonomy is None:
        rec.update(status="na", why="no us-gaap or ifrs-full financial statements in SEC companyfacts")
        return rec
    tax = facts[taxonomy]
    if any(t in tax for t in BANK_TAGS):
        rec["kind"] = "bank"
    elif any(t in tax for t in INSURER_TAGS):
        rec["kind"] = "insurer"
    else:
        rec["kind"] = "operating"
    ccy = pick_currency(facts, taxonomy)
    rec["currency"] = ccy
    rec["adr_ratio"] = float(adr_ratio if adr_ratio is not None else ADR_RATIO.get(tk, 1.0))

    P, Q = {}, {}
    used = {}
    for key, spec in FLOW.items():
        names = spec.get(taxonomy, [])
        P[key] = flow_periods(facts, taxonomy, names, ccy if key != "eps" else None)
        Q[key] = quarters_from_periods(P[key])
        used[key] = sorted({v["concept"] for v in P[key].values()})
    # D&A, chosen per company
    Pc = flow_periods(facts, taxonomy, DA_COMBINED.get(taxonomy, []), ccy)
    Pd = flow_periods(facts, taxonomy, DA_DEP.get(taxonomy, []), ccy)
    Pa = flow_periods(facts, taxonomy, DA_AMORT.get(taxonomy, []), ccy)
    Qc, Qd, Qa = quarters_from_periods(Pc), quarters_from_periods(Pd), quarters_from_periods(Pa)
    last_c = max((e for (s, e) in Pc), default="")
    last_d = max((e for (s, e) in Pd), default="")
    da_method = "combined" if last_c and last_c >= last_d else ("components" if last_d else "none")
    rec["da_method"] = {"combined": "one tag: " + ", ".join(sorted({v["concept"] for v in Pc.values()})),
                        "components": "Depreciation + AmortizationOfIntangibleAssets (" +
                                      ", ".join(sorted({v["concept"] for v in list(Pd.values()) + list(Pa.values())})) + ")",
                        "none": "no D&A tag found"}[da_method]

    def amort_ttm(end):
        """(value, known, partial) for intangibles amortization, or None."""
        a = ttm_at(Pa, Qa, end)
        if a:
            return a[0], a[1], False
        # Tagged for only 2-3 of the four quarters (a tag the filer started using
        # mid-year): annualise the consecutive run ending here, marked partial.
        run = run_back(Qa, end)
        if len(run) >= 2:
            return sum(x["v"] for x in run) * 4.0 / len(run), max(x["known"] for x in run), True
        return None

    def combined_ttm(end):
        r = ttm_at(Pc, Qc, end)
        if not r:
            return None
        a = amort_ttm(end)
        # A "combined" tag SMALLER than the intangibles line cannot include it (Cisco
        # tags depreciation alone as DepreciationDepletionAndAmortization): add it.
        if a and r[0] < a[0]:
            return r[0] + a[0], max(r[1], a[1]), a[2]
        return r[0], r[1], False

    def components_ttm(end):
        r = ttm_at(Pd, Qd, end)
        if not r:
            return None
        a = amort_ttm(end)
        if a:
            return r[0] + a[0], max(r[1], a[1]), a[2]
        # no amortization line for this window: depreciation alone, marked partial
        return r[0], r[1], True

    def da_ttm(end):
        order = (combined_ttm, components_ttm) if da_method == "combined" else (components_ttm, combined_ttm)
        for fn in order:
            r = fn(end)
            if r:
                return r
        return None

    I = {k: instant_points(facts, taxonomy, spec.get(taxonomy, []), ccy) for k, spec in INSTANT.items()}
    debt_at = debt_resolver(facts, taxonomy, ccy, tk)

    # the quarter-end grid is the revenue line's (fall back to operating income)
    grid = sorted(set(Q["rev"]) | {e for (s, e) in P["rev"] if FY_MIN <= _days(s, e) <= FY_MAX})
    if not grid:
        grid = sorted(set(Q["oi"]) | set(Q["ni"]))
    rows = []
    for end in grid[-KEEP_QUARTERS:]:
        row = {"end": end}
        known = []
        for key in ("rev", "gp", "oi", "ni", "eps", "cfo", "capex", "sbc"):
            r = ttm_at(P[key], Q[key], end)
            if r:
                row[key] = r[0]
                known.append(r[1])
                if key == "rev":
                    row["rev_how"] = r[2]
        if "oi" not in row:
            pt = ttm_at(P["pretax"], Q["pretax"], end)
            if pt:
                ie = ttm_at(P["intexp"], Q["intexp"], end)
                row["oi"] = pt[0] + (ie[0] if ie else 0.0)
                row["oi_proxy"] = True
        if "gp" not in row and "rev" in row:
            r = ttm_at(P["cogs"], Q["cogs"], end)
            if r:
                row["gp"] = row["rev"] - r[0]
        r = da_ttm(end)
        if r:
            row["da"] = r[0]
            if r[2]:
                row["da_partial"] = True
        if "oi" in row and "da" in row:
            row["ebitda"] = row["oi"] + row["da"]
        sw = ttm_at(P["capsw"], Q["capsw"], end)
        if sw and "capex" in row:
            row["capex"] += sw[0]
            row["capex_incl_software"] = True
        dcl = ttm_at(P["dcl"], Q["dcl"], end)
        if dcl and "cfo" in row and row["cfo"] > 0 and dcl[0] > 0.5 * row["cfo"]:
            row["cfo_advances"] = round(dcl[0] / row["cfo"], 2)   # share of CFO from customer advances
        if "cfo" in row and "capex" in row:
            row["fcf"] = row["cfo"] - row["capex"]
        if end in Q["rev"]:
            row["rev_q"] = Q["rev"][end]["v"]
        # balance sheet at the quarter end
        cc = I["cash_combined"].get(end)
        if cc:
            row["cash"] = cc["v"]
        elif end in I["cash"]:
            row["cash"] = I["cash"][end]["v"] + (I["sti"][end]["v"] if end in I["sti"] else 0.0)
        d = debt_at(end)
        if d.get("v") is None:
            row["debt_why"] = d["why"]          # no "debt" key: EV is n/a, never debt = 0
        else:
            row["debt"] = d["v"]
            row["debt_src"] = d["src"]
            if d.get("stale"):
                row["debt_asof"] = d["asof"]    # an earlier balance sheet: EV metrics partial
        if end in I["equity"]:
            row["equity"] = I["equity"][end]["v"]
        # when it became public: the first filing that carried the quarter's revenue
        # (or, for a name without one, the latest of the pieces)
        rk = None
        if end in Q["rev"]:
            rk = Q["rev"][end]["known"]
        else:
            for (s, e), v in P["rev"].items():
                if e == end and FY_MIN <= _days(s, e) <= FY_MAX:
                    rk = v["first"]
        row["known"] = rk or (max(known) if known else None)
        rows.append(row)

    obs = share_observations(facts)
    splits = detect_splits(obs, restatements(facts))
    split_adjust(obs, splits)
    rec["shares"] = [{"end": o["end"], "filed": o["filed"], "raw": o["v"], "adj": o["adj"], "src": o["src"]}
                     for o in obs if o["filed"] and not o.get("glitch")
                     and o["filed"] >= (rows[0]["end"] if rows else "")]
    rec["splits"] = splits
    rec["concepts"] = used
    rec["quarters"] = rows
    if rec["kind"] in ("bank", "insurer"):
        # never a fake EBITDA: interest is the product, not a financing cost
        for row in rows:
            for k in ("ebitda", "da", "da_partial"):
                row.pop(k, None)
    ent = (rec.get("entity") or "").upper()
    if re.search(r"\bTRUST\b|\bETF\b|\bFUND\b", ent) and not Q["rev"]:
        rec["status"] = "na"
        rec["why"] = "trust / fund (" + (rec.get("entity") or "") + ") - no company financials"
    elif not rows or "rev" not in rows[-1]:
        rec["status"] = "na"
        rec["why"] = ("fewer than four consecutive quarters of revenue in XBRL filings so far (a recent listing) - no TTM yet"
                      if P["rev"] else
                      "no revenue line in the filings' XBRL (" + (taxonomy or "?") + ")")
    elif rec["kind"] in ("bank", "insurer"):
        rec["status"] = "partial"
        rec["why"] = rec["kind"] + " - EBITDA and EV are not meaningful for a balance-sheet business; P/E and P/B only"
    elif not rec["shares"]:
        rec["status"] = "partial"
        rec["why"] = "no share count tagged in the filings"
    else:
        rec["status"] = "ok"
    return rec


def _round(rec):
    """Store money in millions (one decimal) and shares in millions (three)."""
    for r in rec.get("quarters", []):
        for k, v in list(r.items()):
            if isinstance(v, float) and k != "cfo_advances":    # a ratio, not money
                r[k] = round(v, 4) if k == "eps" else round(v / 1e6, 1)
    for s in rec.get("shares", []):
        s["raw"] = round(s["raw"] / 1e6, 3)
        s["adj"] = round(s["adj"] / 1e6, 3)
    return rec


def universe(extra=()):
    funds = fund_symbols()
    names, na = [], {}
    for tk, com, hdr in read_tickers():
        why = classify(tk, com, hdr, funds)
        if why:
            na[tk] = why
        elif tk not in names:
            names.append(tk)
    for tk in extra:
        if tk not in names and tk not in na:
            names.append(tk)
    return names, na


def peer_names():
    try:
        with open(os.path.join(DATA, "peers.json"), encoding="utf-8") as f:
            P = json.load(f)
    except (OSError, ValueError):
        return []
    out = []
    for v in (P.get("overrides") or {}).values():
        out.extend(v.get("peers", []) if isinstance(v, dict) else v)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tickers", default="")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--stdout", action="store_true")
    a = ap.parse_args(argv)
    with open(os.path.join(DATA, "cik_map.json"), encoding="utf-8") as f:
        cmap = json.load(f)["map"]
    names, na = universe(peer_names())
    if a.tickers:
        want = [t.strip().upper() for t in a.tickers.split(",") if t.strip()]
        names = [t for t in want if t not in na]
        na = {t: na[t] for t in want if t in na}
    out = {"generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
           "source": "SEC XBRL companyfacts (data.sec.gov/api/xbrl/companyfacts)",
           "units": "money in millions of the filing currency; shares in millions (split-adjusted `adj`)",
           "method": ("TTM = fiscal-year fact at a fiscal year end, else four consecutive quarters; a quarter is "
                      "reported (3 months) or derived (a longer period minus the same-start period one quarter "
                      "shorter, e.g. Q4 = FY - 9M). Restated values keep the latest filing; `known` is the first "
                      "filing date. Debt = long-term noncurrent + current portion + commercial paper/short-term "
                      "borrowings, operating leases excluded. Cash = cash + short-term marketable securities."),
           "names": {}, "na": {}}
    for tk, why in na.items():
        out["na"][tk] = why
    errors = {}
    for tk in names:
        cik = CIK_OVERRIDE.get(tk) or cmap.get(tk)
        if tk in HAND_NA:
            out["na"][tk] = HAND_NA[tk]
            continue
        if not cik:
            out["na"][tk] = "no SEC registrant for this symbol (foreign listing or OTC line) - no XBRL filings to read"
            continue
        try:
            cf = sec_facts.companyfacts(cik, offline=a.offline)
        except Exception as e:      # one name's outage costs that name only
            if getattr(e, "code", None) == 404:
                out["na"][tk] = ("SEC has no XBRL companyfacts for this registrant (an OTC ADR or foreign "
                                 "ordinary line that files no financial statements in XBRL)")
            else:
                errors[tk] = repr(e)[:200]
            continue
        if cf is None:
            errors[tk] = "not cached (offline run)"
            continue
        rec = build_company(cf, tk)
        rec["cik"] = cik
        # The legal name stays out of the committed payload (CIK identifies the
        # registrant): JPMORGAN CHASE & CO trips the public repo's rule-18 prose guard.
        rec.pop("entity", None)
        # Same reason for the per-key concept lists (IFRS "PurchaseOfPropertyPlant..."
        # contains "chase"); they are diagnostics, reproducible with --stdout locally.
        if not a.stdout:
            rec.pop("concepts", None)
        out["names"][tk] = _round(rec)
    out["errors"] = errors
    s = json.dumps(out, separators=(",", ":"), ensure_ascii=False)
    if a.stdout:
        print(s)
        return 0
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(s)
    ok = sum(1 for r in out["names"].values() if r.get("status") == "ok")
    print("fundamentals.json: %d ok, %d partial, %d na-in-filings, %d not applicable, %d errors, %d bytes" % (
        ok, sum(1 for r in out["names"].values() if r.get("status") == "partial"),
        sum(1 for r in out["names"].values() if r.get("status") == "na"), len(out["na"]), len(errors), len(s)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
