"""Dump the official ISM and University of Michigan release schedules as text.

Prints the lines that carry 2026/2027 dates, plus the links that look like a
schedule, so the dates can be transcribed into jr-dash macro-calendar.json.
Public pages only; nothing is written back to the repo.
"""
import io, re, sys, urllib.request
from html.parser import HTMLParser

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}
PAGES = [
    "https://www.ismworld.org/supply-management-news-and-reports/reports/rob-report-calendar/",
    "https://www.sca.isr.umich.edu/",
    "https://www.sca.isr.umich.edu/release-schedule.html",
    "https://data.sca.isr.umich.edu/",
]
MON = r"(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"

class Text(HTMLParser):
    def __init__(s): super().__init__(); s.out = []; s.links = []; s.skip = 0
    def handle_starttag(s, t, a):
        if t in ("script", "style"): s.skip += 1
        if t == "a":
            h = dict(a).get("href") or ""
            s.links.append(h)
        if t in ("tr", "p", "li", "br", "div", "h1", "h2", "h3", "h4", "td", "th"): s.out.append("\n" if t != "td" and t != "th" else " | ")
    def handle_endtag(s, t):
        if t in ("script", "style"): s.skip -= 1
    def handle_data(s, d):
        if not s.skip: s.out.append(d)

def get(u):
    r = urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=30)
    return r.read(), r.headers.get("content-type", "")

def dump(u, depth=0):
    print(f"\n######## {u}")
    try:
        b, ct = get(u)
    except Exception as e:
        print("  FETCH FAILED:", type(e).__name__, str(e)[:150]); return
    if "pdf" in ct or u.lower().endswith(".pdf"):
        from pypdf import PdfReader
        txt = "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(b)).pages)
        links = []
    else:
        p = Text(); p.feed(b.decode("utf-8", "replace")); txt = "".join(p.out); links = p.links
    lines = [re.sub(r"\s+", " ", l).strip() for l in txt.splitlines()]
    hits = [l for l in lines if l and (re.search(r"202[67]", l) or re.search(MON + r"\.? \d{1,2}\b", l))]
    print(f"  {len(lines)} lines, {len(hits)} with dates")
    for l in hits[:200]: print("  ", l[:220])
    if depth == 0:
        sch = sorted({urllib.request.urljoin(u, h) for h in links if re.search(r"schedul|calendar|release", h, re.I)})
        print("  schedule-like links:", sch[:30])
        for s in sch[:6]:
            if s.rstrip("/") != u.rstrip("/"): dump(s, 1)

for u in PAGES: dump(u)
