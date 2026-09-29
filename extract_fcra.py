"""
extract_fcra.py

Lead Generation (FCRA / foreign-source donors): foreign entities that can fund
India under the Foreign Contribution Regulation Act. Sources:
  1. IATI Datastore  -- foreign donors funding India (precise; reuses IATI key).
  2. NGObase global foundations -- broad pool of foreign foundations (page-capped
     so it stays fast; the full worldwide list is ~17k / ~855 pages).
  3. Curated foreign govt aid agencies -- major bilateral/multilateral donors.

NOTE: FCRA is a compliance classification, not a separate donor segment, so
these foreign donors overlap with the Institutional Donors tab. That's expected.

Run:  python extract_fcra.py
Output: fcra_donors.xlsx
"""

import re
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup
import openpyxl

import extract_iati as eiat
import extract_foundation as ef

# NGObase global list is huge (~855 pages). Cap how many pages to pull.
GLOBAL_PAGE_CAP = 500
# Pages are fetched in parallel (they're independent), which is the real speed-up
# vs. the sequential 1-req/sec loop. 10 workers ≈ 10x faster.
GLOBAL_WORKERS = 10


def make_record(name, dtype=None, country=None, source=""):
    return {
        "name": (name or "").strip(),
        "type": (dtype or "").strip() or None,
        "country": (country or "").strip() or None,
        "source": source,
    }


# --- Curated foreign govt aid agencies (bilateral / multilateral) ---
FOREIGN_AID = [
    ("USAID", "Bilateral aid (USA)", "USA"),
    ("FCDO (UK Aid)", "Bilateral aid (UK)", "UK"),
    ("GIZ (Germany)", "Bilateral aid (Germany)", "Germany"),
    ("KfW Development Bank", "Bilateral aid (Germany)", "Germany"),
    ("JICA (Japan)", "Bilateral aid (Japan)", "Japan"),
    ("AFD (France)", "Bilateral aid (France)", "France"),
    ("Global Affairs Canada", "Bilateral aid (Canada)", "Canada"),
    ("Norad (Norway)", "Bilateral aid (Norway)", "Norway"),
    ("Sida (Sweden)", "Bilateral aid (Sweden)", "Sweden"),
    ("DFAT (Australia)", "Bilateral aid (Australia)", "Australia"),
    ("European Union (EU)", "Multilateral", "EU"),
    ("Bill & Melinda Gates Foundation", "Foreign foundation", "USA"),
    ("Ford Foundation", "Foreign foundation", "USA"),
    ("MacArthur Foundation", "Foreign foundation", "USA"),
]


def curated_foreign_aid():
    return [make_record(name, dtype=dtype, country=country, source="Curated (foreign aid)")
            for (name, dtype, country) in FOREIGN_AID]


def scrape_iati_foreign():
    """Foreign donors funding India, from IATI (reuses IATI_API_KEY in .env)."""
    records = []
    for r in eiat.scrape_iati():
        records.append(make_record(r["donor_name"], dtype="Foreign (IATI)", source="IATI"))
    return records


GLOBAL_PATH = "/agencies/t/ff"


def _fetch_global_page(page):
    """Fetch and parse one NGObase global page. Own session (thread-safe).
    Returns (page, list_of_ngobase_records) or (page, [])."""
    url = f"{ef.BASE}{GLOBAL_PATH}?page={page}"
    try:
        resp = requests.get(url, headers=ef.BROWSER_HEADERS, timeout=ef.REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException:
        return page, []
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("div.row.border.bg-white")
    recs = [ef.parse_row(r, "Foreign Foundation", "NGObase Global") for r in rows]
    return page, [r for r in recs if r]


def scrape_ngobase_global():
    """Broad pool of foreign foundations from NGObase global list, fetched in
    parallel (page-capped). Drops India-tagged rows (FCRA = foreign-source)."""
    print(f"[NGObase Global] fetching up to {GLOBAL_PAGE_CAP} pages with "
          f"{GLOBAL_WORKERS} workers...")
    records = []
    with ThreadPoolExecutor(max_workers=GLOBAL_WORKERS) as pool:
        for page, raw in pool.map(_fetch_global_page, range(1, GLOBAL_PAGE_CAP + 1)):
            for r in raw:
                if (r.get("country") or "").strip().lower() == "india":
                    continue
                records.append(make_record(r["foundation_name"], dtype="Foreign Foundation",
                                           country=r.get("country"), source="NGObase Global"))
    print(f"[NGObase Global] got {len(records)} foreign foundations")
    return records


def normalize_name(name):
    key = (name or "").lower().strip()
    key = re.sub(r"\(.*?\)", " ", key)
    key = re.sub(r"[.,&'\-/]", " ", key)
    return re.sub(r"\s+", " ", key).strip()


def dedupe(records):
    print("[dedupe] merging...")
    by_key = {}
    for rec in records:
        key = normalize_name(rec["name"])
        if not key:
            continue
        if key in by_key:
            existing = by_key[key]
            for f in ("type", "country"):
                if not existing[f] and rec[f]:
                    existing[f] = rec[f]
            srcs = set(existing["source"].split(", ")) | {rec["source"]}
            existing["source"] = ", ".join(sorted(s for s in srcs if s))
        else:
            by_key[key] = dict(rec)
    merged = list(by_key.values())
    print(f"[dedupe] {len(records)} raw -> {len(merged)} unique")
    return merged


def write_excel(records, filename="fcra_donors.xlsx"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "FCRA Foreign Donors"
    ws.append(["Name", "Type", "Country", "Source(s)"])
    for rec in sorted(records, key=lambda r: r["name"].lower()):
        ws.append([rec["name"], rec["type"] or "", rec["country"] or "", rec["source"]])
    wb.save(filename)
    print(f"[excel] wrote {len(records)} rows -> {filename}")


def main():
    all_records = []
    all_records += curated_foreign_aid()
    all_records += scrape_iati_foreign()
    all_records += scrape_ngobase_global()
    merged = dedupe(all_records)
    write_excel(merged)
    print("Done.")


if __name__ == "__main__":
    main()
