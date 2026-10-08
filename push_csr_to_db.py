"""
push_csr_to_db.py

Upload the local CSR / Corporates data to MongoDB exactly as it is, so the
deployed app (which can't reach BSE / MCA) shows the same list as this laptop:
  - company_universe.xlsx      -> lead_csr_data / lead_csr_meta ("universe")
  - company_pbt_cache.jsonl    -> lead_csr_data / lead_csr_meta ("pbt")
Nothing is fetched from NSE / BSE / MCA / the unlisted sites.

Run again after categorize_pbt.py / verify_pbt_matches.py to refresh the classes.

Usage:
    python push_csr_to_db.py            # upload both
    python push_csr_to_db.py --dry-run  # only print what would be uploaded
    python push_csr_to_db.py --force    # upload even if much smaller than MongoDB's copy
"""
import argparse
from collections import Counter

import openpyxl

UNIVERSE_FILE = "company_universe.xlsx"
PBT_FIELDS = ("company_name", "isin", "screener_name", "category")
SHRINK_LIMIT = 0.8  # refuse a universe below 80% of the saved row count


def read_universe():
    wb = openpyxl.load_workbook(UNIVERSE_FILE, read_only=True)
    rows = list(wb.active.iter_rows(values_only=True))
    wb.close()
    headers = [str(h) if h is not None else "" for h in rows[0]]
    return headers, rows[1:]


def read_pbt():
    from categorize_pbt import load_cache  # latest record per company name
    return [{f: rec.get(f) for f in PBT_FIELDS} for rec in load_cache().values()]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true",
                        help="upload even if the universe is much smaller than MongoDB's")
    args = parser.parse_args()

    headers, rows = read_universe()
    i_status = headers.index("Listing Status")
    print(f"[universe] {len(rows)} rows | "
          + " ".join(f"{k}={v}" for k, v in Counter(r[i_status] for r in rows).items()))
    pbt = read_pbt()
    print(f"[pbt] {len(pbt)} records | "
          + " ".join(f"{k}={v}" for k, v in sorted(Counter(r["category"] for r in pbt).items())))
    import db
    saved = (db.get_lead_meta(db.LEAD_UNIVERSE_KEY) or {}).get("count", 0)
    print(f"[mongo] universe currently has {saved} rows")
    # A failed source (e.g. MCA down) shrinks the file; don't overwrite good data with it.
    if len(rows) < saved * SHRINK_LIMIT and not args.force:
        print(f"[stop] {len(rows)} rows is under {SHRINK_LIMIT:.0%} of MongoDB's {saved} "
              "- check the file, or pass --force to upload anyway")
        return
    if args.dry_run:
        return

    from extract_company import save_universe_rows
    save_universe_rows(headers, rows)
    db.save_lead_set(db.LEAD_PBT_KEY, pbt)
    print(f"[mongo] saved {len(pbt)} PBT records")


if __name__ == "__main__":
    main()
