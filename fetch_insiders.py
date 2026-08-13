#!/usr/bin/env python3
"""Weekly insider-transaction fetch for the jr-dash Book tab.

Source: SEC EDGAR only — primary, free, no API key.
  1. ticker -> CIK via company_tickers.json (one fetch per run)
  2. per CIK: data.sec.gov submissions JSON (one fetch per name)
  3. Form 4 / 4/A raw XML, fetched ONLY for accessions not already stored
     (incremental: the committed data/insiders.json is also the state)

Signal filter: open-market purchases (code P) above the axis, open-market
sales (code S) below. Grants (A), option exercises (M), tax withholding (F),
and gifts (G) are deliberately excluded — they are compensation mechanics,
not discretionary buy/sell decisions. 10b5-1 plan sales ARE included but
flagged per-transaction, so the dashboard can report the plan share.

Politeness: SEC asks for a descriptive UA and <=10 req/s. We sleep 0.12s
between requests and retry once on 429/503.

Output: data/insiders.json — raw transactions (15-month retention) plus
12 pre-aggregated monthly buckets per ticker. Served to the dashboard
through the same Access-walled /data proxy as latest.json.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "insiders.json")
OUT_COMPACT = os.path.join(HERE, "data", "insiders_12m.json")
TICKERS_FILE = os.path.join(HERE, "insider_tickers.txt")

UA = {"User-Agent": "jr-dash insider fetch jakeradencom@gmail.com"}
SLEEP = 0.12
RETENTION_MONTHS = 15   # raw transaction retention; export window is 12
WINDOW_MONTHS = 12

# Names with no Section 16 regime by construction — never fetched.
SKIP = {
    "BTC-USD": "crypto",
    "EWY": "etf", "RING": "etf", "GDXJ": "etf", "ICOP": "etf",
    "QQQ": "etf", "IGV": "etf", "MARS": "etf",
}

BUY_CODES = {"P"}
SELL_CODES = {"S"}


def get(url, retry=1):
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code in (429, 503) and retry:
            time.sleep(2.0)
            return get(url, retry - 1)
        raise
    finally:
        time.sleep(SLEEP)


def month_floor(d: date, months_back: int) -> str:
    """YYYY-MM string months_back months before d's month."""
    y, m = d.year, d.month - months_back
    while m <= 0:
        y, m = y - 1, m + 12
    return f"{y:04d}-{m:02d}"


def load_tickers():
    out = []
    with open(TICKERS_FILE, encoding="utf-8") as fh:
        for line in fh:
            t = line.split("#")[0].strip().upper()
            if t:
                out.append(t)
    return out


def cik_map():
    data = json.loads(get("https://www.sec.gov/files/company_tickers.json"))
    return {v["ticker"].upper(): f"{v['cik_str']:010d}" for v in data.values()}


def list_forms(cik, since_iso):
    """{'f4': [...], 'f144': [...]} of (accession, primaryDocument, filingDate,
    form) since since_iso.

    The 'recent' block covers the last 1000 filings; if that window doesn't
    reach back to since_iso, pull the older index pages it references.

    Form 144s matter because FPIs (e.g. SHOP) are exempt from Section 16 — their
    insiders never file Form 4s, but affiliate sales still require a Form 144
    notice, which lands in the ISSUER's stream. That is the only EDGAR-visible
    insider-sale signal for such names.
    """
    sub = json.loads(get(f"https://data.sec.gov/submissions/CIK{cik}.json"))
    blocks = [sub["filings"]["recent"]]
    recent = sub["filings"]["recent"]
    if recent["filingDate"] and min(recent["filingDate"]) > since_iso:
        for extra in sub["filings"].get("files", []):
            if extra.get("filingTo", "") >= since_iso:
                blocks.append(json.loads(get(
                    "https://data.sec.gov/submissions/" + extra["name"])))
    out = {"f4": [], "f144": []}
    for b in blocks:
        for form, fdate, acc, doc in zip(
                b["form"], b["filingDate"], b["accessionNumber"],
                b["primaryDocument"]):
            if fdate < since_iso:
                continue
            if form in ("4", "4/A"):
                out["f4"].append((acc, doc, fdate, form))
            elif form in ("144", "144/A"):
                out["f144"].append((acc, doc, fdate, form))
    return out


def _local(tag):
    """Strip XML namespace from a tag name."""
    return tag.rsplit("}", 1)[-1]


