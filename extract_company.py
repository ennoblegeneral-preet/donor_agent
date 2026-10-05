
"""
extract_company_universe.py

Phase 1 of Lead Generation: extract a combined universe of listed (NSE + BSE)
and unlisted (SharesCart, UnlistedZone, Stockify) company names into one
deduplicated Excel file. No research/scoring here -- just the raw name list.

Run:  python extract_company_universe.py
Output: company_universe.xlsx  (in the same folder)
"""

import csv
import io
import re
import time

import requests
from bs4 import BeautifulSoup
import openpyxl

# ---------------------------------------------------------------------------
# Shared HTTP session with browser-like headers (NSE/BSE reject bare requests)
# ---------------------------------------------------------------------------
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

REQUEST_TIMEOUT = 30
POLITE_DELAY = 1.0  # seconds between page fetches -- be polite to the sites


def make_session(warm_up_url=None):
    """Return a requests.Session with browser headers. Optionally hit a
    warm-up URL first so the server hands us cookies (needed by NSE/BSE)."""
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    if warm_up_url:
        try:
            s.get(warm_up_url, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            print(f"[warn] warm-up request to {warm_up_url} failed: {e}")
    return s


# ---------------------------------------------------------------------------
# Record helper -- every source returns dicts of this exact shape
# ---------------------------------------------------------------------------
def make_record(company_name, isin=None, sector=None,
                listing_status="Unlisted", paidup_capital=None, status=None, source=""):
    name = (company_name or "").strip()
    return {
        "company_name": name,
        "isin": (isin or "").strip().upper() or None,
        "sector": (sector or "").strip() or None,
        "listing_status": listing_status,
        "paidup_capital": paidup_capital,   # in Rs (from MCA); None for NSE/BSE/unlisted
        "status": (status or "").strip() or None,   # Active / Strike Off / etc. (MCA)
        "source": source,
    }


# ---------------------------------------------------------------------------
# 1. LISTED -- NSE (official equity master CSV)
# ---------------------------------------------------------------------------
def fetch_nse():
    print("[NSE] fetching equity master list...")
    records = []
    url = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
    session = make_session(warm_up_url="https://www.nseindia.com")
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        for row in reader:
            name = row.get("NAME OF COMPANY") or row.get("NAME OF COMPANY ")
            isin = row.get("ISIN NUMBER") or row.get(" ISIN NUMBER")
            if name:
                records.append(make_record(name, isin=isin,
                                           listing_status="Listed", source="NSE"))
    except requests.RequestException as e:
        print(f"[NSE] ERROR: {e}")
    print(f"[NSE] got {len(records)} companies")
    return records


# ---------------------------------------------------------------------------
# 2. LISTED -- BSE (official scrip list via API)
# ---------------------------------------------------------------------------
def fetch_bse():
    print("[BSE] fetching scrip list...")
    records = []
    # BSE's list-of-scrips API returns JSON of all active equity scrips.
    url = ("https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
           "?Group=&Scripcode=&industry=&segment=Equity&status=Active")
    session = make_session(warm_up_url="https://www.bseindia.com")
    session.headers.update({"Referer": "https://www.bseindia.com/"})
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        for row in data:
            name = row.get("Issuer_Name") or row.get("Scrip_Name")
            isin = row.get("ISIN_NUMBER") or row.get("ISINNo")
            sector = row.get("Industry")
            if name:
                records.append(make_record(name, isin=isin, sector=sector,
                                           listing_status="Listed", source="BSE"))
    except (requests.RequestException, ValueError) as e:
        print(f"[BSE] ERROR: {e}")
    print(f"[BSE] got {len(records)} companies")
    return records


# ---------------------------------------------------------------------------
# Generic paginated HTML scraper for the unlisted-share sites
# ---------------------------------------------------------------------------
def scrape_paginated(label, url_template, row_selector, parse_row,
                     max_pages=50, warm_up_url=None):
    """url_template must contain '{page}'. parse_row(row_soup) -> record dict
    or None. Stops when a page yields no rows."""
    print(f"[{label}] scraping...")
    records = []
    session = make_session(warm_up_url=warm_up_url or url_template.split("?")[0])
    for page in range(1, max_pages + 1):
        url = url_template.format(page=page)
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"[{label}] page {page} ERROR: {e}")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select(row_selector)
        if not rows:
            if page == 1:
                print(f"[{label}] WARNING: 0 rows on page 1. The site is "
                      f"likely JavaScript-rendered -- use Playwright/Selenium "
                      f"for this source instead of requests.")
            break

        added = 0
        for r in rows:
            rec = parse_row(r)
            if rec and rec["company_name"]:
                records.append(rec)
                added += 1
        print(f"[{label}] page {page}: +{added}")
        if added == 0:
            break
        time.sleep(POLITE_DELAY)

    print(f"[{label}] got {len(records)} companies")
    return records


