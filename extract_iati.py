"""
extract_iati.py

Lead Generation (Institutional Donors) via the IATI Datastore API.
Requires a free IATI API key -> put it in .env as:  IATI_API_KEY=xxxxxxxx
(register at developer.iatistandard.org)

Run:  python extract_iati.py
Output: iati_donors.xlsx
"""

import os
import re

import requests
import openpyxl
from dotenv import load_dotenv

load_dotenv()

IATI_API_KEY = os.getenv("IATI_API_KEY")
IATI_URL = "https://api.iatistandard.org/datastore/activity/select"
REQUEST_TIMEOUT = 40


def make_record(name, ref=None, source="IATI"):
    return {
        "donor_name": (name or "").strip(),
        "iati_ref": (ref or "").strip() or None,
        "source": source,
    }


def _first(v):
    """IATI Solr fields come back as lists; take the first value."""
    if isinstance(v, list):
        return v[0] if v else None
    return v


def scrape_iati(max_rows=5000):
    """Fetch India + education activities; return distinct reporting orgs (donors).
    Sector scope is the full DAC education family (111 basic, 112 secondary,
    113 post-secondary, 114 advanced) so we catch every education funder, not
    just basic-education ones (broadening 111* -> 111-114 roughly doubles it)."""
    if not IATI_API_KEY:
        print("[IATI] No IATI_API_KEY in .env - skipping.")
        return []

    print("[IATI] querying India education activities...")
    headers = {"Ocp-Apim-Subscription-Key": IATI_API_KEY}
    params = {
        # recipient India + full DAC education family (111xx-114xx)
        "q": ("recipient_country_code:IN AND (sector_code:111* OR sector_code:112* "
              "OR sector_code:113* OR sector_code:114*)"),
        "fl": "reporting_org_narrative,reporting_org_ref",
        "rows": max_rows,
        "wt": "json",
    }
    try:
        r = requests.get(IATI_URL, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        docs = r.json().get("response", {}).get("docs", [])
    except (requests.RequestException, ValueError) as e:
        print(f"[IATI] ERROR: {e}")
        return []

    records, seen = [], set()
    for d in docs:
        name = _first(d.get("reporting_org_narrative")) or ""
        ref = _first(d.get("reporting_org_ref"))
        key = name.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        records.append(make_record(name, ref=ref))
    print(f"[IATI] got {len(records)} distinct donor organizations")
    return records


def write_excel(records, filename="iati_donors.xlsx"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "IATI Donors"
    ws.append(["Donor Name", "IATI Ref", "Source"])
    for rec in sorted(records, key=lambda r: r["donor_name"].lower()):
        ws.append([rec["donor_name"], rec["iati_ref"] or "", rec["source"]])
    wb.save(filename)
    print(f"[excel] wrote {len(records)} rows -> {filename}")


def main():
    records = scrape_iati()
    write_excel(records)
    print("Done.")


if __name__ == "__main__":
    main()
