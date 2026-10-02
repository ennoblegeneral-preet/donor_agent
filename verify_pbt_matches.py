"""
Verify every Screener match in company_pbt_cache.jsonl against official NSE/BSE codes.

Har company ke liye: jo Screener page use hua tha use kholo (redirect follow karke),
us page pe likhe BSE code / NSE symbol padho, aur hamare ISIN ke official codes
(NSE EQUITY_L.csv + BSE scrip list) se match karo:
    Confirmed - codes match, naam bhi same
    Renamed   - codes match, naam alag (company ne naam badla) -> sahi hai
    Fixed     - codes match nahi hue -> official code wala sahi page lo, PBT dobara nikalo
    Review    - official code mila hi nahi (ISIN nahi / exchange list mein nahi) -> Class "Review"
Do ya zyada rows ek hi Screener company pe jaayein to report mein "Duplicate" flag hota hai
(sirf flag, kuch hide nahi hota).

Resumable: verified record cache mein append hota hai (verify_status ke saath);
dobara chalane par already-verified companies skip hoti hain.

Usage:
    python verify_pbt_matches.py                      # saari companies
    python verify_pbt_matches.py --limit 20           # pehli 20 (testing)
    python verify_pbt_matches.py --only "EMCO LIMITED" --dry-run   # test, cache mein kuch save nahi
"""
import argparse
import difflib
import json
import re
import time

import openpyxl
import requests
from bs4 import BeautifulSoup

from categorize_pbt import (CACHE_FILE, DELAY_SECONDS, _name_key, fetch_exchange_rows,
                            fetch_pbt_fields, load_cache, load_listed_companies,
                            screener_page_name, write_output)
from search_tool import SCREENER_HEADERS

REPORT_FILE = "pbt_verification_report.xlsx"
SAME_NAME_RATIO = 0.85


def load_official_index():
    """ISIN / issuer prefix (ISIN ke pehle 7 chars, sirf INE) / lowercase name
    -> {"codes": set(NSE symbols + BSE codes), "names": set(official names)}."""
    by_isin, by_issuer, by_name = {}, {}, {}

    def _add(index, key, row):
        entry = index.setdefault(key, {"codes": set(), "names": set()})
        entry["codes"].add(row["code"].upper())
        entry["names"].update(n for n in row["names"] if n)

    for row in fetch_exchange_rows():
        if row["isin"]:
            _add(by_isin, row["isin"], row)
            if row["isin"].startswith("INE"):
                _add(by_issuer, row["isin"][:7], row)  # stock split ke baad ISIN badalta hai
        for n in row["names"]:
            if n:
                _add(by_name, n.lower(), row)
    print(f"[codes] {len(by_isin)} ISINs, {len(by_name)} names loaded")
    return by_isin, by_issuer, by_name


def official_for(rec, index):
    by_isin, by_issuer, by_name = index
    isin = (rec.get("isin") or "").strip()
    if isin:
        found = by_isin.get(isin) or (by_issuer.get(isin[:7]) if isin.startswith("INE") else None)
        if found:
            return found
    return by_name.get(rec["company_name"].strip().lower())


def read_screener_page(url):
    """(final_url, h1 name, set of BSE/NSE codes on the page); page nahi to (url, None, set()).
    Network error par None (next run retry)."""
    try:
        resp = requests.get(url, headers=SCREENER_HEADERS, timeout=15)
        if resp.status_code == 429:
            print("[rate-limit] Screener 429, 60s wait...")
            time.sleep(60)
            resp = requests.get(url, headers=SCREENER_HEADERS, timeout=15)
    except requests.RequestException as e:
        print(f"[error] {url}: {e}")
        return None
    if not resp.ok or "/company/" not in resp.url:
        return resp.url, None, set()
    soup = BeautifulSoup(resp.content, "html.parser")
    h1 = soup.find("h1")
    codes = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"bseindia\.com/stock-share-price/[^/]+/[^/]+/(\d+)/?$", href)
        if m:
            codes.add(m.group(1))
        m = re.search(r"nseindia\.com/get-quotes/equity\?symbol=([^&]+)", href)
        if m:
            codes.add(m.group(1).upper())
    return resp.url, (h1.get_text(strip=True) if h1 else None), codes


def _same_name(a, b):
    return difflib.SequenceMatcher(None, _name_key(a), _name_key(b)).ratio() >= SAME_NAME_RATIO


