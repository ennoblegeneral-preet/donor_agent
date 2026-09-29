"""
extract_mca.py

Lead Generation (CSR / Corporates) from the official MCA Company Master Data
on data.gov.in -- the Registrar of Companies registry (listed AND unlisted).

Uses the data.gov.in API key stored in .env as:  csr_corporate_data=<key>

Pulls the ENTIRE MAHARASHTRA registry (~426k companies, 31 March 2021 snapshot),
sorted by paid-up capital (largest first). No filtering here -- all fields are
kept so a CSR-threshold filter can be applied later.

Run:  python extract_mca.py
Output: mca_maharashtra.xlsx
"""

import os

import requests
import openpyxl
from dotenv import dotenv_values

API_KEY = dotenv_values(".env").get("csr_corporate_data") or os.getenv("csr_corporate_data")
API_BASE = "https://api.data.gov.in/resource"

# State -> latest data.gov.in resource id (Maharashtra only for now).
STATE_RESOURCES = {
    "Maharashtra": "b83e1bfa-14a6-4ac1-8ce2-3f74b82a0a5d",  # upto 31 March 2021
}

PAGE_SIZE = 10000            # max the API allows per call
MAX_RECORDS = 500000         # safety cap; loop stops earlier when the state runs out
REQUEST_TIMEOUT = 180
HEADERS = {"User-Agent": "Mozilla/5.0 Chrome/124.0 Safari/537.36"}

FIELDS = [
    "corporate_identification_number", "company_name", "company_status",
    "company_class", "company_category", "company_sub_category",
    "date_of_registration", "registered_state", "authorized_cap",
    "paidup_capital", "industrial_class", "principal_business_activity_as_per_cin",
    "registered_office_address", "registrar_of_companies", "email_addr",
]


def fetch_state(state, resource_id):
    """Fetch ALL companies for one state, sorted by paid-up capital (desc)."""
    if not API_KEY:
        print("[MCA] No 'csr_corporate_data' key in .env - aborting.")
        return []
    print(f"[MCA] fetching entire {state} registry (largest first)...")
    records, offset = [], 0
    while offset < MAX_RECORDS:
        params = {
            "api-key": API_KEY, "format": "json",
            "limit": PAGE_SIZE, "offset": offset,
            "sort[paidup_capital]": "desc",
        }
        try:
            r = requests.get(f"{API_BASE}/{resource_id}", params=params,
                             headers=HEADERS, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            batch = r.json().get("records", [])
        except (requests.RequestException, ValueError) as e:
            print(f"[MCA] {state} offset {offset} ERROR: {e}")
            break
        if not batch:
            break
        records.extend(batch)
        print(f"[MCA] {state}: +{len(batch)} (total {len(records)})")
        offset += len(batch)
        if len(batch) < PAGE_SIZE:
            break
    print(f"[MCA] {state}: got {len(records)} companies")
    return records


def write_excel(records, filename="mca_maharashtra.xlsx"):
    wb = openpyxl.Workbook(write_only=True)
    ws = wb.create_sheet("MCA Companies")
    ws.append(["CIN", "Company Name", "Status", "Class", "Category", "Sub-Category",
               "Date of Registration", "State", "Authorized Capital (Rs)",
               "Paid-up Capital (Rs)", "Industrial Class", "Business Activity",
               "Registered Address", "RoC", "Email"])
    for rec in records:
        ws.append([rec.get(f, "") for f in FIELDS])
    wb.save(filename)
    print(f"[excel] wrote {len(records)} rows -> {filename}")


def cin_listing_status(cin):
    """MCA CIN encodes listing status in its first letter: L = Listed, U = Unlisted."""
    return "Listed" if (cin or "").strip()[:1].upper() == "L" else "Unlisted"


def pipeline_records():
    """Return MCA companies in the company-universe record shape so they merge
    into the CSR/Corporates pipeline (dedupes by name against NSE/BSE)."""
    import extract_company as ec
    raw = []
    for state, rid in STATE_RESOURCES.items():
        raw += fetch_state(state, rid)
    out = []
    for r in raw:
        name = r.get("company_name", "")
        if not name:
            continue
        out.append(ec.make_record(
            name,
            isin=None,
            sector=r.get("principal_business_activity_as_per_cin"),
            listing_status=cin_listing_status(r.get("corporate_identification_number")),
            source="MCA Maharashtra",
        ))
    return out


def main():
    all_records = []
    for state, rid in STATE_RESOURCES.items():
        all_records += fetch_state(state, rid)
    write_excel(all_records)
    print("Done.")


if __name__ == "__main__":
    main()