# ---------------------------------------------------------------------------
# 3. UNLISTED -- SharesCart
#    TODO: open the page in dev-tools and verify the selectors below.
# ---------------------------------------------------------------------------
def scrape_sharescart():
    def parse_row(tr):
        cells = tr.find_all("td")
        if len(cells) < 2:
            return None
        name = cells[0].get_text(strip=True)         # TODO: verify column order
        sector = cells[1].get_text(strip=True) if len(cells) > 1 else None
        return make_record(name, sector=sector,
                           listing_status="Unlisted", source="SharesCart")

    return scrape_paginated(
        label="SharesCart",
        url_template="https://www.sharescart.com/unlisted-shares/unlisted-shares-quotes.php?page={page}",
        row_selector="table tbody tr",              # TODO: verify selector
        parse_row=parse_row,
    )


# ---------------------------------------------------------------------------
# 4. UNLISTED -- UnlistedZone (server-rendered; ~270 companies, 24/page)
# ---------------------------------------------------------------------------
def scrape_unlistedzone():
    def parse_row(card):
        name_el = card.select_one(".nm")
        sector_el = card.select_one(".sec")
        name = name_el.get_text(strip=True) if name_el else ""
        # Trim the trailing " Unlisted Shares" the site appends to every name.
        name = re.sub(r"\s+Unlisted Shares$", "", name).strip()
        sector = sector_el.get_text(strip=True) if sector_el else None
        return make_record(name, sector=sector,
                           listing_status="Unlisted", source="UnlistedZone")

    return scrape_paginated(
        label="UnlistedZone",
        url_template="https://unlistedzone.com/shares?page={page}",
        row_selector="div.scard",
        parse_row=parse_row,
    )


# ---------------------------------------------------------------------------
# 5. UNLISTED -- Stockify (only source with ISIN)
#    TODO: verify selectors.
# ---------------------------------------------------------------------------
def scrape_stockify():
    def parse_row(card):
        name_el = card.select_one(".company-name")   # TODO: verify selector
        isin_el = card.select_one(".isin")           # TODO: verify selector
        sector_el = card.select_one(".sector")       # TODO: verify selector
        name = name_el.get_text(strip=True) if name_el else ""
        isin = isin_el.get_text(strip=True) if isin_el else None
        sector = sector_el.get_text(strip=True) if sector_el else None
        return make_record(name, isin=isin, sector=sector,
                           listing_status="Unlisted", source="Stockify")

    return scrape_paginated(
        label="Stockify",
        url_template="https://stockify.net.in/buy-unlisted-shares/?page={page}",
        row_selector=".company-card",                # TODO: verify selector
        parse_row=parse_row,
    )


# ---------------------------------------------------------------------------
# Dedupe -- ISIN first, else normalized company name
# ---------------------------------------------------------------------------
_SUFFIXES = [" limited", " ltd", " private", " pvt", " (india)", " india"]

def normalize_name(name):
    key = (name or "").lower().strip()
    key = re.sub(r"[.,&']", " ", key)
    for _ in range(3):  # strip repeated suffixes like "pvt ltd"
        for suf in _SUFFIXES:
            if key.endswith(suf):
                key = key[: -len(suf)].strip()
    return re.sub(r"\s+", " ", key).strip()


