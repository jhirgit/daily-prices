"""Pass 2: show what the ISM calendar page actually returns, and list every link
on the UMich pages, so a full-year release schedule can be found and transcribed
into jr-dash macro-calendar.json. Public pages only; writes nothing back.
"""
import re, urllib.request

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
      "Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-US,en;q=0.9"}

def get(u):
    try:
        r = urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=30)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, type(e).__name__ + " " + str(e)[:150]

def text(h):
    h = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", h)
    h = re.sub(r"(?i)<(br|/p|/tr|/li|/div|/h\d)[^>]*>", "\n", h)
    h = re.sub(r"(?i)</t[dh]>", " | ", h)
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", l)).strip() for l in h.splitlines()]

MON = r"(January|February|March|April|May|June|July|August|September|October|November|December)"
for u in ["https://www.ismworld.org/supply-management-news-and-reports/reports/rob-report-calendar/",
          "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-report-on-business/",
          "https://www.prnewswire.com/news/institute-for-supply-management/"]:
    st, h = get(u)
    print(f"\n######## {u}\n  status {st}, {len(h)} bytes; title: {re.search(r'(?is)<title>(.*?)</title>', h).group(1).strip()[:120] if re.search(r'(?is)<title>', h) else None}")
    lines = [l for l in text(h) if l]
    hits = [l for l in lines if re.search(MON + r" \d{1,2}", l) and re.search(r"202[67]", l)]
    for l in (hits or lines[:25])[:80]: print("   ", l[:220])
    cal = sorted(set(re.findall(r'href="([^"]*(?:calendar|schedule|\.pdf)[^"]*)"', h, re.I)))
    print("  calendar/pdf links:", cal[:25])

for u in ["https://www.sca.isr.umich.edu/", "https://data.sca.isr.umich.edu/"]:
    st, h = get(u)
    print(f"\n######## {u}  status {st}")
    links = sorted(set(re.findall(r'href="([^"#]+)"', h)))
    print("  links:", links[:120])
