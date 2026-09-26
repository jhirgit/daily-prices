#!/usr/bin/env python3
"""
SPEC-86 section 6 as data: what each sector-ladder row's trend is a bet on, and
the row-specific checks that say whether the move is still supported.

One entry per PRIMARY row of ``regime.REG_ETFS`` (63). Twins inherit their
primary's checks through ``regime.REG_ETFS`` ``grp`` (see ``primary_of``) and
show their own holdings / phase. Editing a row's checks is a one-line change
here; ``rotation_phase.py`` (daily) and ``rotation_evidence.py`` (calibration)
both read this table, so the evidence always describes the checks on the board.

Check fields
------------
  id       short slug, unique within the row
  kind     driver | ratio | curve | level | concentration | split |
           participation | extension          (spec section 5)
  inputs   yfinance symbols (or ladder basket names): driver [X]; ratio [A, B];
           curve [long, short]; level [A, B] (A - B vs `threshold`);
           concentration [names] ([] = the top 3 by weight); split / extension /
           participation []
  s        +1 = this measure RISING supports the row's up-trend; -1 = falling
           supports it; 0 = informational (no expected sign; status "info")
  window   sessions (63 default; 21 / 126 where section 6 says so)
  label    plain-English label for the board
  up_div   words for a DIVERGENCE while the row is in an up-trend (the
           topping-side tell); dn_div the same in a down-trend (the bottoming-
           side tell). Written for this row; a generic phrase is generated
           only for the auto-added benchmark ratio.
  unit     "bp" for yields (the measure is a change in basis points)
  fx       a USD conversion series for a foreign-currency input
  groups   split only: [{"name", "members"}], members None = the rest of the fund

Every row also gets an ``extension`` check and, unless section 6 already lists
a ratio against the row's natural benchmark, a ``ratio`` check against
``bench`` (sector ETF for a sub-industry, SPY for a sector; SPY for
commodity-linked sleeves whose sector ETF is a different commodity).

Banned in every string here and everything generated from it: buy, sell,
should, will, likely, target, recommend (test_rotation_phase.py enforces it).
"""

from __future__ import annotations

import regime as RG

FAMILIES = ("commodity", "cyclical", "defensive", "growth", "theme")

# Yield series: the measure is a change in basis points, not a log change.
YIELD_SYMS = {"^TNX", "^FVX", "^IRX", "^TYX"}

# Sprott Physical Uranium trades in CAD; converted to USD with this series.
FX_OF = {"U-UN.TO": "CADUSD=X"}


# --------------------------------------------------------------------------
# constructors (keep the table below readable)
# --------------------------------------------------------------------------

def drv(id, sym, s, label, up, dn, window=63):
    c = {"id": id, "kind": "driver", "inputs": [sym], "s": s, "window": window,
         "label": label, "up_div": up, "dn_div": dn}
    if sym in YIELD_SYMS:
        c["unit"] = "bp"
    if sym in FX_OF:
        c["fx"] = FX_OF[sym]
    return c


def rat(id, a, b, s, label, up, dn, window=63):
    return {"id": id, "kind": "ratio", "inputs": [a, b], "s": s, "window": window,
            "label": label, "up_div": up, "dn_div": dn}


def info_ratio(id, a, b, label, why):
    return {"id": id, "kind": "ratio", "inputs": [a, b], "s": 0, "window": 63,
            "label": label, "why": why}


def crv(id, long, short, s, label, up, dn, window=63):
    return {"id": id, "kind": "curve", "inputs": [long, short], "s": s, "window": window,
            "label": label, "unit": "bp", "up_div": up, "dn_div": dn}


def lvl(id, a, b, s, label, up, dn, threshold=0.0, window=63):
    return {"id": id, "kind": "level", "inputs": [a, b], "s": s, "window": window,
            "label": label, "threshold": threshold, "up_div": up, "dn_div": dn}


def conc(id, names, label, why):
    return {"id": id, "kind": "concentration", "inputs": list(names), "s": 0, "window": 63,
            "label": label, "why": why}


def split(id, groups, label, why):
    return {"id": id, "kind": "split", "inputs": [], "s": 0, "window": 63, "label": label,
            "groups": [{"name": n, "members": (list(m) if m is not None else None)}
                       for n, m in groups],
            "why": why}


def part(label="Share of the fund's weight rising",
         up="fewer of the fund's names rising than the move suggests",
         dn="most of the fund's names rising while the fund falls"):
    return {"id": "breadth", "kind": "participation", "inputs": [], "s": 1, "window": 63,
            "label": label, "up_div": up, "dn_div": dn}


TOP3 = []  # concentration on the top 3 holdings by weight


# --------------------------------------------------------------------------
# THE TABLE -- spec section 6, one entry per primary row
# --------------------------------------------------------------------------