def verify(rec, index):
    """Updated record (verify_status ke saath), ya None on transient error."""
    rec = dict(rec)
    official = official_for(rec, index)
    rec["official_codes"] = sorted(official["codes"]) if official else []
    rec["class_before"] = rec["category"]

    if not rec.get("screener_url"):
        rec.update(verify_status="Not on Screener", page_codes=[], current_name=None,
                   verify_note="Screener par koi page nahi mila")
        return rec

    page = read_screener_page(rec["screener_url"])
    if page is None:
        return None
    final_url, page_name, page_codes = page
    rec["page_codes"] = sorted(page_codes)
    rec["current_name"] = page_name

    if official and page_codes & official["codes"]:
        if _same_name(rec["company_name"], page_name or ""):
            rec.update(verify_status="Confirmed", verify_note=None)
        else:
            rec.update(verify_status="Renamed",
                       verify_note=f"Naam badla: {rec['company_name']} -> {page_name}")
        return rec

    if official:
        # Galat page - official code wala sahi page dhundo (NSE symbol pehle, phir BSE code).
        old = f"{rec.get('screener_name')} ({final_url})"
        for code in sorted(official["codes"], key=lambda c: c.isdigit()):
            url = f"https://www.screener.in/company/{code}/consolidated/"
            name = screener_page_name(url)
            if not name:
                continue
            fields = fetch_pbt_fields(url, fallback=True)
            if fields is None:
                return None
            rec.update(fields)
            rec.update(screener_name=name, current_name=name, page_codes=[code],
                       verify_status="Fixed", verify_note=f"Galat page tha: {old}")
            return rec
        rec.update(screener_name=None, screener_url=None, pbt={}, calc_years=[],
                   average_pbt=None, csr_budget_2pct=None, category="NA",
                   verify_status="Fixed",
                   verify_note=f"Galat page tha: {old}; sahi company Screener par nahi")
        return rec

    # Official code nahi mila - confirm nahi ho sakta.
    if rec["category"] == "NA":
        rec.update(verify_status="Unconfirmed",
                   verify_note="Official NSE/BSE code nahi mila (data bhi nahi)")
    else:
        rec.update(category="Review", verify_status="Review",
                   verify_note="Official NSE/BSE code nahi mila - manually check karo")
    return rec


def flag_duplicates(records):
    """Duplicate rows flag karo: ek hi Screener company (same page codes / URL) pe jaayein,
    ya naam same ho (jaise MCA 'H S INDIA LIMITED' aur BSE 'H.S.India Ltd.')."""
    groups = {}
    for rec in records:
        keys = [("name", _name_key(rec["company_name"]))]
        if rec.get("screener_url") and rec["verify_status"] != "Review":
            keys.append(("page", tuple(rec.get("page_codes") or []) or
                         rec["screener_url"].split("/company/")[-1].split("/")[0].upper()))
        for key in keys:
            groups.setdefault(key, []).append(rec)
    dup = [g for g in groups.values() if len(g) > 1]
    for group in dup:
        for rec in group:
            others = {r["company_name"] for r in group if r is not rec}
            prev = set(filter(None, (rec.get("duplicate_of") or "").split(", ")))
            rec["duplicate_of"] = ", ".join(sorted(prev | others))
    return len(dup)


def write_report(records):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Verification"
    ws.append(["Company Name", "ISIN", "Status", "Note", "Class Before", "Class Now",
               "Screener Name (current)", "Screener URL", "Codes on Page",
               "Official NSE/BSE Codes", "Duplicate Of"])
    for rec in sorted(records, key=lambda r: (r["verify_status"], r["company_name"].lower())):
        ws.append([rec["company_name"], rec.get("isin"), rec["verify_status"],
                   rec.get("verify_note"), rec.get("class_before"), rec["category"],
                   rec.get("current_name"), rec.get("screener_url"),
                   ", ".join(rec.get("page_codes") or []),
                   ", ".join(rec.get("official_codes") or []), rec.get("duplicate_of")])
    wb.save(REPORT_FILE)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--only", action="append", default=[],
                        help="sirf ye company naam (repeat kar sakte ho)")
    parser.add_argument("--dry-run", action="store_true", help="cache/report mein kuch save nahi")
    args = parser.parse_args()

    names = {c["company_name"] for c in load_listed_companies()}
    done = {n: r for n, r in load_cache().items() if n in names}
    if args.only:
        wanted = {n.lower() for n in args.only}
        todo = [r for r in done.values() if r["company_name"].lower() in wanted]
    else:
        todo = [r for r in done.values() if "verify_status" not in r]
    if args.limit:
        todo = todo[:args.limit]
    print(f"[start] {len(done)} companies, {len(todo)} to verify"
          + (" (dry run)" if args.dry_run else ""))
    index = load_official_index()

    with open(CACHE_FILE, "a", encoding="utf-8") as cache:
        for n, rec in enumerate(todo, 1):
            try:
                new = verify(rec, index)
            except Exception as e:
                print(f"[error] {rec['company_name']}: {e}")
                new = None
            if new:
                change = (f"{new['class_before']} -> {new['category']}"
                          if new["class_before"] != new["category"] else new["category"])
                print(f"[{n}/{len(todo)}] {new['company_name']} -> {new['verify_status']} "
                      f"| {new.get('current_name')} | {change}")
                if not args.dry_run:
                    done[new["company_name"]] = new
                    cache.write(json.dumps(new, ensure_ascii=False) + "\n")
                    cache.flush()
            time.sleep(DELAY_SECONDS)

    if args.dry_run:
        return
    verified = [r for r in done.values() if "verify_status" in r]
    dup_groups = flag_duplicates(verified)
    write_report(verified)
    write_output(list(done.values()))
    counts = {}
    for r in verified:
        counts[r["verify_status"]] = counts.get(r["verify_status"], 0) + 1
    print(f"[report] {len(verified)} verified -> {REPORT_FILE} | "
          + " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
          + f" | duplicate groups={dup_groups}")


if __name__ == "__main__":
    main()
