"""
extract_hurun.py

Lead Generation (HNIs / individual & family philanthropists). Two sources:
  1. CURATED philanthropists -- hand-picked from the EdelGive Hurun India
     PHILANTHROPY List (most generous givers). Donation amounts where known.
  2. Hurun India RICH List (top 100 by net worth) -- scraped from a public
     news table. This is WEALTH (giving *capacity*), NOT confirmed giving.
     Per the framework: never treat net worth as a presumed grant budget.

Run:  python extract_hurun.py
Output: hurun_philanthropists.xlsx
"""

import re

import requests
from bs4 import BeautifulSoup
import openpyxl

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}
REQUEST_TIMEOUT = 40

# Public news table carrying the Hurun India Rich List 2025 top 100.
RICHLIST_URL = ("https://www.financialexpress.com/life/lifestyle-top-100-richest-"
                "people-in-india-list-includes-mukesh-ambani-this-new-entrant-"
                "made-history-hurun-report-3995516/")


def make_record(name, wealth_source=None, focus=None, donation_cr=None,
                net_worth_cr=None, city=None, source="Hurun 2025"):
    return {
        "name": (name or "").strip(),
        "wealth_source": (wealth_source or "").strip() or None,
        "focus": (focus or "").strip() or None,
        "donation_cr": donation_cr,
        "net_worth_cr": net_worth_cr,
        "city": (city or "").strip() or None,
        "source": source,
    }


# Curated from the EdelGive Hurun India PHILANTHROPY List (most generous givers).
# (name, wealth_source, focus, donation_cr)  -- donation_cr None where unknown.
CURATED = [
    ("Shiv Nadar & family", "HCL Technologies", "Education", 2708),
    ("Mukesh Ambani & family", "Reliance Industries", "Education, health, rural", 626),
    ("Bajaj family", "Bajaj Group", "Healthcare, education", 446),
    ("Rohini Nilekani", "Infosys (Nilekani)", "Education, environment, social", 204),
    ("Nandan Nilekani", "Infosys", "Education, technology for good", None),
    ("Kumar Mangalam Birla & family", "Aditya Birla Group", "Education, healthcare", None),
    ("Gautam Adani & family", "Adani Group", "Education, healthcare, skilling", None),
    ("Anil Agarwal & family", "Vedanta", "Child welfare, nutrition, education", None),
    ("Kiran Mazumdar-Shaw", "Biocon", "Healthcare, education", None),
    ("Azim Premji", "Wipro", "Education", None),
    ("Ajay Piramal & family", "Piramal Group", "Healthcare, education", None),
    ("Anand Mahindra", "Mahindra Group", "Education, girl child", None),
    ("Sunil Bharti Mittal & family", "Bharti Enterprises", "Education (Satya Bharti)", None),
    ("Cyrus & Adar Poonawalla", "Serum Institute of India", "Healthcare, sanitation", None),
    ("Nusli Wadia & family", "Wadia Group", "Healthcare, education", None),
    ("Subroto & Susmita Bagchi", "Mindtree", "Healthcare, arts, education", None),
    ("Ashish Dhawan", "ChrysCapital / Central Square", "School education", None),
    ("PNC Menon & family", "Sobha Group", "Education, poverty", None),
    ("Nikhil Kamath", "Zerodha", "Climate, education (Young Indian Philanthropic Pledge)", None),
    ("Vellayan Subbiah & family", "Murugappa Group", "Education, healthcare", None),
    ("Ronnie Screwvala", "RSVP / upGrad", "Rural development, education", None),
    ("Kris Gopalakrishnan", "Infosys (co-founder)", "Science & research, education", None),
    ("S.D. Shibulal & family", "Infosys (co-founder)", "Education, healthcare", None),
    ("Savitri Jindal & family", "O.P. Jindal Group", "Education, healthcare", None),
    ("Sajjan Jindal & family", "JSW Group", "Education, healthcare, arts", None),
    ("Naveen Jindal", "Jindal Steel & Power", "Education, sports", None),
    ("Hinduja family", "Hinduja Group", "Healthcare, education", None),
    ("Munjal family", "Hero MotoCorp", "Education, rural, sports", None),
    ("G.M. Rao & family", "GMR Group", "Education (GMR Varalakshmi Foundation)", None),
    ("Venu Srinivasan & family", "TVS Motor", "Rural development, education", None),
    ("Dilip Shanghvi & family", "Sun Pharmaceutical", "Healthcare, education", None),
    ("Uday Kotak & family", "Kotak Mahindra Bank", "Healthcare, education", None),
    ("Radhakishan Damani & family", "Avenue Supermarts (DMart)", "Healthcare, education", None),
    ("Ranjan Pai & family", "Manipal Group", "Education, healthcare", None),
    ("Anu Aga & Meher Pudumjee", "Thermax", "Education, health", None),
    ("Rishad Premji", "Wipro", "Education", None),
    ("Nithin Kamath", "Zerodha", "Climate, ecology (Rainmatter)", None),
    ("Habil Khorakiwala & family", "Wockhardt", "Healthcare, education", None),
    ("Vinod & Neena Gupta", "NIIT", "Education", None),
    ("Leena Gandhi Tewari & family", "USV", "Healthcare, education", None),
]


