"""
extract_institutional.py

Lead Generation (Institutional Donors): build a list of major institutional
donors active in India, from:
  1. World Bank Projects API  -- live: confirms World Bank as an active donor
     and attaches its current count of India education projects.
  2. A curated seed list of major institutional donors (UN agencies, bilateral
     aid agencies, multilaterals, big global/Indian grant-making institutions).

Why curated, not fully scraped: institutional donors are a SMALL, stable,
well-known universe (a few dozen). OECD CRS / IATI can enrich this later, but
the core list barely changes and a curated authoritative list is more reliable
than a fragile SDMX/API scrape. IATI (extract_iati.py) adds breadth once a key
is available.

Run:  python extract_institutional.py
Output: institutional_donors.xlsx  (same folder)
"""

import re

import requests
import openpyxl

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}
REQUEST_TIMEOUT = 40


def make_record(name, dtype=None, focus=None, scope=None, website=None,
                year=None, amount=None, source=""):
    return {
        "donor_name": (name or "").strip(),
        "type": (dtype or "").strip() or None,
        "focus": (focus or "").strip() or None,
        "scope": (scope or "").strip() or None,
        "website": (website or "").strip() or None,
        "year": year,
        "amount": amount,
        "source": source,
    }


# --- Curated seed list: major institutional donors active in India education ---
# (name, type, focus, scope, website)
CURATED = [
    ("World Bank Group", "Multilateral", "Education, development finance", "Global", "https://www.worldbank.org/en/country/india"),
    ("Asian Development Bank (ADB)", "Multilateral", "Education, infrastructure", "Asia-Pacific", "https://www.adb.org/countries/india/main"),
    ("UNICEF India", "UN Agency", "Child education, health", "India", "https://www.unicef.org/india/"),
    ("UNESCO", "UN Agency", "Education, culture", "Global", "https://www.unesco.org/en"),
    ("UNDP India", "UN Agency", "Development, education", "India", "https://www.undp.org/india"),
    ("USAID India", "Bilateral (USA)", "Education, development", "India", "https://www.usaid.gov/india"),
    ("FCDO (UK Aid)", "Bilateral (UK)", "Education, development", "Global", "https://www.gov.uk/government/organisations/foreign-commonwealth-development-office"),
    ("GIZ (Germany)", "Bilateral (Germany)", "Technical cooperation, education", "Global", "https://www.giz.de/en/worldwide/368.html"),
    ("KfW Development Bank", "Bilateral (Germany)", "Development finance", "Global", "https://www.kfw-entwicklungsbank.de/"),
    ("JICA (Japan)", "Bilateral (Japan)", "Development, education", "Global", "https://www.jica.go.jp/english/"),
    ("European Union (EU Delegation to India)", "Multilateral", "Education, development", "India", "https://www.eeas.europa.eu/delegations/india_en"),
    ("Global Partnership for Education (GPE)", "Multilateral fund", "Education", "Global", "https://www.globalpartnership.org/"),
    ("Bill & Melinda Gates Foundation", "Foundation", "Education, health", "Global", "https://www.gatesfoundation.org/our-work/places/india"),
    ("MacArthur Foundation India", "Foundation", "Education, development", "India", "https://www.macfound.org/"),
    ("Ford Foundation India", "Foundation", "Education, social justice", "India", "https://www.fordfoundation.org/"),
    ("Central Square Foundation", "Indian grant-making", "School education", "India", "https://centralsquarefoundation.org/"),
    ("Michael & Susan Dell Foundation India", "Foundation", "Education, livelihoods", "India", "https://www.msdf.org/india/"),
]


def curated_records():
    records = []
    for name, dtype, focus, scope, website in CURATED:
        records.append(make_record(name, dtype=dtype, focus=focus,
                                   scope=scope, website=website, source="Curated"))
    return records