THESES = {
    # ---------------- Broad and regions ----------------
    "SPY": {
        "family": "cyclical",
        "bet": "broad earnings growth at today's multiple",
        "bench": None,   # SPY is the benchmark
        "checks": [
            rat("breadth", "RSP", "SPY", 1, "Equal-weight vs cap-weight S&P (RSP/SPY)",
                "the rally narrowing to the mega-caps -- equal-weight lagging",
                "the average stock holding up better than the index"),
            conc("conc", TOP3, "Top-3 share of the move",
                 "how much of the index's move its three largest names made"),
            rat("credit", "HYG", "IEF", 1, "Credit vs Treasuries (HYG/IEF)",
                "credit not confirming -- high yield lagging Treasuries",
                "credit firming while stocks fall"),
            lvl("vixts", "^VIX3M", "^VIX", 1, "VIX term structure (VIX3M - VIX)",
                "the VIX curve inverted -- near-term stress priced above 3-month",
                "the VIX curve back in calm contango"),
        ],
        "topping": "The index keeps rising while equal-weight lags, high yield stops beating "
                   "Treasuries and the VIX curve flattens toward inversion -- a narrowing, "
                   "credit-unconfirmed advance.",
        "bottoming": "The index is still down but equal-weight and high yield start "
                     "outperforming and the VIX curve rebuilds contango -- stress draining "
                     "before price turns.",
    },
    "QQQ": {
        "family": "growth",
        "bet": "mega-cap growth earnings, and duration",
        "bench": "SPY",
        "checks": [
            rat("eqw", "QQQE", "QQQ", 1, "Equal-weight Nasdaq vs QQQ (QQQE/QQQ)",
                "leadership narrowing -- the equal-weight Nasdaq lagging",
                "the average Nasdaq name steadier than the mega-caps"),
            conc("conc", TOP3, "Top-3 share of the move",
                 "whether a handful of mega-caps is the whole move"),
            drv("rates", "^TNX", -1, "10y Treasury yield (^TNX)",
                "the 10y yield rising against long-duration growth",
                "yields falling -- duration relief"),
            rat("engine", "SMH", "QQQ", 1, "Semis vs Nasdaq (SMH/QQQ)",
                "the engine stalling -- semis lagging the index",
                "semis leading again inside a falling index"),
            rat("payers", "HYPERSCALE", "QQQ", 1, "Hyperscalers vs Nasdaq (HYPERSCALE/QQQ)",
                "the capex payers lagging the index",
                "the hyperscalers holding up better than the index"),
        ],
        "topping": "QQQ rising on fewer names: the equal-weight Nasdaq and the semis lag while "
                   "the 10y yield climbs -- a narrow, duration-exposed advance.",
        "bottoming": "QQQ still falling but semis and the equal-weight Nasdaq start beating it "
                     "and the 10y yield rolls over -- the engine restarting before the index.",
    },
    "IWM": {
        "family": "cyclical",
        "bet": "a domestic cyclical recovery on cheaper financing",
        "bench": None,   # IWM/SPY is in the table
        "checks": [
            drv("rate5", "^FVX", -1, "5y Treasury yield (^FVX, closest to the 2y)",
                "financing costs rising again -- the 5y yield up",
                "the 5y yield falling -- cheaper financing arriving"),
            rat("credit", "HYG", "IEF", 1, "Credit vs Treasuries (HYG/IEF)",
                "credit not confirming the small-cap rally",
                "high yield firming ahead of small caps"),
            rat("rel", "IWM", "SPY", 1, "Small caps vs S&P (IWM/SPY)",
                "small caps rising but lagging the S&P",
                "small caps falling less than the S&P"),
            rat("banks", "KRE", "IWM", 1, "Regional banks vs small caps (KRE/IWM)",
                "regional banks -- small caps' largest sector -- lagging",
                "regional banks leading the small-cap tape"),
            part(),
        ],
        "topping": "Small caps rise while the 5y yield climbs back and regional banks lag -- "
                   "the cheaper-financing premise fading under the move.",
        "bottoming": "Small caps still down, but the 5y yield falls, high yield firms and "
                     "regional banks start leading -- financing relief arriving first.",
    },
    "EWY": {
        "family": "cyclical",
        "bet": "the memory cycle (SK hynix + Samsung, about 46% of the fund) plus the won",
        "bench": None,   # EWY/EEM is in the table
        "checks": [
            conc("memory", ["000660.KS", "005930.KS"], "SK hynix + Samsung share of the move",
                 "EWY is two memory makers and a country; this says which one moved it"),
            drv("won", "KRWUSD=X", 1, "Korean won (KRWUSD)",
                "the won weakening under the rally",
                "the won strengthening while Korea falls"),
            rat("mu", "MU", "SMH", 1, "Micron vs semis (MU/SMH)",
                "memory lagging -- Micron behind the semis index",
                "memory leading the semis again"),
            rat("em", "EWY", "EEM", 1, "Korea vs emerging markets (EWY/EEM)",
                "Korea lagging EM",
                "Korea beating EM while falling"),
        ],
        "topping": "EWY higher but carried by two memory names while Micron lags the semis and "
                   "the won weakens -- a narrow, currency-unconfirmed memory trade.",
        "bottoming": "EWY still down, but Micron starts beating the semis and the won firms -- "
                     "the memory cycle and the currency turning under the index.",
    },

    # ---------------- Energy ----------------
    "XLE": {
        "family": "commodity",
        "bet": "crude times capital discipline",
        "bench": "SPY",
        "checks": [
            drv("crude", "CL=F", 1, "WTI crude (CL=F)",
                "crude falling under the energy rally",
                "crude rising while energy equities fall"),
            rat("svc", "OIH", "XLE", 1, "Oil services vs sector (OIH/XLE)",
                "activity not confirming -- services lagging",
                "services leading -- activity firming"),
            rat("ep", "XOP", "XLE", 1, "E&P vs sector (XOP/XLE)",
                "the high-beta E&P leg not joining",
                "E&P beta leading a falling sector"),
            split("majors", [("XOM + CVX", ["XOM", "CVX"]), ("the rest", None)],
                  "Majors vs the rest",
                  "XOM and CVX are about 40% of XLE; a majors-only move is a defensive bid, "
                  "not an oil cycle"),
        ],
        "topping": "XLE rising on XOM and CVX alone while crude fades and E&P and services lag "
                   "-- a defensive majors bid, not an oil cycle.",
        "bottoming": "XLE still down, but crude turns up and XOP starts beating the sector -- "
                     "the high-beta end leading the turn.",
    },
    "OIH": {
        "family": "commodity",
        "bet": "the upstream capex cycle, which lags crude by quarters",
        "bench": None,   # OIH/XLE is in the table
        "checks": [
            drv("crude126", "CL=F", 1, "WTI crude over 126d (CL=F)",
                "crude down over six months -- capex budgets under pressure",
                "crude up over six months while services fall", window=126),
            rat("rel", "OIH", "XLE", 1, "Services vs sector (OIH/XLE)",
                "services lagging the sector",
                "services beating the sector"),
            drv("gas", "NG=F", 1, "Natural gas (NG=F)",
                "gas falling -- the gas-directed rig count exposed",
                "gas rising while services fall"),
            split("svcmix", [("SLB + HAL + BKR", ["SLB", "HAL", "BKR"]),
                             ("offshore / drillers", ["RIG", "VAL", "NE", "TDW", "FTI", "OII",
                                                      "HP", "PTEN", "SDRL", "DO"]),
                             ("the rest", None)],
                  "Big three vs offshore / drillers",
                  "the big three follow global capex; offshore and drillers are the "
                  "late-cycle, high-beta leg"),
        ],
        "topping": "Services up while six-month crude rolls over and OIH lags XLE -- the capex "
                   "cycle running on the memory of old prices.",
        "bottoming": "Services still down, but six-month crude is rising and OIH starts beating "
                     "XLE -- budgets catching up to price.",
    },
    "XOP": {
        "family": "commodity",
        "bet": "crude and gas prices at high beta, equal-ish weight",
        "bench": None,   # XOP/XLE is in the table
        "checks": [
            drv("crude21", "CL=F", 1, "WTI crude over 21d (CL=F)",
                "crude falling this month", "crude rising this month", window=21),
            drv("crude63", "CL=F", 1, "WTI crude over 63d (CL=F)",
                "crude falling over the quarter", "crude rising over the quarter"),
            drv("gas", "NG=F", 1, "Natural gas (NG=F)",
                "gas falling under the producers", "gas rising while producers fall"),
            rat("rel", "XOP", "XLE", 1, "E&P vs sector (XOP/XLE)",
                "E&P lagging the sector -- beta not paying",
                "E&P beating the sector while falling"),
            part(),
        ],
        "topping": "XOP up but crude has turned over the last month, gas is soft and fewer "
                   "producers are rising -- beta outrunning the commodity.",
        "bottoming": "XOP still down while crude firms on both 21d and 63d and XOP begins to "
                     "beat XLE -- the commodity turning ahead of the producers.",
    },
    "AMLP": {
        "family": "commodity",
        "bet": "volumes plus yield -- an income vehicle",
        "bench": None,   # AMLP/XLE is in the table
        "checks": [
            drv("rates", "^TNX", -1, "10y Treasury yield (^TNX)",
                "yields rising against the income bid",
                "yields falling -- the income case improving"),
            rat("credit", "HYG", "IEF", 1, "Credit vs Treasuries (HYG/IEF)",
                "high yield lagging Treasuries -- the income trade not confirmed",
                "high yield firming while midstream falls"),
            rat("toll", "AMLP", "XLE", 1, "Midstream vs energy (AMLP/XLE)",
                "toll roads lagging the commodity names",
                "toll roads holding up better than the commodity names"),
        ],
        "topping": "AMLP rising while the 10y climbs and high yield lags Treasuries -- an income "
                   "vehicle fighting its own discount rate.",
        "bottoming": "AMLP still down, but yields fall, credit firms and midstream outperforms "
                     "XLE -- the income case repairing.",
    },
    "URA": {
        "family": "commodity",
        "bet": "uranium price and reactor build-outs, with an SMR speculative layer",
        "bench": "SPY",   # XLE is oil and gas: not uranium's peer group
        "checks": [
            drv("spot", "U-UN.TO", 1, "Physical uranium (Sprott U-UN.TO, in USD)",
                "physical uranium falling under the equities",
                "physical uranium rising while the miners fall"),
            split("layers", [("producers / physical", ["CCO.TO", "CCJ", "NXE", "NXE.TO", "KAP.L",
                                                       "KAP", "U-UN.TO", "UEC", "PDN.AX",
                                                       "EFR.TO", "UUUU", "DML.TO", "DNN", "LEU"]),
                             ("SMR developers", ["OKLO", "SMR", "NNE", "XE"]),
                             ("the rest", None)],
                  "Producers vs SMR developers",
                  "producers move with the fuel price; SMR developers are the speculative "
                  "layer"),
            info_ratio("nlr", "NLR", "URA", "Nuclear utilities vs uranium (NLR/URA)",
                       "operators vs the fuel -- which end of the nuclear trade is leading"),
        ],
        "topping": "URA rising while physical uranium stalls and the SMR names carry the gain -- "
                   "speculation outrunning the fuel price.",
        "bottoming": "URA still down, but physical uranium turns up and the producers, not the "
                     "SMR names, lead the rebound.",
    },

    # ---------------- Materials ----------------
    "XLB": {
        "family": "commodity",
        "bet": "the global industrial cycle and the dollar",
        "bench": "SPY",
        "checks": [
            drv("copper", "HG=F", 1, "Copper (HG=F)",
                "copper falling -- the industrial cycle not confirming",
                "copper rising while materials fall"),
            drv("dollar", "UUP", -1, "US dollar (UUP)",
                "the dollar strengthening against the materials bid",
                "the dollar weakening while materials fall"),
            drv("china", "FXI", 1, "China large caps (FXI)",
                "China equities falling -- the largest metals consumer not confirming",
                "China equities rising first"),
            split("mix", [("chemicals", ["SHW", "ECL", "CTVA", "DD", "DOW", "PPG", "IFF", "LYB",
                                         "CF", "MOS", "ALB", "EMN", "CE", "FMC"]),
                          ("metals / mining", ["NEM", "FCX", "NUE", "STLD", "RS"]),
                          ("gases", ["LIN", "APD"]),
                          ("the rest", None)],
                  "Chemicals vs metals / mining vs gases",
                  "gases and gold are not the industrial cycle; chemicals and base metals are"),
        ],
        "topping": "XLB up on the gases and the gold/copper miners while copper and China fade "
                   "and the dollar firms -- not the broad industrial cycle.",
        "bottoming": "XLB still down, but copper and China equities turn up and the dollar "
                     "softens -- the cycle inputs turning before the sector.",
    },
    "XME": {
        "family": "commodity",
        "bet": "US metals and mining, equal weight",
        "bench": "SPY",
        "checks": [
            drv("steel", "HRC=F", 1, "Hot-rolled coil steel (HRC=F)",
                "steel prices falling under the miners", "steel prices rising"),
            drv("copper", "HG=F", 1, "Copper (HG=F)",
                "copper falling under the miners", "copper rising"),
            drv("gold", "GC=F", 1, "Gold (GC=F)",
                "gold falling under the miners", "gold rising"),
            drv("dollar", "UUP", -1, "US dollar (UUP)",
                "the dollar strengthening against metals", "the dollar weakening"),
            part(),
        ],
        "topping": "XME rising on fewer names while steel and copper roll over -- gold alone "
                   "carrying an equal-weight metals fund.",
        "bottoming": "XME still down, but steel and copper firm and more members rise -- the "
                     "equal-weight basket turning from the inside.",
    },
    "SLX": {
        "family": "commodity",
        "bet": "steel prices behind tariffs",
        "bench": None,   # SLX/XME is in the table
        "checks": [
            drv("hrc", "HRC=F", 1, "Hot-rolled coil steel (HRC=F)",
                "US steel prices falling -- the tariff premium fading",
                "US steel prices rising while the fund falls"),
            rat("rel", "SLX", "XME", 1, "Steel vs metals & mining (SLX/XME)",
                "steel lagging the wider metals complex",
                "steel beating the wider metals complex"),
            split("ore", [("iron-ore miners", ["BHP", "RIO", "VALE", "RIO.AX", "BHP.AX",
                                               "FMG.AX", "RIO.L"]),
                          ("steelmakers", ["NUE", "STLD", "MT", "5401.T", "PKX", "RS", "CLF",
                                           "CMC", "TX", "GGB", "SID", "X"]),
                          ("the rest", None)],
                  "Iron-ore miners vs steelmakers",
                  "SLX's largest weights are iron-ore miners; a move they carry is an ore "
                  "trade, not a steel-price trade"),
            part(),
        ],
        "topping": "SLX up while hot-rolled coil prices fall and the iron-ore miners, not the "
                   "steelmakers, carry it -- the tariff premium fading.",
        "bottoming": "SLX still down, but HRC turns up and the steelmakers start leading the "
                     "iron-ore names.",
    },
    "COPX": {
        "family": "commodity",
        "bet": "copper, with miners' operating leverage",
        "bench": "SPY",
        "checks": [
            drv("copper", "HG=F", 1, "Copper (HG=F)",
                "copper falling under the miners", "copper rising while the miners fall"),
            rat("lev", "COPX", "HG=F", 1, "Miners vs copper (COPX/HG=F)",
                "leverage failing -- miners lagging copper",
                "miners beating copper -- leverage returning"),
            drv("china", "FXI", 1, "China large caps (FXI)",
                "China equities falling -- the largest copper consumer not confirming",
                "China equities rising first"),
            drv("dollar", "UUP", -1, "US dollar (UUP)",
                "the dollar strengthening against copper", "the dollar weakening"),
            part(),
        ],
        "topping": "Copper holds but the miners stop beating it and fewer of them rise -- "
                   "operating leverage failing while the metal is still firm.",
        "bottoming": "Copper still soft, but COPX starts beating the metal and China equities "
                     "firm -- leverage turning before price.",
    },
    "LIT": {
        "family": "commodity",
        "bet": "lithium price and EV/storage demand, China-heavy",
        "bench": "SPY",
        "checks": [
            split("chain", [("lithium miners", ["ALB", "SQM", "PLS.AX", "MIN.AX"]),
                            ("Rio Tinto", ["RIO", "RIO.L", "RIO.AX"]),
                            ("cells / EV", None)],
                  "Lithium miners vs Rio Tinto vs cells / EV",
                  "Rio Tinto (via Arcadium) is LIT's largest weight but moves with iron ore "
                  "and copper, not lithium"),
            drv("alb", "ALB", 1, "Albemarle (the lithium-price proxy)",
                "Albemarle falling -- the lithium price not confirming",
                "Albemarle rising -- the lithium price turning"),
            drv("china", "FXI", 1, "China large caps (FXI)",
                "China equities falling under the chain", "China equities rising first"),
            rat("ev", "LIT", "DRIV", 1, "Lithium chain vs autos / EV (LIT/DRIV)",
                "the lithium chain lagging the EV names",
                "the lithium chain beating the EV names"),
        ],
        "topping": "LIT rising on Rio Tinto and the cell makers while Albemarle lags -- the fund "
                   "up without the lithium price.",
        "bottoming": "LIT still down, but Albemarle turns and LIT starts beating DRIV -- the "
                     "lithium price leading the chain.",
    },
    "REMX": {
        "family": "commodity",
        "bet": "rare-earth and strategic-metal pricing set by Chinese export policy",
        "caveat": "policy-driven: durability evidence is thin by construction",
        "bench": None,   # REMX/XME is in the table
        "checks": [
            conc("rare", ["MP", "LYC.AX"], "MP + Lynas share of the move",
                 "the two non-Chinese rare-earth producers; a move they alone make is a "
                 "policy-headline trade"),
            split("mix", [("rare earths", ["MP", "LYC.AX", "600111.SS", "ILU.AX", "ARU.AX",
                                           "600392.SS", "000831.SZ"]),
                          ("lithium", ["PLS.AX", "ALB", "SQM", "MIN.AX", "LTR.AX"]),
                          ("the rest", None)],
                  "Rare earths vs lithium",
                  "about a fifth of REMX is lithium, a different price cycle"),
            rat("rel", "REMX", "XME", 1, "Strategic metals vs metals & mining (REMX/XME)",
                "strategic metals lagging the wider metals complex",
                "strategic metals beating the wider metals complex"),
        ],
        "topping": "REMX up with MP and Lynas carrying the move while REMX lags XME -- a policy "
                   "headline trade narrowing to two names.",
        "bottoming": "REMX still down, but the rare-earth names outpace the lithium names and "
                     "REMX starts beating XME.",
    },
    "GDX": {
        "family": "commodity",
        "bet": "gold, with miners' margin leverage (price minus AISC)",
        "bench": "SPY",
        "checks": [
            drv("gold", "GC=F", 1, "Gold (GC=F)",
                "gold falling under the miners", "gold rising while the miners fall"),
            rat("lev", "GDX", "GLD", 1, "Miners vs bullion (GDX/GLD)",
                "leverage failing -- miners lagging gold",
                "miners beating gold again -- leverage returning"),
            rat("jr", "GDXJ", "GDX", 1, "Juniors vs majors (GDXJ/GDX)",
                "juniors not joining", "juniors leading the majors"),
            drv("rates", "^TNX", -1, "10y yield (^TNX, the real-rate leg by proxy)",
                "the 10y yield rising -- the real-rate leg against gold",
                "yields falling -- the real-rate leg turning"),
            drv("dollar", "UUP", -1, "US dollar (UUP)",
                "the dollar strengthening against gold", "the dollar weakening"),
        ],
        "topping": "Gold still rising but GDX stops beating GLD and the juniors lag -- leverage "
                   "failing while the metal holds, the classic miner top.",
        "bottoming": "Miners still down, but GDX starts beating GLD and GDXJ leads GDX -- "
                     "leverage returning ahead of the trend.",
    },
    "SILJ": {
        "family": "commodity",
        "bet": "silver, with junior leverage",
        "bench": "SPY",
        "checks": [
            drv("silver", "SI=F", 1, "Silver (SI=F)",
                "silver falling under the juniors", "silver rising while the juniors fall"),
            rat("lev", "SILJ", "SLV", 1, "Junior silver miners vs silver (SILJ/SLV)",
                "leverage failing -- juniors lagging the metal",
                "juniors beating the metal -- leverage returning"),
            rat("gsr", "GC=F", "SI=F", -1, "Gold/silver ratio (GC=F/SI=F)",
                "the gold/silver ratio rising -- silver lagging gold",
                "the ratio falling -- silver outpacing gold"),
        ],
        "topping": "Silver holds but junior silver miners stop beating SLV and the gold/silver "
                   "ratio climbs -- the high-beta end fading first.",
        "bottoming": "Juniors still down, but SILJ starts beating SLV and silver outpaces gold "
                     "-- leverage and the ratio turning together.",
    },
    "SLV": {
        "family": "commodity",
        "bet": "silver as monetary plus industrial metal",
        "bench": "SPY",   # a metal has no sector ETF
        "checks": [
            drv("gold", "GC=F", 1, "Gold (the monetary leg, GC=F)",
                "gold falling -- the monetary leg gone", "gold rising -- the monetary leg turning"),
            drv("copper", "HG=F", 1, "Copper (the industrial leg, HG=F)",
                "copper falling -- the industrial leg gone",
                "copper rising -- the industrial leg turning"),
            rat("gsr", "GC=F", "SI=F", -1, "Gold/silver ratio (GC=F/SI=F)",
                "the gold/silver ratio rising -- silver lagging gold",
                "the ratio falling -- silver outpacing gold"),
            drv("dollar", "UUP", -1, "US dollar (UUP)",
                "the dollar strengthening against silver", "the dollar weakening"),
        ],
        "topping": "Silver up while copper rolls over and the gold/silver ratio rises -- the "
                   "industrial leg gone, only the monetary bid left.",
        "bottoming": "Silver still down, but copper and gold both turn and silver starts "
                     "outpacing gold.",
    },

    # ---------------- Industrials ----------------
    "XLI": {
        "family": "cyclical",
        "bet": "the capex cycle -- now split between electrical/AI-power and the broad cycle",
        "bench": "SPY",
        "checks": [
            rat("trans", "IYT", "XLI", 1, "Transports vs industrials (IYT/XLI)",
                "transports not confirming -- goods volumes lagging",
                "transports leading the industrials"),
            info_ratio("whose", "GRID", "XLI", "Grid equipment vs industrials (GRID/XLI)",
                       "whose cycle it is: rising = the electrical/AI-power capex, falling = the "
                       "broad cycle"),
            drv("copper", "HG=F", 1, "Copper (HG=F)",
                "copper falling -- the broad cycle not confirming",
                "copper rising while industrials fall"),
            conc("conc", TOP3, "Top-3 share of the move",
                 "a capex theme concentrated in a few electrical names reads narrow"),
        ],
        "topping": "XLI higher on the electrical/AI-power names while transports and copper lag "
                   "-- one capex theme, not the broad cycle.",
        "bottoming": "XLI still down, but transports start beating it and copper firms -- the "
                     "goods cycle turning under the sector.",
    },
    "ITA": {
        "family": "cyclical",
        "bet": "defense budgets plus commercial aerospace",
        "bench": None,   # ITA/SPY is in the table
        "checks": [
            split("mix", [("defense primes", ["LMT", "RTX", "NOC", "GD", "LHX"]),
                          ("commercial aero", ["GE", "BA", "HWM", "TDG"]),
                          ("the rest", None)],
                  "Defense primes vs commercial aero",
                  "budgets and air traffic are two different cycles in one fund"),
            rat("eqw", "XAR", "ITA", 1, "Equal-weight vs cap-weight A&D (XAR/ITA)",
                "breadth fading -- equal-weight A&D lagging",
                "the smaller A&D names holding up better"),
            rat("rel", "ITA", "SPY", 1, "Aerospace & defense vs S&P (ITA/SPY)",
                "A&D rising but lagging the S&P",
                "A&D beating the S&P while falling"),
        ],
        "topping": "ITA up on GE and Boeing while XAR lags -- commercial aero carrying a "
                   "narrowing sector.",
        "bottoming": "ITA still down, but XAR starts beating it and ITA beats SPY -- breadth "
                     "returning to defense.",
    },
    "GRID": {
        "family": "theme",
        "bet": "utility T&D capex plus data-center power",
        "bench": None,   # GRID/XLI is in the table
        "checks": [
            drv("ceg", "CEG", 1, "Constellation Energy (AI-power demand, CEG)",
                "CEG falling -- the AI-power demand leg not confirming",
                "CEG rising while grid equipment falls"),
            drv("xlu", "XLU", 1, "Utilities (the funders' capacity, XLU)",
                "utilities falling -- the funders' capacity under pressure",
                "utilities rising while grid equipment falls"),
            rat("sec", "GRID", "XLI", 1, "Grid vs industrials (GRID/XLI)",
                "secular lagging cyclical -- GRID behind XLI",
                "GRID beating XLI again"),
            conc("conc", TOP3, "Top-3 share of the move",
                 "whether a few electrical names carry the fund"),
        ],
        "topping": "GRID rising while CEG and the utilities that fund the capex roll over -- the "
                   "demand story ahead of the funders.",
        "bottoming": "GRID still down, but CEG and XLU turn up and GRID beats XLI again -- "
                     "secular over cyclical returning.",
    },
    "PAVE": {
        "family": "cyclical",
        "bet": "US infrastructure spending",
        "bench": None,   # PAVE/XLI is in the table
        "checks": [
            rat("rel", "PAVE", "XLI", 1, "Infrastructure vs industrials (PAVE/XLI)",
                "infrastructure lagging the industrials",
                "infrastructure beating the industrials"),
            drv("steel", "HRC=F", 1, "Hot-rolled coil steel (HRC=F)",
                "steel prices falling -- construction demand not confirming",
                "steel prices rising"),
            drv("rates", "^TNX", -1, "10y Treasury yield (^TNX)",
                "yields rising against project financing", "yields falling"),
            part(),
        ],
        "topping": "PAVE up but lagging XLI while steel prices fall and yields rise -- spending "
                   "priced ahead of the inputs.",
        "bottoming": "PAVE still down, but it begins beating XLI, steel firms and more members "
                     "rise.",
    },
    "IYT": {
        "family": "cyclical",
        "bet": "freight volumes -- the goods economy",
        "bench": None,   # IYT/SPY is in the table
        "checks": [
            rat("dow", "IYT", "SPY", 1, "Transports vs S&P (Dow-theory, IYT/SPY)",
                "transports not confirming the market",
                "transports beating the market while falling"),
            rat("eqw", "XTN", "IYT", 1, "Equal-weight vs price-weighted transports (XTN/IYT)",
                "the average transport lagging the price-weighted index",
                "the average transport holding up better"),
            drv("oil", "CL=F", -1, "WTI crude (fuel cost, CL=F)",
                "fuel costs rising under the transports", "fuel costs falling"),
            split("mix", [("rails", ["UNP", "CSX", "NSC", "CP", "CNI"]),
                          ("trucking", ["ODFL", "JBHT", "XPO", "SAIA", "KNX", "LSTR", "CHRW"]),
                          ("Uber", ["UBER"]),
                          ("the rest", None)],
                  "Rails vs trucking vs Uber vs the rest",
                  "Uber is about 15% of IYT and is not freight"),
        ],
        "topping": "IYT up while it lags SPY, XTN lags IYT and crude climbs -- freight not "
                   "confirming, with Uber carrying the price-weighted index.",
        "bottoming": "IYT still down, but XTN starts beating IYT, crude eases and rails or "
                     "truckers lead -- freight turning under the index.",
    },
    "JETS": {
        "family": "cyclical",
        "bet": "travel demand against jet fuel",
        "bench": None,   # JETS/SPY is in the table
        "checks": [
            drv("fuel", "HO=F", -1, "Heating oil (about jet fuel, HO=F)",
                "jet fuel rising under the airline rally",
                "jet fuel falling while airlines fall"),
            rat("rel", "JETS", "SPY", 1, "Airlines vs S&P (JETS/SPY)",
                "airlines lagging the market", "airlines beating the market while falling"),
            drv("pej", "PEJ", 1, "Leisure & entertainment (PEJ)",
                "the wider travel-and-leisure demand falling",
                "travel-and-leisure demand turning up"),
            conc("big4", ["DAL", "UAL", "AAL", "LUV"], "Big four carriers' share of the move",
                 "the four US majors vs the regional and foreign carriers"),
        ],
        "topping": "Airlines up while jet fuel climbs and PEJ softens -- the demand leg fading "
                   "as costs rise.",
        "bottoming": "Airlines still down, but jet fuel falls and PEJ turns up -- margins and "
                     "demand repairing together.",
    },

    # ---------------- Consumer ----------------
    "XLY": {
        "family": "cyclical",
        "bet": "the consumer -- but AMZN + TSLA are about 40% of it",
        "bench": "SPY",
        "checks": [
            conc("amzn_tsla", ["AMZN", "TSLA"], "AMZN + TSLA share of the move",
                 "is it a consumer read at all, or two stocks"),
            rat("breadth", "XRT", "XLY", 1, "Median shopper vs the sector (XRT/XLY)",
                "the median retailer lagging -- breadth of the consumer fading",
                "the median retailer holding up better than the sector"),
            rat("offdef", "XLY", "XLP", 1, "Discretionary vs staples (XLY/XLP)",
                "discretionary lagging staples", "discretionary beating staples while falling"),
            drv("rates", "^TNX", -1, "10y Treasury yield (^TNX)",
                "yields rising on the consumer", "yields falling"),
        ],
        "topping": "XLY rising on AMZN and TSLA while XRT and XLY/XLP lag -- two stocks, not "
                   "the consumer.",
        "bottoming": "XLY still down, but XRT starts beating it and discretionary gains on "
                     "staples -- the median consumer turning first.",
    },
    "DRIV": {
        "family": "theme",
        "bet": "autos/EV/autonomy -- TSLA plus chips",
        "bench": None,   # DRIV/XLY is in the table
        "checks": [
            conc("tsla", ["TSLA"], "Tesla's share of the move",
                 "one name vs the auto, chip and battery chain"),
            rat("rel", "DRIV", "XLY", 1, "Autos / EV vs discretionary (DRIV/XLY)",
                "the auto/EV chain lagging discretionary",
                "the auto/EV chain beating discretionary"),
            drv("china", "FXI", 1, "China large caps (China EV, FXI)",
                "China equities falling -- the China EV leg not confirming",
                "China equities rising first"),
        ],
        "topping": "DRIV up on Tesla and the chip names while it lags XLY and China equities "
                   "fade -- the EV leg missing.",
        "bottoming": "DRIV still down, but it starts beating XLY and China equities firm -- the "
                     "auto/EV leg turning.",
    },
    "ITB": {
        "family": "cyclical",
        "bet": "new-home demand at today's mortgage rate",
        "bench": None,   # ITB/SPY is in the table
        "checks": [
            drv("rates", "^TNX", -1, "10y yield (mortgage rates track it, ^TNX)",
                "the 10y yield rising under the builders",
                "the 10y yield falling -- mortgage relief"),
            rat("chain", "XHB", "ITB", 1, "Wider housing chain vs builders (XHB/ITB)",
                "the wider housing chain not joining",
                "the wider housing chain holding up better"),
            rat("rel", "ITB", "SPY", 1, "Homebuilders vs S&P (ITB/SPY)",
                "builders lagging the market", "builders beating the market while falling"),
            conc("big4", ["DHI", "LEN", "PHM", "NVR"], "Big four builders' share of the move",
                 "the four largest builders vs the suppliers and retailers"),
        ],
        "topping": "Builders up while the 10y yield climbs and XHB lags -- the rally running "
                   "ahead of mortgage rates.",
        "bottoming": "Builders still down, but the 10y yield falls and XHB starts beating ITB -- "
                     "the wider chain turning with rates.",
    },
    "PEJ": {
        "family": "cyclical",
        "bet": "experiences spending -- travel, leisure, live events",
        "bench": None,   # PEJ/XLY is in the table
        "checks": [
            rat("rel", "PEJ", "XLY", 1, "Leisure vs discretionary (PEJ/XLY)",
                "experiences lagging the wider consumer",
                "experiences beating the wider consumer"),
            drv("jets", "JETS", 1, "Airlines (JETS)",
                "airlines falling -- travel demand not confirming", "airlines turning up"),
            rat("offdef", "XLY", "XLP", 1, "Discretionary vs staples (XLY/XLP)",
                "the consumer rotating defensive", "discretionary gaining on staples"),
            part(),
        ],
        "topping": "PEJ up while airlines and discretionary-vs-staples roll over -- experiences "
                   "holding while the wider consumer fades.",
        "bottoming": "PEJ still down, but airlines turn up and PEJ begins beating XLY.",
    },
    "XRT": {
        "family": "cyclical",
        "bet": "the median US shopper (equal weight)",
        "bench": None,   # XRT/XLY is in the table
        "checks": [
            part(),
            rat("rel", "XRT", "XLY", 1, "Median retailer vs discretionary (XRT/XLY)",
                "the median retailer lagging the cap-weighted sector",
                "the median retailer holding up better"),
            drv("rates", "^TNX", -1, "10y Treasury yield (^TNX)",
                "yields rising on the shopper", "yields falling"),
            rat("credit", "HYG", "IEF", 1, "Credit vs Treasuries (HYG/IEF)",
                "credit softening under the retailers", "credit firming"),
        ],
        "topping": "XRT up on fewer names while it lags XLY and credit softens -- the median "
                   "shopper not confirming.",
        "bottoming": "XRT still down, but more members rise, XRT beats XLY and high yield firms.",
    },
    "XLP": {
        "family": "defensive",
        "bet": "a defensive rotation -- bond proxy plus low beta",
        "bench": None,   # XLP/SPY is in the table
        "checks": [
            rat("rel", "XLP", "SPY", 1, "Staples vs S&P (XLP/SPY)",
                "no relative strength -- a rotation needs it",
                "staples beating the market while falling"),
            drv("rates", "^TNX", -1, "10y Treasury yield (^TNX)",
                "yields rising against the bond proxy", "yields falling"),
            rat("xlu", "XLU", "SPY", 1, "Utilities vs S&P (XLU/SPY)",
                "utilities not joining the defensive rotation",
                "utilities leading a defensive turn"),
            rat("xlv", "XLV", "SPY", 1, "Health care vs S&P (XLV/SPY)",
                "health care not joining the defensive rotation",
                "health care leading a defensive turn"),
            conc("growthy", ["COST", "WMT"], "COST + WMT share of the move",
                 "the two growth-like staples; a move they alone carry is not a defensive "
                 "rotation"),
        ],
        "topping": "XLP up but lagging SPY while utilities and health care also lag and yields "
                   "rise -- not a real defensive rotation, just COST and WMT.",
        "bottoming": "XLP still down, but it starts beating SPY alongside XLU and XLV -- a broad "
                     "defensive rotation forming.",
    },
    "PBJ": {
        "family": "defensive",
        "bet": "food and beverage pricing power against volumes",
        "bench": None,   # PBJ/XLP is in the table
        "checks": [
            rat("rel", "PBJ", "XLP", 1, "Food & beverage vs staples (PBJ/XLP)",
                "food and beverage lagging the staples sector",
                "food and beverage beating the staples sector"),
            part(),
            rat("defbid", "XLP", "SPY", 1, "Staples vs S&P (the defensive bid, XLP/SPY)",
                "the defensive bid PBJ rides fading", "a defensive bid forming"),
        ],
        "topping": "PBJ up but lagging XLP with fewer members rising -- pricing power running "
                   "out of volume.",
        "bottoming": "PBJ still down, but more members rise and it starts beating XLP.",
    },

    # ---------------- Health care ----------------
    "XLV": {
        "family": "defensive",
        "bet": "defensive plus pharma/managed-care fundamentals; LLY is the swing weight",
        "bench": None,   # XLV/SPY is in the table
        "checks": [
            conc("lly", ["LLY"], "LLY's share of the move", "one franchise vs the sector"),
            split("mix", [("pharma", ["LLY", "JNJ", "ABBV", "MRK", "PFE"]),
                          ("managed care", ["UNH", "ELV", "CI", "HUM", "CVS"]),
                          ("devices / tools", ["TMO", "ABT", "ISRG", "DHR", "SYK"]),
                          ("the rest", None)],
                  "Pharma vs managed care vs devices / tools",
                  "three businesses with three different drivers"),
            rat("rel", "XLV", "SPY", 1, "Health care vs S&P (XLV/SPY)",
                "health care lagging the market", "health care beating the market while falling"),
            part(),
        ],
        "topping": "XLV up on LLY while managed care and devices lag and fewer members rise -- "
                   "one franchise carrying the sector.",
        "bottoming": "XLV still down, but more members rise and it starts beating SPY -- the "
                     "defensive bid broadening.",
    },
    "IHI": {
        "family": "defensive",
        "bet": "procedure volumes and long-duration device growth",
        "bench": None,   # IHI/XLV is in the table
        "checks": [
            rat("rel", "IHI", "XLV", 1, "Devices vs health care (IHI/XLV)",
                "devices lagging health care", "devices beating health care"),
            drv("rates", "^TNX", -1, "10y Treasury yield (^TNX)",
                "the 10y yield rising against long-dated device growth",
                "yields falling -- duration relief"),
            conc("conc", TOP3, "Top-3 share of the move",
                 "ABT, ISRG and SYK are about 40% of IHI"),
            part(),
        ],
        "topping": "IHI up but lagging XLV while the 10y yield rises -- duration working "
                   "against long-dated device growth.",
        "bottoming": "IHI still down, but yields ease and IHI starts beating XLV.",
    },
    "IHF": {
        "family": "defensive",
        "bet": "managed-care margins vs utilization and Medicare policy",
        "bench": None,   # IHF/XLV is in the table
        "checks": [
            rat("rel", "IHF", "XLV", 1, "Providers vs health care (IHF/XLV)",
                "providers lagging health care", "providers beating health care"),
            conc("unh", ["UNH"], "UNH's share of the move",
                 "UNH is about a fifth of IHF; policy relief in one name reads narrow"),
            part(),
        ],
        "topping": "IHF up with UNH carrying most of it and fewer members rising -- policy "
                   "relief concentrated in one name.",
        "bottoming": "IHF still down, but more members rise and it starts beating XLV -- "
                     "utilization fears easing beyond UNH.",
    },
    "XBI": {
        "family": "growth",
        "bet": "the small/mid biotech financing window and M&A",
        "bench": "XLV",
        "checks": [
            rat("appetite", "XBI", "IBB", 1, "Small vs large biotech (XBI/IBB)",
                "appetite narrowing -- small biotech lagging large",
                "small biotech outperforming while the group falls"),
            drv("rates", "^TNX", -1, "10y Treasury yield (^TNX)",
                "yields rising on pre-revenue duration", "yields falling"),
            rat("smallcap", "IWM", "SPY", 1, "Small caps vs S&P (IWM/SPY)",
                "small-cap appetite fading", "small-cap appetite returning"),
            rat("credit", "HYG", "IEF", 1, "Credit (the financing window, HYG/IEF)",
                "the financing window narrowing -- high yield lagging",
                "the financing window reopening -- high yield firming"),
            part(),
        ],
        "topping": "XBI up while it lags IBB, yields rise and high yield softens -- the "
                   "financing window narrowing under the rally.",
        "bottoming": "XBI still down, but it starts beating IBB and credit firms -- small "
                     "biotech outperforming while the group falls, the classic early turn.",
    },
    "XPH": {
        "family": "defensive",
        "bet": "pharma pricing, policy and the GLP-1 franchises (equal weight)",
        "bench": None,   # XPH/XLV is in the table
        "checks": [
            rat("rel", "XPH", "XLV", 1, "Pharma vs health care (XPH/XLV)",
                "pharma lagging health care", "pharma beating health care"),
            part(),
            drv("rates", "^TNX", -1, "10y Treasury yield (^TNX)",
                "yields rising against the defensive bid", "yields falling"),
        ],
        "topping": "XPH up but lagging XLV with fewer members rising -- pharma breadth fading.",
        "bottoming": "XPH still down, but more members rise and XPH starts beating XLV.",
    },

    # ---------------- Financials ----------------
    "XLF": {
        "family": "cyclical",
        "bet": "net interest income, credit and capital-markets activity",
        "bench": "SPY",
        "checks": [
            crv("curve", "^TNX", "^IRX", 1, "Yield curve, 10y minus 3m (^TNX - ^IRX)",
                "the curve flattening -- NIM against the move",
                "the curve steepening while financials fall"),
            rat("credit", "HYG", "IEF", 1, "Credit vs Treasuries (HYG/IEF)",
                "credit softening under the financials", "credit firming"),
            rat("regionals", "KRE", "XLF", 1, "Regionals vs sector (KRE/XLF)",
                "breadth not reaching the regionals", "regionals leading"),
            split("mix", [("banks", ["JPM", "BAC", "WFC", "C"]),
                          ("payments", ["V", "MA", "AXP"]),
                          ("brokers", ["GS", "MS", "SCHW"]),
                          ("the rest", None)],
                  "Banks vs payments vs brokers",
                  "lending, payments and capital markets are three different trades"),
        ],
        "topping": "XLF up on payments and brokers while the curve flattens, credit softens and "
                   "regionals lag -- not a lending story.",
        "bottoming": "XLF still down, but the curve steepens, credit firms and regionals start "
                     "leading.",
    },
    "KBE": {
        "family": "cyclical",
        "bet": "bank NIM and credit, equal weight",
        "bench": None,   # KBE/XLF is in the table
        "checks": [
            crv("curve", "^TNX", "^IRX", 1, "Yield curve, 10y minus 3m (^TNX - ^IRX)",
                "the curve flattening -- NIM against the move",
                "the curve steepening while banks fall"),
            rat("credit", "HYG", "IEF", 1, "Credit vs Treasuries (HYG/IEF)",
                "credit softening under the banks", "credit firming"),
            rat("rel", "KBE", "XLF", 1, "Banks vs financials (KBE/XLF)",
                "banks lagging the wider sector", "banks beating the wider sector"),
            part(),
        ],
        "topping": "Banks up while the curve flattens and credit softens -- NIM and credit both "
                   "against the move.",
        "bottoming": "Banks still down, but the curve steepens, credit firms and more banks "
                     "rise.",
    },
    "KRE": {
        "family": "cyclical",
        "bet": "regional NIM, deposit stability, CRE credit",
        "bench": "XLF",
        "checks": [
            drv("front", "^IRX", -1, "3-month bill yield (^IRX)",
                "short rates rising -- deposit costs up",
                "short rates falling -- deposit pressure easing"),
            crv("curve", "^TNX", "^IRX", 1, "Yield curve, 10y minus 3m (^TNX - ^IRX)",
                "the curve flattening -- NIM against the move",
                "the curve steepening while regionals fall"),
            rat("credit", "HYG", "IEF", 1, "Credit vs Treasuries (HYG/IEF)",
                "credit widening under the regionals", "credit firming"),
            rat("cre", "XLRE", "SPY", 1, "Real estate vs S&P (CRE proxy, XLRE/SPY)",
                "real estate lagging -- CRE credit pressure",
                "real estate firming -- CRE pressure easing"),
            part(),
        ],
        "topping": "KRE up while the curve steepens because long yields jump and credit widens "
                   "-- a bear steepener, the wrong kind for regionals.",
        "bottoming": "Regionals still down, but short rates fall, the curve steepens from the "
                     "front end and credit and REITs firm -- a bull steepener.",
    },
    "IAI": {
        "family": "cyclical",
        "bet": "capital-markets activity and retail engagement",
        "bench": None,   # IAI/XLF is in the table
        "checks": [
            rat("rel", "IAI", "XLF", 1, "Capital markets vs financials (IAI/XLF)",
                "capital markets lagging the sector", "capital markets beating the sector"),
            split("mix", [("GS + MS + SCHW", ["GS", "MS", "SCHW"]),
                          ("HOOD + IBKR + COIN", ["HOOD", "IBKR", "COIN"]),
                          ("the rest", None)],
                  "Wirehouses vs retail platforms",
                  "institutional activity vs retail engagement"),
            drv("btc", "BTC-USD", 1, "Bitcoin (retail risk appetite, BTC-USD)",
                "bitcoin falling -- retail engagement cooling",
                "bitcoin rising -- retail engagement returning"),
        ],
        "topping": "IAI up on the retail platforms while bitcoin fades and IAI lags XLF -- "
                   "engagement cooling under the move.",
        "bottoming": "IAI still down, but bitcoin turns up and IAI starts beating XLF.",
    },
    "FINX": {
        "family": "growth",
        "bet": "payments/fintech growth plus crypto sentiment",
        "bench": None,   # FINX/XLF is in the table
        "checks": [
            rat("rel", "FINX", "XLF", 1, "Fintech vs financials (FINX/XLF)",
                "fintech lagging the incumbents", "fintech beating the incumbents"),
            drv("btc", "BTC-USD", 1, "Bitcoin (BTC-USD)",
                "bitcoin falling -- crypto sentiment fading", "bitcoin rising"),
            drv("rates", "^TNX", -1, "10y Treasury yield (^TNX)",
                "yields rising on growth multiples", "yields falling"),
            conc("conc", TOP3, "Top-3 share of the move",
                 "whether HOOD, XYZ and PYPL are the whole move"),
        ],
        "topping": "FINX up while bitcoin rolls over and yields rise -- crypto sentiment fading "
                   "under the fintech names.",
        "bottoming": "FINX still down, but bitcoin turns up, yields ease and FINX beats XLF.",
    },
    "KIE": {
        "family": "defensive",
        "bet": "the insurance pricing cycle plus investment income",
        "bench": None,   # KIE/XLF is in the table
        "checks": [
            rat("rel", "KIE", "XLF", 1, "Insurance vs financials (KIE/XLF)",
                "insurers lagging the sector", "insurers beating the sector"),
            drv("rates", "^TNX", 1, "10y yield (investment income, ^TNX)",
                "yields falling -- the investment-income leg fading",
                "yields rising -- investment income improving"),
            part(),
        ],
        "topping": "KIE up while yields fall and it lags XLF with fewer members rising -- "
                   "pricing and investment income both softening.",
        "bottoming": "KIE still down, but yields rise and more insurers climb -- the income leg "
                     "turning.",
    },

    # ---------------- Technology ----------------
    "XLK": {
        "family": "growth",
        "bet": "AI hardware (NVDA, AVGO) plus mega-cap software/devices",
        "bench": "SPY",
        "checks": [
            conc("big3", ["NVDA", "MSFT", "AAPL"], "NVDA + MSFT + AAPL share of the move",
                 "three names carry a large share of XLK's weight"),
            rat("semis", "SMH", "XLK", 1, "Semis vs tech (SMH/XLK)",
                "the AI hardware leg lagging the sector",
                "the AI hardware leg leading a falling sector"),
            info_ratio("sw", "IGV", "XLK", "Software vs tech (IGV/XLK)",
                       "which half of tech is moving it: software or hardware"),
            drv("rates", "^TNX", -1, "10y Treasury yield (^TNX)",
                "yields rising on long-duration tech", "yields falling"),
        ],
        "topping": "XLK up on NVDA, MSFT and AAPL while semis lag the sector and yields rise -- "
                   "the AI hardware leg tiring.",
        "bottoming": "XLK still down, but semis start beating it and yields ease.",
    },
    "IGV": {
        "family": "growth",
        "bet": "software earnings durability against AI disruption, and duration",
        "bench": "XLK",
        "checks": [
            rat("vs_semis", "IGV", "SMH", 1, "Software vs semis (IGV/SMH)",
                "money leaving software monetization for semis",
                "money moving back to software from semis"),
            rat("saas", "WCLD", "IGV", 1, "High-multiple SaaS vs software (WCLD/IGV)",
                "high-multiple SaaS not joining", "high-multiple SaaS leading"),
            drv("rates", "^TNX", -1, "10y Treasury yield (^TNX)",
                "yields rising on software multiples", "yields falling -- duration relief"),
            conc("conc", TOP3, "Top-3 share of the move",
                 "PLTR, PANW and MSFT are about 30% of IGV"),
        ],
        "topping": "IGV up on a few large names while WCLD lags and yields rise -- high-multiple "
                   "SaaS not joining.",
        "bottoming": "IGV still down, but it starts beating SMH and WCLD leads -- money moving "
                     "back to software monetization.",
    },
    "CIBR": {
        "family": "growth",
        "bet": "non-discretionary security spend",
        "bench": None,   # CIBR/IGV is in the table (software is the peer group)
        "checks": [
            rat("rel", "CIBR", "IGV", 1, "Cybersecurity vs software (CIBR/IGV)",
                "security lagging software -- the non-discretionary premium fading",
                "security holding up better than software"),
            conc("conc", TOP3, "Top-3 share of the move",
                 "PANW, CRWD and FTNT vs the rest of the fund"),
            part(),
        ],
        "topping": "CIBR up on its top three while fewer members rise and it lags IGV -- the "
                   "non-discretionary bid narrowing.",
        "bottoming": "CIBR still down, but it starts beating IGV and more members rise.",
    },
    "OPTICS": {
        "family": "theme",
        "bet": "the AI optical-interconnect ramp funded by hyperscaler capex",
        "bench": "XLK",
        "checks": [
            part(label="Members rising (6)",
                 up="fewer of the six optics names rising",
                 dn="most of the six optics names rising while the basket falls"),
            rat("beta", "AAOI", "GLW", 1, "High-beta vs low-beta member (AAOI/GLW)",
                "the high-beta end fading -- AAOI lagging Corning",
                "AAOI beating Corning -- speculative appetite returning"),
            drv("payers", "HYPERSCALE", 1, "The payers (HYPERSCALE basket)",
                "the hyperscalers falling -- the funders not confirming",
                "the hyperscalers turning up"),
            info_ratio("smh", "OPTICS", "SMH", "Optics vs semis (OPTICS/SMH)",
                       "whether optics is leading or trailing the wider AI hardware trade"),
        ],
        "topping": "OPTICS up while AAOI lags Corning and the hyperscalers roll over -- the ramp "
                   "priced past its funders.",
        "bottoming": "OPTICS still down, but AAOI starts beating Corning and the hyperscalers "
                     "turn up.",
    },
    "SMH": {
        "family": "growth",
        "bet": "AI accelerators and foundry, broadening to memory and equipment",
        "bench": "XLK",
        "checks": [
            rat("eqw", "XSD", "SMH", 1, "Equal-weight vs cap-weight semis (XSD/SMH)",
                "breadth fading -- equal-weight semis lagging",
                "the average semi holding up better"),
            conc("big3", ["NVDA", "TSM", "AVGO"], "NVDA + TSM + AVGO share of the move",
                 "AI compute and foundry vs the broadening"),
            split("mix", [("compute", ["NVDA", "AVGO", "AMD"]),
                          ("equipment", ["ASML", "AMAT", "LRCX", "KLAC"]),
                          ("memory", ["MU"]),
                          ("foundry", ["TSM"]),
                          ("the rest", None)],
                  "Compute vs equipment vs memory vs foundry",
                  "a broadening cycle shows up as equipment and memory joining compute"),
            rat("mu", "MU", "SMH", 1, "Micron vs semis (MU/SMH)",
                "memory not joining", "memory leading a falling group"),
            drv("payers", "HYPERSCALE", 1, "The payers (HYPERSCALE basket)",
                "the hyperscalers falling -- the capex payers not confirming",
                "the hyperscalers turning up"),
        ],
        "topping": "SMH up on NVDA, TSM and AVGO while XSD and Micron lag and the hyperscalers "
                   "stall -- AI compute alone, not broadening.",
        "bottoming": "SMH still down, but XSD and Micron start beating it and the hyperscalers "
                     "turn up -- breadth returning first.",
    },
    "DRAM": {
        "family": "growth",
        "bet": "the memory pricing upcycle",
        "caveat": "history from 2026-04: no evidence",
        "bench": "SMH",   # the semis industry, not the whole tech sector
        "checks": [
            drv("ewy", "EWY", 1, "Korea (hynix + Samsung, EWY)",
                "Korea falling -- the two largest memory makers not confirming",
                "Korea turning up"),
            rat("mu", "MU", "SMH", 1, "Micron vs semis (MU/SMH)",
                "memory lagging the semis", "memory leading the semis"),
            conc("conc", TOP3, "Top-3 share of the move",
                 "Samsung and SK hynix are about 36% of DRAM"),
        ],
        "topping": "DRAM up while Micron lags the semis and Korea rolls over -- the memory "
                   "pricing signal fading.",
        "bottoming": "DRAM still down, but Micron starts beating SMH and EWY turns up.",
    },
    "POWERSEMI": {
        "family": "theme",
        "bet": "AI data-center power delivery vs the auto/industrial cycle",
        "bench": "SMH",
        "checks": [
            split("legs", [("AI power", ["NVTS", "VICR", "MPWR", "AOSL"]),
                           ("auto / industrial", ["ON", "STM"])],
                  "AI power vs auto / industrial",
                  "two different end markets in one basket"),
            part(label="Members rising (6)",
                 up="fewer of the six power-semi names rising",
                 dn="most of the six power-semi names rising while the basket falls"),
            drv("smh", "SMH", 1, "Semis (SMH)",
                "the semis index falling under the basket", "the semis index turning up"),
            drv("driv", "DRIV", 1, "Autos / EV (the auto leg, DRIV)",
                "the auto leg falling", "the auto leg turning up"),
        ],
        "topping": "POWERSEMI up on the AI-power names while ON/STM and DRIV lag and SMH stalls "
                   "-- one leg of two.",
        "bottoming": "POWERSEMI still down, but the auto leg (DRIV) and SMH turn up and more "
                     "members rise.",
    },

    # ---------------- Communication services ----------------
    "XLC": {
        "family": "growth",
        "bet": "digital advertising (META + GOOGL) plus streaming, telecom as ballast",
        "bench": None,   # XLC/SPY is in the table
        "checks": [
            conc("ads", ["META", "GOOGL", "GOOG"], "META + Alphabet share of the move",
                 "the ad duopoly vs the rest of the sector"),
            split("mix", [("ads", ["META", "GOOGL", "GOOG"]),
                          ("media", ["NFLX", "DIS", "WBD"]),
                          ("telecom", ["T", "VZ", "TMUS", "CHTR"]),
                          ("the rest", None)],
                  "Ads vs media vs telecom",
                  "telecom is the ballast; ads and streaming are the growth"),
            rat("rel", "XLC", "SPY", 1, "Communication services vs S&P (XLC/SPY)",
                "the sector lagging the market", "the sector beating the market while falling"),
            part(),
        ],
        "topping": "XLC up on META and Alphabet while fewer members rise and it lags SPY -- the "
                   "ad duopoly alone.",
        "bottoming": "XLC still down, but more members rise and it starts beating SPY.",
    },
    "ESPO": {
        "family": "theme",
        "bet": "the game release cycle and engagement",
        "caveat": "release-event-driven",
        "bench": None,   # ESPO/XLC is in the table
        "checks": [
            conc("conc", TOP3, "Top-3 share of the move",
                 "a release cycle often lives in two or three names"),
            rat("rel", "ESPO", "XLC", 1, "Games vs communication services (ESPO/XLC)",
                "games lagging the sector", "games beating the sector"),
            part(),
        ],
        "topping": "ESPO up on its top three while it lags XLC -- a release-driven move "
                   "narrowing.",
        "bottoming": "ESPO still down, but it starts beating XLC and more members rise.",
    },
    "FDN": {
        "family": "growth",
        "bet": "internet platforms' ad and commerce growth",
        "bench": None,   # FDN/QQQ is in the table
        "checks": [
            conc("conc", TOP3, "Top-3 share of the move",
                 "AMZN and META vs the rest of the internet names"),
            rat("rel", "FDN", "QQQ", 1, "Internet vs Nasdaq (FDN/QQQ)",
                "internet lagging the Nasdaq", "internet beating the Nasdaq"),
            drv("rates", "^TNX", -1, "10y Treasury yield (^TNX)",
                "yields rising on platform multiples", "yields falling"),
        ],
        "topping": "FDN up on AMZN and META while it lags QQQ and yields rise.",
        "bottoming": "FDN still down, but it starts beating QQQ and yields ease.",
    },

    # ---------------- Utilities and real estate ----------------
    "XLU": {
        "family": "defensive",
        "bet": "two trades in one fund: bond proxy (falling yields) and AI power demand (the IPPs)",
        "bench": None,   # XLU/SPY is in the table
        "checks": [
            split("legs", [("IPPs", ["VST", "CEG", "NRG"]), ("regulated", None)],
                  "IPPs vs regulated utilities",
                  "the AI-power trade vs the bond proxy"),
            drv("rates", "^TNX", -1, "10y Treasury yield (^TNX)",
                "yields rising against the bond-proxy leg",
                "yields falling -- the bond-proxy leg returning"),
            rat("rel", "XLU", "SPY", 1, "Utilities vs S&P (XLU/SPY)",
                "utilities lagging the market", "utilities beating the market while falling"),
            part(),
        ],
        "topping": "XLU up on VST, CEG and NRG while yields rise and fewer regulated names climb "
                   "-- the AI-power trade alone, the bond proxy gone.",
        "bottoming": "XLU still down, but yields fall and more regulated names rise -- the "
                     "bond-proxy leg returning.",
    },
    "ICLN": {
        "family": "theme",
        "bet": "renewables economics -- financing rates plus policy credits",
        "bench": None,   # ICLN/XLU is in the table
        "checks": [
            drv("rates", "^TNX", -1, "10y Treasury yield (^TNX)",
                "the 10y yield climbing -- financing costs against the build-out",
                "yields falling -- financing relief"),
            rat("rel", "ICLN", "XLU", 1, "Renewables vs utilities (ICLN/XLU)",
                "renewables lagging the utilities", "renewables beating the utilities"),
            conc("conc", TOP3, "Top-3 share of the move",
                 "FSLR, BE and a few others vs the rest of the fund"),
            part(),
        ],
        "topping": "ICLN up while the 10y yield climbs and it lags XLU -- financing costs moving "
                   "against the build-out.",
        "bottoming": "ICLN still down, but yields fall and ICLN starts beating XLU.",
    },
    "NLR": {
        "family": "theme",
        "bet": "the nuclear renaissance -- life extensions, uprates, AI-power PPAs",
        "bench": "XLU",
        "checks": [
            drv("fuel", "URA", 1, "Uranium equities (the fuel side, URA)",
                "the fuel side falling", "the fuel side turning up"),
            drv("ceg", "CEG", 1, "Constellation Energy (CEG)",
                "the largest operator falling", "the largest operator turning up"),
            split("legs", [("operators", ["CEG", "VST", "PEG", "FORTUM.HE", "D", "DUK", "SO"]),
                           ("fuel", ["CCJ", "CCO.TO", "UEC", "NXE", "NXE.TO", "PDN.AX", "DML.TO",
                                     "DNN", "KAP.L", "KAP", "LEU", "UUUU", "EFR.TO"]),
                           ("SMR developers", ["OKLO", "SMR", "NNE", "XE"]),
                           ("the rest", None)],
                  "Operators vs fuel vs SMR developers",
                  "operators earn on PPAs today; SMR developers are the speculative layer"),
        ],
        "topping": "NLR up on the SMR developers while uranium and CEG roll over -- speculation "
                   "outrunning the operators and the fuel.",
        "bottoming": "NLR still down, but uranium equities and CEG turn up and the operators "
                     "lead.",
    },
    "XLRE": {
        "family": "defensive",
        "bet": "cap rates and property fundamentals; data centers and towers are the growth slice",
        "bench": "SPY",
        "checks": [
            drv("rates", "^TNX", -1, "10y Treasury yield (^TNX)",
                "the 10y yield climbing -- cap-rate math against the move",
                "yields falling -- cap-rate relief"),
            rat("credit", "HYG", "IEF", 1, "Credit vs Treasuries (HYG/IEF)",
                "credit softening under the REITs", "credit firming"),
            info_ratio("dtcr", "DTCR", "XLRE", "Data centers vs real estate (DTCR/XLRE)",
                       "whether the growth slice or the property book is moving the sector"),
            part(),
        ],
        "topping": "XLRE up while the 10y yield climbs and credit softens -- cap-rate math "
                   "against the move.",
        "bottoming": "XLRE still down, but yields fall, credit firms and more REITs rise.",
    },
    "DTCR": {
        "family": "theme",
        "bet": "data-center and tower demand from hyperscaler capex, financed at a cost",
        "bench": None,   # DTCR/XLRE is in the table
        "checks": [
            drv("payers", "HYPERSCALE", 1, "The payers (HYPERSCALE basket)",
                "the hyperscalers falling -- demand not confirming",
                "the hyperscalers turning up"),
            rat("rel", "DTCR", "XLRE", 1, "Digital infra vs real estate (DTCR/XLRE)",
                "digital infrastructure lagging the property sector",
                "digital infrastructure beating the property sector"),
            drv("rates", "^TNX", -1, "10y Treasury yield (^TNX)",
                "yields rising -- the financing cost climbing", "yields falling"),
            conc("big3", ["DLR", "AMT", "EQIX"], "DLR + AMT + EQIX share of the move",
                 "the three largest landlords vs the rest"),
        ],
        "topping": "DTCR up while the hyperscalers roll over and yields rise -- the demand and "
                   "the financing both turning against it.",
        "bottoming": "DTCR still down, but the hyperscalers turn up and DTCR starts beating "
                     "XLRE.",
    },

    # ---------------- Themes ----------------
    "ARTY": {
        "family": "theme",
        "bet": "AI adoption across the stack (semis-heavy on the 9/22 look-through)",
        "bench": None,   # ARTY/SMH is in the table
        "checks": [
            rat("broad", "ARTY", "SMH", 1, "AI basket vs semis (ARTY/SMH)",
                "adoption narrowing back to a semis trade",
                "the AI basket holding up better than semis"),
            drv("sw", "IGV", 1, "Software (the adoption leg, IGV)",
                "software falling -- the adoption leg not confirming",
                "software turning up"),
            drv("payers", "HYPERSCALE", 1, "The payers (HYPERSCALE basket)",
                "the hyperscalers falling", "the hyperscalers turning up"),
            conc("conc", TOP3, "Top-3 share of the move",
                 "whether a few chip names are the whole move"),
        ],
        "topping": "ARTY up but lagging SMH while software and the hyperscalers stall -- the "
                   "adoption story reverting to a semis trade.",
        "bottoming": "ARTY still down, but it starts beating SMH and software turns up -- "
                     "adoption broadening.",
    },
    "HYPERSCALE": {
        "family": "growth",
        "bet": "the capex payers keep earning enough to fund capex",
        "bench": None,   # HYPERSCALE/QQQ is in the table
        "checks": [
            part(label="Members rising (5)",
                 up="fewer of the five payers rising",
                 dn="most of the five payers rising while the basket falls"),
            rat("orcl", "ORCL", "MSFT", 1, "Most- vs least-levered spender (ORCL/MSFT)",
                "ORCL breaking down against MSFT -- financing stress at the most levered "
                "spender",
                "ORCL beating MSFT -- financing stress easing"),
            rat("ig", "LQD", "IEF", 1, "IG credit vs Treasuries (LQD/IEF)",
                "IG spreads widening -- they fund capex in IG now",
                "IG spreads tightening"),
            rat("rel", "HYPERSCALE", "QQQ", 1, "Hyperscalers vs Nasdaq (HYPERSCALE/QQQ)",
                "the payers lagging the Nasdaq", "the payers beating the Nasdaq while falling"),
        ],
        "topping": "Hyperscalers up while ORCL lags MSFT and IG spreads widen -- capex financing "
                   "strain showing before the price.",
        "bottoming": "Hyperscalers still down, but ORCL starts beating MSFT and IG credit firms.",
    },
    "NEOCLOUD": {
        "family": "theme",
        "bet": "GPU-cloud growth financed while debt and equity markets stay open",
        "bench": "SPY",
        "checks": [
            rat("credit", "HYG", "IEF", 1, "Credit (the financing window, HYG/IEF)",
                "the financing window narrowing -- high yield lagging",
                "the financing window reopening -- high yield firming"),
            part(label="Members rising (6)",
                 up="fewer of the six neoclouds rising",
                 dn="most of the six neoclouds rising while the basket falls"),
            drv("btc", "BTC-USD", 1, "Bitcoin (ex-miners still move with it, BTC-USD)",
                "bitcoin falling under the ex-miners", "bitcoin turning up"),
            drv("nvda", "NVDA", 1, "NVIDIA (the supplier, NVDA)",
                "NVIDIA falling -- the supply chain not confirming", "NVIDIA turning up"),
        ],
        "topping": "NEOCLOUD up while high yield lags Treasuries and bitcoin fades -- the "
                   "financing window narrowing under a levered build-out.",
        "bottoming": "NEOCLOUD still down, but high yield firms, bitcoin and NVDA turn up and "
                     "more members rise.",
    },
    "QUANTUM": {
        "family": "theme",
        "bet": "speculative capital for pre-revenue technology",
        "caveat": "speculative: phases change fast, evidence thin",
        "bench": "SPY",
        "checks": [
            part(label="Members rising (5)",
                 up="fewer of the five quantum names rising",
                 dn="most of the five quantum names rising while the basket falls"),
            rat("micro", "IWC", "IWM", 1, "Micro caps vs small caps (IWC/IWM)",
                "micro-cap appetite fading", "micro-cap appetite returning"),
            drv("btc", "BTC-USD", 1, "Bitcoin (speculative appetite, BTC-USD)",
                "bitcoin falling -- speculative appetite cooling", "bitcoin turning up"),
        ],
        "topping": "QUANTUM up while micro caps lag and bitcoin fades -- speculative appetite "
                   "cooling under the names.",
        "bottoming": "QUANTUM still down, but micro caps beat small caps and bitcoin turns up.",
    },
    "UFO": {
        "family": "theme",
        "bet": "the space economy -- launch, satellites, defense space",
        "bench": None,   # UFO/ITA is in the table
        "checks": [
            conc("conc", TOP3, "Top-3 share of the move",
                 "a few launch and satellite names vs the rest"),
            rat("rel", "UFO", "ITA", 1, "Space vs aerospace & defense (UFO/ITA)",
                "space lagging aerospace and defense", "space beating aerospace and defense"),
            part(),
        ],
        "topping": "UFO up on its top three while it lags ITA and fewer members rise.",
        "bottoming": "UFO still down, but it starts beating ITA and more members rise.",
    },
    "BOTZ": {
        "family": "theme",
        "bet": "robotics and automation plus AI",
        "bench": None,   # BOTZ/SMH is in the table
        "checks": [
            conc("nvda", ["NVDA"], "NVDA's share of the move",
                 "one AI chip name vs the automation makers"),
            rat("rel", "BOTZ", "SMH", 1, "Robotics vs semis (BOTZ/SMH)",
                "automation lagging the chip trade", "automation beating the chip trade"),
            drv("yen", "JPYUSD=X", 1, "Yen (Fanuc/Keyence translate, JPYUSD)",
                "the yen weakening -- the Japanese holdings translating lower",
                "the yen strengthening"),
            part(),
        ],
        "topping": "BOTZ up on NVDA while it lags SMH and the yen weakens -- the automation "
                   "names not joining.",
        "bottoming": "BOTZ still down, but the yen firms and more automation names rise.",
    },
}


