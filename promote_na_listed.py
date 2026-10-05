"""
promote_na_listed.py

Class NA wali companies mein se jo NSE/BSE pe listed hain aur "Limited" company
hain (mutual fund / ETF nahi), unhe Class A mein daalo.

NSE/BSE check: universe ka "Source(s)" column BSE/NSE ho, ya ISIN / naam official
NSE + BSE equity list mein mile (MCA Maharashtra wali rows ke liye).
Classes MongoDB ("pbt" set) mein update hoti hain, aur local
company_pbt_cache.jsonl ho to usme bhi.

Usage:
    python promote_na_listed.py            # update
    python promote_na_listed.py --dry-run  # sirf list print karo
"""
import argparse
import json
import os
import re

import db
from categorize_pbt import CACHE_FILE, _name_key, fetch_exchange_rows

FUND_RE = re.compile(r"mutual\s*fund|\bfund\b|\betf\b|\bscheme\b", re.I)
LIMITED_RE = re.compile(r"(limited|ltd)\b", re.I)


def _key(name):
    """_name_key() + MCA quirks: 'SERVICESLIMITED', '(TRANSFERRED FROM ...)'."""
    name = re.sub(r"\(transferred.*$", "", name or "", flags=re.I)
    name = re.sub(r"(?<=[A-Za-z.])(limited|ltd\.?)\s*$", " limited", name, flags=re.I)
    return _name_key(name)


def is_limited_company(name, isin):
    return bool(LIMITED_RE.search(name or "")) and not FUND_RE.search(name or "") \
        and not (isin or "").startswith("INF")  # INF = mutual fund units


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    meta = db.get_lead_meta(db.LEAD_PBT_KEY)
    pbt = db.load_lead_set(meta)
    umeta = db.get_lead_meta(db.LEAD_UNIVERSE_KEY)
    i_src = umeta["headers"].index("Source(s)")
    sources = {d["r"][0]: d["r"][i_src] or "" for d in db.load_lead_set(umeta)}

    ex_isins, ex_names = set(), set()
    for row in fetch_exchange_rows():
        ex_isins.add(row["isin"])
        ex_names.update(_key(n) for n in row["names"] if n)

    promoted = []
    for rec in pbt:
        name, isin = rec["company_name"], rec.get("isin") or ""
        if rec["category"] != "NA" or not is_limited_company(name, isin):
            continue
        on_exchange = (re.search(r"\b(BSE|NSE)\b", sources.get(name, ""))
                       or (isin and isin in ex_isins) or _key(name) in ex_names)
        if on_exchange:
            rec["category"] = "A"
            promoted.append(rec)
            print(f"[A] {name} ({isin or 'no ISIN'}) | {sources.get(name, '')}")
    print(f"[done] {len(promoted)} NA companies -> A")
    if args.dry_run or not promoted:
        return

    db.save_lead_set(db.LEAD_PBT_KEY, pbt)
    print(f"[mongo] saved {len(pbt)} PBT records")
    if os.path.exists(CACHE_FILE):  # load_cache() keeps the last line per company
        with open(CACHE_FILE, "a", encoding="utf-8") as f:
            for rec in promoted:
                f.write(json.dumps(dict(rec, note="Promoted NA -> A (NSE/BSE listed Ltd)"),
                                   ensure_ascii=False) + "\n")
        print(f"[cache] appended {len(promoted)} records -> {CACHE_FILE}")


if __name__ == "__main__":
    main()