# --- OECD/DAC donor roster (the authoritative aid-donor list). Curated because
#     OECD's SDMX API is not reliably accessible. (name, type, country) ---
OECD_DAC = [
    # DAC bilateral aid agencies
    ("Australian Aid (DFAT)", "Bilateral aid", "Australia"),
    ("Austrian Development Agency (ADA)", "Bilateral aid", "Austria"),
    ("Enabel (Belgium)", "Bilateral aid", "Belgium"),
    ("Global Affairs Canada", "Bilateral aid", "Canada"),
    ("Danida (Denmark)", "Bilateral aid", "Denmark"),
    ("Ministry for Foreign Affairs of Finland", "Bilateral aid", "Finland"),
    ("Agence Française de Développement (AFD)", "Bilateral aid", "France"),
    ("Irish Aid", "Bilateral aid", "Ireland"),
    ("Italian Agency for Development Cooperation (AICS)", "Bilateral aid", "Italy"),
    ("KOICA (Korea)", "Bilateral aid", "South Korea"),
    ("LuxDev (Luxembourg)", "Bilateral aid", "Luxembourg"),
    ("Netherlands MFA (DGIS)", "Bilateral aid", "Netherlands"),
    ("New Zealand Aid (MFAT)", "Bilateral aid", "New Zealand"),
    ("Norad (Norway)", "Bilateral aid", "Norway"),
    ("AECID (Spain)", "Bilateral aid", "Spain"),
    ("Sida (Sweden)", "Bilateral aid", "Sweden"),
    ("SDC (Switzerland)", "Bilateral aid", "Switzerland"),
    # Multilateral development banks & funds
    ("African Development Bank (AfDB)", "Multilateral", "Global"),
    ("Inter-American Development Bank (IDB)", "Multilateral", "Global"),
    ("European Bank for Reconstruction and Development (EBRD)", "Multilateral", "Global"),
    ("International Fund for Agricultural Development (IFAD)", "Multilateral", "Global"),
    ("Islamic Development Bank (IsDB)", "Multilateral", "Global"),
    ("OPEC Fund for International Development", "Multilateral", "Global"),
    ("Global Fund to Fight AIDS, TB and Malaria", "Multilateral fund", "Global"),
    ("Gavi, the Vaccine Alliance", "Multilateral fund", "Global"),
    ("Green Climate Fund (GCF)", "Multilateral fund", "Global"),
    ("Global Environment Facility (GEF)", "Multilateral fund", "Global"),
    # UN agencies
    ("UNFPA", "UN Agency", "Global"),
    ("UNHCR", "UN Agency", "Global"),
    ("World Food Programme (WFP)", "UN Agency", "Global"),
    ("World Health Organization (WHO)", "UN Agency", "Global"),
    ("FAO", "UN Agency", "Global"),
    ("ILO", "UN Agency", "Global"),
    ("UN Women", "UN Agency", "Global"),
    ("IOM", "UN Agency", "Global"),
    ("UNIDO", "UN Agency", "Global"),
    # Private foundations reporting to OECD
    ("Mastercard Foundation", "Foundation", "Canada"),
    ("IKEA Foundation", "Foundation", "Netherlands"),
    ("Children's Investment Fund Foundation (CIFF)", "Foundation", "UK"),
    ("LEGO Foundation", "Foundation", "Denmark"),
    ("Bloomberg Philanthropies", "Foundation", "USA"),
    ("Jacobs Foundation", "Foundation", "Switzerland"),
    ("Oak Foundation", "Foundation", "Switzerland"),
    ("Rockefeller Foundation", "Foundation", "USA"),
    ("Omidyar Network", "Foundation", "USA"),
]


def oecd_dac_records():
    """Fallback only: hand-typed OECD/DAC roster, used if the live API fails."""
    return [make_record(name, dtype=dtype, scope=country, source="OECD/DAC list (fallback)")
            for (name, dtype, country) in OECD_DAC]


# --- Live OECD CRS API (Creditor Reporting System): real donors funding India
#     education. Base path is /dcd-public/ (not /public/). ---
OECD_CRS_URL = ("https://sdmx.oecd.org/dcd-public/rest/data/"
                "OECD.DCD.FSD,DSD_CRS@DF_CRS,1.6/.IND.110.100._T._T.D.Q._T..")
# Aggregate/group rows to drop (we want individual donors, not totals).
_OECD_AGG_MARKERS = ("dac ", "g7", "g20", "evolving composition", "all donors",
                     "non-dac", "regional", "eu countries")


def _clean_donor(name):
    # strip a trailing code in brackets, e.g. "Asian Development Bank [AsDB]"
    return re.sub(r"\s*\[[^\]]+\]\s*$", "", name or "").strip()


def _is_oecd_aggregate(name):
    low = name.lower()
    if any(m in low for m in _OECD_AGG_MARKERS):
        return True
    return low.endswith("countries") or low.endswith("members")