def curated_records():
    return [make_record(name, wealth_source=ws, focus=focus, donation_cr=amt,
                        source="Hurun Philanthropy 2025 (curated)")
            for (name, ws, focus, amt) in CURATED]


def _num(s):
    s = re.sub(r"[^\d]", "", str(s or ""))
    return int(s) if s.isdigit() else None


def scrape_richlist():
    """Scrape the Hurun India Rich List 2025 top 100 (net worth) from the public
    news table. Net worth = giving capacity, not confirmed giving."""
    print("[Hurun Rich List] scraping top 100...")
    records = []
    try:
        r = requests.get(RICHLIST_URL, headers=BROWSER_HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"[Hurun Rich List] ERROR: {e}")
        return records

    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table")
    if not table:
        print("[Hurun Rich List] table not found (page layout changed).")
        return records

    for tr in table.find_all("tr"):
        cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) < 6 or cells[0].lower() == "rank":
            continue
        rank, name, wealth, company, industry, city = cells[:6]
        if not name:
            continue
        records.append(make_record(name, wealth_source=company, focus=industry,
                                   net_worth_cr=_num(wealth), city=city,
                                   source="Hurun Rich List 2025"))
    print(f"[Hurun Rich List] got {len(records)} individuals")
    return records


def normalize_name(name):
    key = (name or "").lower().strip()
    key = re.sub(r"&\s*family", " ", key)
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
            for f in ("wealth_source", "focus", "donation_cr", "net_worth_cr", "city"):
                if not existing[f] and rec[f]:
                    existing[f] = rec[f]
            srcs = set(existing["source"].split(", ")) | {rec["source"]}
            existing["source"] = ", ".join(sorted(s for s in srcs if s))
        else:
            by_key[key] = dict(rec)
    merged = list(by_key.values())
    print(f"[dedupe] {len(records)} raw -> {len(merged)} unique")
    return merged


def write_excel(records, filename="hurun_philanthropists.xlsx"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "HNI Philanthropists"
    ws.append(["Name", "Wealth Source", "Focus / Industry", "Donation (Rs Cr)",
               "Net Worth (Rs Cr)", "City", "Source"])
    for rec in sorted(records, key=lambda r: (-(r["net_worth_cr"] or r["donation_cr"] or 0),
                                              r["name"].lower())):
        ws.append([rec["name"], rec["wealth_source"] or "", rec["focus"] or "",
                   rec["donation_cr"] or "", rec["net_worth_cr"] or "",
                   rec["city"] or "", rec["source"]])
    wb.save(filename)
    print(f"[excel] wrote {len(records)} rows -> {filename}")


def main():
    records = curated_records() + scrape_richlist()
    merged = dedupe(records)
    write_excel(merged)
    print("Done.")


if __name__ == "__main__":
    main()