def parse_form144(cik, acc, doc):
    """Return sell-notice txns from one Form 144 XML (namespaced 'own:' schema).

    A 144 is a NOTICE of proposed sale, not an execution report — value is the
    aggregate market value the seller declared. Older paper-style 144s without
    XML are skipped (returned as empty).
    """
    a = acc.replace("-", "")
    base = doc.split("/")[-1]
    if not base.endswith(".xml"):
        # paper/HTML 144 — no structured data; treat as empty so it is never re-fetched
        return []
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{a}/{base}"
    t = ET.fromstring(get(url))
    vals = {}
    for el in t.iter():
        vals.setdefault(_local(el.tag), (el.text or "").strip())
    # A 144 can sit in a company's stream because the company is the FILER
    # (e.g. Novo Nordisk A/S noticing a sale of ANOTHER issuer's shares).
    # Only keep notices where the securities sold are OUR issuer's.
    issuer = vals.get("issuerCik", "")
    if not issuer or int(issuer) != int(cik):
        return []
    seller = vals.get("nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold", "?")
    rel = vals.get("relationshipToIssuer", "")
    units = float(vals.get("noOfUnitsSold", 0) or 0)
    value = float(vals.get("aggregateMarketValue", 0) or 0)
    d = vals.get("approxSaleDate", "")  # MM/DD/YYYY
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", d)
    iso = f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else ""
    if not (units and iso):
        return []
    px = round(value / units, 2) if units else 0
    return [{"d": iso, "o": seller, "r": rel, "c": "S144",
             "sh": round(units), "px": px, "v": round(value),
             "plan": False, "acc": acc}]


def parse_form4(cik, acc, doc):
    """Return list of P/S transactions from one Form 4 XML."""
    a = acc.replace("-", "")
    # primaryDocument may carry an xsl-rendered path; the raw XML is the basename
    base = doc.split("/")[-1]
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{a}/{base}"
    t = ET.fromstring(get(url))
    owner = t.findtext(".//reportingOwnerId/rptOwnerName") or "?"
    title = (t.findtext(".//reportingOwnerRelationship/officerTitle") or "").strip()
    if not title:
        rel = t.find(".//reportingOwnerRelationship")
        if rel is not None:
            if (rel.findtext("isDirector") or "") in ("1", "true"):
                title = "Director"
            elif (rel.findtext("isTenPercentOwner") or "") in ("1", "true"):
                title = "10% owner"
    plan_flag = (t.findtext(".//aff10b5One") or "") in ("1", "true")
    foot = " ".join((f.text or "") for f in t.findall(".//footnote"))
    plan = plan_flag or ("10b5-1" in foot)
    txns = []
    for tr in t.findall(".//nonDerivativeTransaction"):
        code = tr.findtext(".//transactionCoding/transactionCode")
        if code not in BUY_CODES | SELL_CODES:
            continue
        sh = float(tr.findtext(".//transactionShares/value") or 0)
        px = float(tr.findtext(".//transactionPricePerShare/value") or 0)
        d = tr.findtext(".//transactionDate/value") or ""
        txns.append({
            "d": d, "o": owner, "r": title, "c": code,
            "sh": round(sh), "px": round(px, 2), "v": round(sh * px),
            "plan": plan, "acc": acc,
        })
    return txns


def aggregate(txns, months):
    """12 monthly buckets {m, b, s, bn, sn, sp} (sp = plan share of sells $)."""
    idx = {m: {"m": m, "b": 0, "s": 0, "bn": 0, "sn": 0, "sp": 0} for m in months}
    for x in txns:
        m = x["d"][:7]
        if m not in idx:
            continue
        row = idx[m]
        if x["c"] in BUY_CODES:
            row["b"] += x["v"]; row["bn"] += 1
        else:  # "S" or "S144" — anything stored that isn't a buy is a sell
            row["s"] += x["v"]; row["sn"] += 1
            if x["plan"]:
                row["sp"] += x["v"]
    return [idx[m] for m in months]