# --------------------------------------------------------------------------
# resolution helpers
# --------------------------------------------------------------------------

BASKETS = {e["t"]: list(e["basket"]) for e in RG.REG_ETFS if e.get("basket")}


def primaries():
    """The primary rows in REG_ETFS order: rows with no grp, plus the FIRST-listed
    member of each grp (regime.sector_ladder's dedup rule)."""
    seen = set()
    out = []
    for e in RG.REG_ETFS:
        g = e.get("grp")
        if not g:
            out.append(e["t"])
        elif g not in seen:
            seen.add(g)
            out.append(e["t"])
    return out


def primary_of(t):
    """The primary a row's checks come from (itself for a primary). Twins resolve
    through REG_ETFS ``grp``: the first-listed member of the group."""
    first = {}
    for e in RG.REG_ETFS:
        g = e.get("grp")
        if g and g not in first:
            first[g] = e["t"]
    for e in RG.REG_ETFS:
        if e["t"] == t:
            g = e.get("grp")
            return first[g] if g else t
    return None


def _bench_ratio(t, bench):
    return {"id": "bench", "kind": "ratio", "inputs": [t, bench], "s": 1, "window": 63,
            "label": f"{t} vs {bench} ({t}/{bench})", "auto": True,
            "up_div": f"{t} lagging {bench}",
            "dn_div": f"{t} beating {bench} while falling"}


EXTENSION = {"id": "ext", "kind": "extension", "inputs": [], "s": 0, "window": 200,
             "label": "Extension vs its 200d mean (percentile of its own 3y range)"}


def checks_for(t):
    """The full check list for a row: its primary's table checks, the auto-added
    benchmark ratio (when the primary names one), then extension."""
    p = primary_of(t)
    th = THESES[p]
    out = [dict(c) for c in th["checks"]]
    if th.get("bench"):
        out.append(_bench_ratio(p, th["bench"]))
    out.append(dict(EXTENSION))
    return out


def price_inputs():
    """Every symbol a price-based check reads (baskets resolved to themselves --
    callers expand them), plus FX series."""
    syms = set()
    for p in primaries():
        for c in checks_for(p):
            if c["kind"] in ("driver", "ratio", "curve", "level"):
                syms.update(c["inputs"])
                if c.get("fx"):
                    syms.add(c["fx"])
    return syms
