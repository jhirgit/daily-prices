"""Pass 3: render the ISM release calendar in headless Chromium (the plain fetch
gets a 922-byte script shell), and read the UMich fetchdoc documents for a
full-year release schedule. Public pages only; writes nothing back.
"""
import io, re, urllib.request
from pypdf import PdfReader
from playwright.sync_api import sync_playwright

MON = r"(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
ISM = "https://www.ismworld.org/supply-management-news-and-reports/reports/rob-report-calendar/"

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36")
    try:
        pg.goto(ISM, wait_until="networkidle", timeout=60000)
        pg.wait_for_timeout(4000)
        t = pg.inner_text("body")
        print(f"######## {ISM}\n  title: {pg.title()!r}, {len(t)} chars")
        for l in [re.sub(r'\s+', ' ', x).strip() for x in t.splitlines()]:
            if l and (re.search(r"202[67]", l) or re.search(MON + r"\.? \d{1,2}\b", l) or re.search(r"Services|Manufacturing", l)):
                print("   ", l[:200])
    except Exception as e:
        print("ISM render failed:", type(e).__name__, str(e)[:200])
    b.close()

ids = [80387, 81063, 81125, 81444, 81504, 81508, 81618, 81740, 81744, 81854, 81976, 81977]
for i in ids:
    u = f"https://data.sca.isr.umich.edu/fetchdoc.php?docid={i}"
    try:
        r = urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=30)
        data, ct = r.read(), r.headers.get("content-type", "")
        txt = "\n".join(pg.extract_text() or "" for pg in PdfReader(io.BytesIO(data)).pages) if data[:4] == b"%PDF" else data.decode("utf-8", "replace")
    except Exception as e:
        print(f"\n## {i}: FAILED {type(e).__name__}"); continue
    lines = [re.sub(r"\s+", " ", l).strip() for l in txt.splitlines() if l.strip()]
    print(f"\n## docid {i} ({ct}, {len(lines)} lines): {lines[0][:120] if lines else ''}")
    sched = any(re.search(r"release (date|schedule)|schedule", l, re.I) for l in lines)
    for l in lines:
        if re.search(r"release|schedule", l, re.I) and (re.search(r"202[67]", l) or re.search(MON + r"\.? \d{1,2}", l)):
            print("   ", l[:200])
    if sched:
        for l in lines[:80]:
            if re.search(MON + r"\.? \d{1,2}", l): print("    *", l[:200])
