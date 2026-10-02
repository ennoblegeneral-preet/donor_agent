"""
extract_foundation.py

Lead Generation (Family Foundations): scrape family/philanthropic foundation
names from NGObase (server-rendered, so plain requests works) into an Excel.

Run:  python extract_foundation.py
Output: foundations.xlsx  (same folder)

NOTE on volume: NGObase's INDIA-only family-foundations list is small (~7).
The global family-foundations list is huge (~17,000 across ~855 pages) but not
India-specific. TARGETS below controls exactly what gets scraped -- start with
India family foundations; add more paths (corporate foundations, etc.) as needed.
"""

import re
import time

import requests
from bs4 import BeautifulSoup
import openpyxl

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
REQUEST_TIMEOUT = 30
POLITE_DELAY = 1.0
BASE = "https://ngobase.org"

# Which NGObase listings to scrape. (path, label, foundation_type)
# Order matters: more specific types (family, corporate) come BEFORE the broad
# all-grantmakers list, so a duplicate keeps its specific type (dedupe keeps the
# first-seen record's type). India-focused; global list stays off.
# - /agencies/ct/IN/ff = India family foundations  (~7, exact)
# - /agencies/ct/IN/cf = India corporate foundations (~49)
# - /agencies/c/IN     = ALL India grantmakers (~144: families + corporates + others)
# - /agencies/t/ff     = ALL family foundations worldwide (~17k; NOT India-only) -- OFF
TARGETS = [
    ("/agencies/ct/IN/ff", "NGObase India Family Foundations", "Family Foundation"),
    ("/agencies/ct/IN/cf", "NGObase India Corporate Foundations", "Corporate Foundation"),
    ("/agencies/c/IN", "NGObase India Grantmakers (all)", "Grantmaker"),
    # ("/agencies/t/ff", "NGObase Global Family Foundations", "Family Foundation"),
]
MAX_PAGES = 900  # safety cap; loop stops earlier when a page has no rows


def make_session():
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    return s


def make_record(name, website=None, country=None, ftype=None, source=""):
    return {
        "foundation_name": (name or "").strip(),
        "website": (website or "").strip() or None,
        "country": (country or "").strip() or None,
        "type": (ftype or "").strip() or None,
        "source": source,
    }


def parse_row(row, ftype, source):
    a = row.select_one("a.link-dark")
    name = a.get_text(strip=True) if a else ""
    if not name:
        return None
    # Country lives in the first col-md-2 cell of the row (e.g. "India",
    # "Canada"). Read it rather than assuming India, so the global list is
    # labelled correctly.
    country = ""
    cols = row.select("div.col-md-2")
    if cols:
        txt = cols[0].get_text(strip=True)
        if txt and txt.lower() != "details":
            country = txt
    website = ""
    for link in row.select("a[href]"):
        href = link.get("href", "")
        if href.startswith("http") and "ngobase.org" not in href:
            website = href
            break
    return make_record(name, website=website,
                        country=country, ftype=ftype, source=source)


def scrape_ngobase(path, label, ftype):
    print(f"[{label}] scraping...")
    records = []
    session = make_session()
    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE}{path}?page={page}"
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"[{label}] page {page} ERROR: {e}")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("div.row.border.bg-white")
        if not rows:
            break

        added = 0
        for row in rows:
            rec = parse_row(row, ftype, label)
            if rec:
                records.append(rec)
                added += 1
        print(f"[{label}] page {page}: +{added}")
        if added == 0:
            break
        time.sleep(POLITE_DELAY)

    print(f"[{label}] got {len(records)} foundations")
    return records


CSRBOX_URL = "https://www.csrbox.org/csr-foundation/list-foundation"


def scrape_csrbox():
    """CSRBox foundation directory -- server-rendered, single page (~96 entries).
    These lean corporate/CSR foundations, not pure family, but many are
    promoter/family-linked. Name comes from each foundation-detail link."""
    print("[CSRBox] scraping...")
    records = []
    session = make_session()
    try:
        resp = session.get(CSRBOX_URL, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[CSRBox] ERROR: {e}")
        return records

    soup = BeautifulSoup(resp.text, "html.parser")
    seen = set()
    for a in soup.select('a[href*="foundation-detail-"]'):
        href = a.get("href", "")
        if href in seen:
            continue
        seen.add(href)
        name = a.get_text(strip=True)
        if not name:
            m = re.search(r"foundation-detail-(.+?)_\d+", href)
            name = m.group(1).replace("-", " ").strip() if m else ""
        if not name:
            continue
        records.append(make_record(name, country="India",
                                   ftype="Corporate/CSR Foundation", source="CSRBox"))
    print(f"[CSRBox] got {len(records)} foundations")
    return records


def normalize_name(name):
    key = (name or "").lower().strip()
    key = re.sub(r"[.,&'()]", " ", key)
    return re.sub(r"\s+", " ", key).strip()


def dedupe(records):
    print("[dedupe] merging...")
    by_key = {}
    for rec in records:
        key = normalize_name(rec["foundation_name"])
        if not key:
            continue
        if key in by_key:
            existing = by_key[key]
            for f in ("website", "country", "type"):
                if not existing[f] and rec[f]:
                    existing[f] = rec[f]
            srcs = set(existing["source"].split(", ")) | {rec["source"]}
            existing["source"] = ", ".join(sorted(s for s in srcs if s))
        else:
            by_key[key] = dict(rec)
    merged = list(by_key.values())
    print(f"[dedupe] {len(records)} raw -> {len(merged)} unique")
    return merged


def write_excel(records, filename="foundations.xlsx"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Foundations"
    ws.append(["Foundation Name", "Website", "Country", "Type", "Source(s)"])
    for rec in sorted(records, key=lambda r: r["foundation_name"].lower()):
        ws.append([rec["foundation_name"], rec["website"] or "",
                   rec["country"] or "", rec["type"] or "", rec["source"]])
    wb.save(filename)
    print(f"[excel] wrote {len(records)} rows -> {filename}")


def main():
    all_records = []
    for path, label, ftype in TARGETS:
        all_records += scrape_ngobase(path, label, ftype)
    all_records += scrape_csrbox()
    merged = dedupe(all_records)
    write_excel(merged)
    print("Done.")


if __name__ == "__main__":
    main()