def dedupe(records):
    """Merge duplicates by ISIN OR by normalized name. Matching on both matters
    because MCA companies carry a CIN (no ISIN) while NSE/BSE carry an ISIN, so
    the same company must still collapse when it appears in both."""
    print("[dedupe] merging...")
    by_isin = {}   # isin -> canonical record
    by_name = {}   # normalized name -> canonical record
    canon = []     # unique records, appended once each
    for rec in records:
        name_key = normalize_name(rec["company_name"])
        if not name_key and not rec["isin"]:
            continue
        # Find an existing record this one matches (ISIN first, then name).
        existing = None
        if rec["isin"] and rec["isin"] in by_isin:
            existing = by_isin[rec["isin"]]
        elif name_key and name_key in by_name:
            existing = by_name[name_key]

        if existing is not None:
            for f in ("isin", "sector", "paidup_capital", "status"):
                if not existing.get(f) and rec.get(f):
                    existing[f] = rec[f]
            srcs = set(existing["source"].split(", ")) | {rec["source"]}
            existing["source"] = ", ".join(sorted(s for s in srcs if s))
            if rec["listing_status"] == "Listed":
                existing["listing_status"] = "Listed"
            target = existing
        else:
            target = dict(rec)
            canon.append(target)

        # Index the canonical record under both keys so later dups find it.
        if target["isin"]:
            by_isin[target["isin"]] = target
        tname = normalize_name(target["company_name"])
        if tname:
            by_name[tname] = target
    print(f"[dedupe] {len(records)} raw -> {len(canon)} unique")
    return canon


# ---------------------------------------------------------------------------
# Trim -- drop the smallest unlisted companies (lowest MCA paid-up capital)
# ---------------------------------------------------------------------------
UNLISTED_DROP_COUNT = 3000

def drop_lowest_unlisted(records, n=UNLISTED_DROP_COUNT):
    """Remove the `n` unlisted companies with the lowest paid-up capital.
    Unlisted companies without a capital figure (non-MCA sources) are kept."""
    ranked = sorted((r for r in records
                     if r["listing_status"] == "Unlisted" and r.get("paidup_capital") is not None),
                    key=lambda r: r["paidup_capital"])
    drop = {id(r) for r in ranked[:n]}
    kept = [r for r in records if id(r) not in drop]
    print(f"[trim] dropped {len(drop)} lowest paid-up unlisted -> {len(kept)} remain")
    return kept


# ---------------------------------------------------------------------------
# Write Excel
# ---------------------------------------------------------------------------
UNIVERSE_HEADERS = ["Company Name", "ISIN", "Sector", "Listing Status",
                    "Paid-up Capital (Rs)", "Source(s)"]


def universe_rows(records):
    """Sorted universe rows in UNIVERSE_HEADERS order (shared by Excel + MongoDB)."""
    return [[rec["company_name"], rec["isin"] or "",
             rec["sector"] or "", rec["listing_status"],
             rec.get("paidup_capital") or "", rec["source"]]
            for rec in sorted(records, key=lambda r: r["company_name"].lower())]


def write_excel(records, filename="company_universe.xlsx"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Company Universe"
    ws.append(UNIVERSE_HEADERS)
    for row in universe_rows(records):
        ws.append(row)
    wb.save(filename)
    print(f"[excel] wrote {len(records)} rows -> {filename}")


def save_universe_rows(headers, rows):
    """Save the universe to MongoDB so every server shows the same list."""
    import db
    db.save_lead_set(db.LEAD_UNIVERSE_KEY, [{"r": list(r)} for r in rows],
                     headers=list(headers))
    print(f"[mongo] saved {len(rows)} universe rows")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    all_records = []
    all_records += fetch_nse()
    all_records += fetch_bse()
    all_records += scrape_sharescart()
    all_records += scrape_unlistedzone()
    all_records += scrape_stockify()

    merged = dedupe(all_records)
    write_excel(merged)
    print("Done.")


if __name__ == "__main__":
    main()
