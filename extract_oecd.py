"""
extract_oecd.py

OECD CRS (Creditor Reporting System) education aid to India, LIVE from the OECD
API. Keeps EVERY row as-is (donor x year x amount) -- no deduplication -- so the
year-by-year giving amounts are preserved.

API base path is /dcd-public/ (not /public/). Dataflow: DSD_CRS@DF_CRS v1.6.
Filter key `.IND.110.100._T._T.D.Q._T..` = all donors, recipient India (IND),
sector Education (110), measure ODA (100).

Run:  python extract_oecd.py
Output: oecd_crs_india_education.xlsx  (all rows)
"""

import csv
import io

import requests
import openpyxl

OECD_URL = ("https://sdmx.oecd.org/dcd-public/rest/data/"
            "OECD.DCD.FSD,DSD_CRS@DF_CRS,1.6/.IND.110.100._T._T.D.Q._T..")
START_PERIOD = "2018"
HEADERS = {"User-Agent": "Mozilla/5.0 Chrome/124.0 Safari/537.36"}
REQUEST_TIMEOUT = 120


def fetch_rows(start_period=START_PERIOD):
    """Return every OECD CRS row (India + Education) as dicts -- no dedupe."""
    print("[OECD CRS] fetching India education aid rows (live)...")
    try:
        r = requests.get(OECD_URL,
                         params={"startPeriod": start_period,
                                 "dimensionAtObservation": "AllDimensions",
                                 "format": "csvfilewithlabels"},
                         headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(r.text)))
    except (requests.RequestException, ValueError) as e:
        print(f"[OECD CRS] ERROR: {e}")
        return []
    print(f"[OECD CRS] got {len(rows)} rows")
    return rows


def write_excel(rows, filename="oecd_crs_india_education.xlsx"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "OECD CRS India Education"
    ws.append(["Donor", "Recipient", "Sector", "Year", "Measure",
               "Flow Type", "Channel", "Amount (USD million)", "Unit"])
    for r in rows:
        ws.append([
            r.get("Donor", ""), r.get("Recipient", ""), r.get("Sector", ""),
            r.get("TIME_PERIOD", ""), r.get("Measure", ""), r.get("Flow type", ""),
            r.get("Channel", ""), r.get("OBS_VALUE", ""), r.get("Unit of measure", ""),
        ])
    wb.save(filename)
    print(f"[excel] wrote {len(rows)} rows -> {filename}")


def main():
    rows = fetch_rows()
    write_excel(rows)
    print("Done.")


if __name__ == "__main__":
    main()