def scrape_oecd_crs(start_period="2018"):
    """Fetch distinct donors funding India (recipient=IND) in education (sector=110)
    from the live OECD CRS API. Returns [] on failure (fallback handled by caller)."""
    import csv
    import io
    print("[OECD CRS] fetching India education donors (live API)...")
    try:
        r = requests.get(OECD_CRS_URL,
                         params={"startPeriod": start_period,
                                 "dimensionAtObservation": "AllDimensions",
                                 "format": "csvfilewithlabels"},
                         headers={"User-Agent": "Mozilla/5.0 Chrome/124.0 Safari/537.36"},
                         timeout=120)
        r.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(r.text)))
    except (requests.RequestException, ValueError) as e:
        print(f"[OECD CRS] ERROR: {e}")
        return []
    seen, out = set(), []
    for row in rows:
        name = _clean_donor(row.get("Donor"))
        if not name or _is_oecd_aggregate(name):
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(make_record(name, dtype="Aid donor (OECD CRS)", source="OECD CRS"))
    print(f"[OECD CRS] got {len(out)} donors")
    return out


def oecd_all_records(start_period="2018"):
    """Keep ALL OECD CRS India-education rows (donor x year x amount), no dedupe.
    Returns records with year + amount so the full aid-flow detail is preserved."""
    import extract_oecd as eo
    rows = eo.fetch_rows(start_period=start_period)
    out = []
    for r in rows:
        name = (r.get("Donor") or "").strip()
        if not name:
            continue
        raw = r.get("OBS_VALUE")
        try:
            amount = float(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            amount = None
        out.append(make_record(name, dtype="Aid donor (OECD CRS)",
                               scope=r.get("Recipient"), year=r.get("TIME_PERIOD"),
                               amount=amount, source="OECD CRS"))
    return out


# --- World Bank: live count of India education projects (confirms WB active) ---
WORLDBANK_API = "https://search.worldbank.org/api/v3/projects"


def scrape_worldbank():
    """Query the World Bank Projects API for India education projects. World Bank
    is one donor, so this returns a single donor record enriched with the live
    project count (real, dynamic data)."""
    print("[WorldBank] querying India education projects...")
    try:
        params = {
            "format": "json",
            "rows": 500,
            "countryshortname_exact": "India",
            "qterm": "education",
            "fl": "project_name,countryshortname,totalamt,boardapprovaldate",
        }
        r = requests.get(WORLDBANK_API, params=params,
                         headers=BROWSER_HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        projects = r.json().get("projects", {})
        count = len(projects)
    except (requests.RequestException, ValueError) as e:
        print(f"[WorldBank] ERROR: {e}")
        count = 0

    focus = f"Education, development finance ({count} India education projects found)" if count \
        else "Education, development finance"
    print(f"[WorldBank] {count} India education projects")
    return [make_record("World Bank Group", dtype="Multilateral", focus=focus,
                        scope="Global", website="https://www.worldbank.org/en/country/india",
                        source="World Bank API")]


def normalize_name(name):
    key = (name or "").lower().strip()
    key = re.sub(r"\(.*?\)", " ", key)          # drop parenthetical qualifiers
    key = re.sub(r"[.,&'\-/]", " ", key)
    return re.sub(r"\s+", " ", key).strip()


def dedupe(records):
    print("[dedupe] merging...")
    by_key = {}
    for rec in records:
        key = normalize_name(rec["donor_name"])
        if not key:
            continue
        if key in by_key:
            existing = by_key[key]
            for f in ("type", "focus", "scope", "website"):
                if not existing[f] and rec[f]:
                    existing[f] = rec[f]
            srcs = set(existing["source"].split(", ")) | {rec["source"]}
            existing["source"] = ", ".join(sorted(s for s in srcs if s))
        else:
            by_key[key] = dict(rec)
    merged = list(by_key.values())
    print(f"[dedupe] {len(records)} raw -> {len(merged)} unique")
    return merged


def write_excel(records, filename="institutional_donors.xlsx"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Institutional Donors"
    ws.append(["Donor Name", "Type", "Focus", "Scope", "Year",
               "Amount (USD million)", "Website", "Source(s)"])
    for rec in records:
        ws.append([rec["donor_name"], rec.get("type") or "", rec.get("focus") or "",
                   rec.get("scope") or "", rec.get("year") or "",
                   rec.get("amount") if rec.get("amount") is not None else "",
                   rec.get("website") or "", rec["source"]])
    wb.save(filename)
    print(f"[excel] wrote {len(records)} rows -> {filename}")


def main():
    # List sources (curated + World Bank) deduped to unique donors,
    # then ALL OECD CRS rows appended as-is (year x amount preserved).
    list_records = dedupe(curated_records() + scrape_worldbank())
    oecd = oecd_all_records()
    write_excel(list_records + (oecd if oecd else oecd_dac_records()))
    print("Done.")


if __name__ == "__main__":
    main()