def main():
    today = date.today()
    since_fetch = month_floor(today, RETENTION_MONTHS) + "-01"
    window_start = month_floor(today, WINDOW_MONTHS - 1)  # 12 buckets incl. current
    months = []
    y, m = int(window_start[:4]), int(window_start[5:7])
    for _ in range(WINDOW_MONTHS):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1

    prev = {}
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as fh:
            prev = json.load(fh).get("tickers", {})

    tickers = load_tickers()
    ciks = cik_map()
    out = {}
    for tk in tickers:
        if tk in SKIP:
            out[tk] = {"status": SKIP[tk]}
            continue
        cik = ciks.get(tk)
        if not cik:
            out[tk] = {"status": "no-cik"}
            continue
        state = prev.get(tk, {})
        seen = {x["acc"] for x in state.get("txns", [])}
        seen |= set(state.get("empty_accs", []))
        try:
            forms = list_forms(cik, since_fetch)
        except Exception as e:
            print(f"{tk}: submissions fetch failed ({e}); carrying prior state",
                  file=sys.stderr)
            out[tk] = state or {"status": "error"}
            continue
        filings = forms["f4"]
        txns = [x for x in state.get("txns", []) if x["d"][:7] >= months[0] or
                x["d"] >= since_fetch]
        empty = set(state.get("empty_accs", []))
        new = 0
        for acc, doc, fdate, form in filings:
            if acc in seen:
                continue
            try:
                got = parse_form4(cik, acc, doc)
            except Exception as e:
                print(f"{tk} {acc}: parse failed ({e})", file=sys.stderr)
                continue
            if got:
                txns.extend(got); new += len(got)
            else:
                empty.add(acc)   # remember P/S-free filings so we never re-fetch
        # 4/A amendments re-report transactions -> dedupe on economics, keep first
        dd, keyset = [], set()
        for x in sorted(txns, key=lambda x: (x["d"], x["acc"])):
            k = (x["o"], x["d"], x["c"], x["sh"], x["px"])
            if k in keyset:
                continue
            keyset.add(k); dd.append(x)
        # accessions whose txns all deduped away contribute nothing forever —
        # mark them empty so amendments aren't refetched every run
        empty |= {x["acc"] for x in txns} - {x["acc"] for x in dd}
        # Mode decision. Form-4 P/S coverage wins. With NO Form-4 P/S, fall back
        # to Form 144 sale notices — the only EDGAR-visible insider-sale signal
        # for FPIs like SHOP, whose insiders are exempt from Section 16 (their
        # buys are not observable on EDGAR at all). 144s are NOT read for names
        # with Form-4 coverage: a domestic insider's 144 precedes the same
        # sale's Form 4, so folding both in would double-count.
        t144, e144, new144 = [], set(state.get("empty_accs144", [])), 0
        if not dd and forms["f144"]:
            t144 = [x for x in state.get("txns144", [])
                    if x["d"][:7] >= months[0] or x["d"] >= since_fetch]
            seen144 = {x["acc"] for x in t144} | e144
            for acc, doc, fdate, form in forms["f144"]:
                if acc in seen144:
                    continue
                try:
                    got = parse_form144(cik, acc, doc)
                except Exception as e:
                    print(f"{tk} {acc}: 144 parse failed ({e})", file=sys.stderr)
                    continue
                if got:
                    t144.extend(got); new144 += len(got)
                else:
                    e144.add(acc)
            # 144/A amendments re-notice the same sale -> dedupe, keep first
            ddd, kset = [], set()
            for x in sorted(t144, key=lambda x: (x["d"], x["acc"])):
                k = (x["o"], x["d"], x["sh"], x["v"])
                if k in kset:
                    continue
                kset.add(k); ddd.append(x)
            t144 = ddd

        if dd:
            status, use = "ok", dd
        elif [x for x in t144 if x["d"][:7] >= months[0]]:
            status, use = "144", t144
        elif filings:
            status, use = "ok", dd      # Form-4 coverage exists, zero P/S — real quiet
        else:
            status, use = "no-section16", dd
        out[tk] = {
            "status": status, "cik": cik,
            "txns": dd, "empty_accs": sorted(empty),
            "txns144": t144, "empty_accs144": sorted(e144),
            "months": aggregate(use, months),
            "tot_b": sum(x["v"] for x in use if x["c"] in BUY_CODES and x["d"][:7] >= months[0]),
            "tot_s": sum(x["v"] for x in use if x["c"] not in BUY_CODES and x["d"][:7] >= months[0]),
        }
        print(f"{tk}: {len(filings)} f4 / {len(forms['f144'])} f144 filings, "
              f"+{new} new txns, +{new144} new 144s, status={out[tk]['status']}")

    doc = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_months": WINDOW_MONTHS,
        "months": months,
        "codes": {"buy": sorted(BUY_CODES), "sell": sorted(SELL_CODES),
                  "note": "open-market P/S only; A/M/F/G excluded; plan = 10b5-1"},
        "tickers": out,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, separators=(",", ":"))
    print(f"wrote {OUT} ({os.path.getsize(OUT):,} bytes)")

    # Compact export for the dashboard: aggregates only, no per-insider detail.
    # This is the ONLY file the /data proxy whitelists — the full file (raw
    # transactions, owner names) stays repo-side as fetch state.
    compact = {
        "generated_at": doc["generated_at"],
        "months": months,
        "note": doc["codes"]["note"],
        "tickers": {
            tk: ({"status": v["status"]} if v.get("status") not in ("ok", "144") else {
                "status": v["status"], "tot_b": v["tot_b"], "tot_s": v["tot_s"],
                # per month: [buy$, sell$, buyN, sellN, plan-sell$]
                "m": [[r["b"], r["s"], r["bn"], r["sn"], r["sp"]]
                      for r in v["months"]],
            })
            for tk, v in out.items()
        },
    }
    with open(OUT_COMPACT, "w", encoding="utf-8") as fh:
        json.dump(compact, fh, separators=(",", ":"))
    print(f"wrote {OUT_COMPACT} ({os.path.getsize(OUT_COMPACT):,} bytes)")


if __name__ == "__main__":
    main()
