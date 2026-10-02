"""
One-company test for the Listed A/B/C/D banding plan.

Metric = Screener P&L "Expenses" row (latest completed FY).
Bands:  A > 1000 Cr | B 500-1000 | C 100-500 | D < 100
        (boundaries land in the higher band: 1000->A, 500->B, 100->C)

Reuses the codebase's own Screener resolver + table parser so the number
here matches exactly what the real pipeline would read. Prints Sales too,
purely as a sanity anchor next to Expenses on the Screener P&L.

Run:  python test_expenses_band.py "Reliance Industries"
      python test_expenses_band.py            # defaults to Reliance
"""
import sys
import requests
from bs4 import BeautifulSoup

from search_tool import (
    find_company_on_screener,
    _extract_screener_table_row,
    SCREENER_HEADERS,
)


def band_by_expenses(value_cr):
    """A > 1000 | B 500-1000 | C 100-500 | D < 100. Boundary -> higher band."""
    if value_cr is None:
        return ""
    if value_cr > 1000:
        return "A"
    if value_cr >= 500:
        return "B"
    if value_cr >= 100:
        return "C"
    return "D"


def get_expenses_from_screener(screener_url):
    """Read the latest-FY 'Expenses' (and 'Sales') from the Screener P&L table."""
    resp = requests.get(screener_url, headers=SCREENER_HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")

    pnl_section = soup.find("section", id="profit-loss")
    if not pnl_section or not pnl_section.find("table"):
        return None
    pnl_table = pnl_section.find("table")

    expenses = _extract_screener_table_row(pnl_table, "Expenses")
    sales = _extract_screener_table_row(pnl_table, "Sales")
    if not expenses:
        return {"latest_fy": None, "expenses": None, "sales": None,
                "all_expenses": expenses}

    # Newest FY first; TTM already excluded by _extract_screener_table_row.
    fiscal_years = sorted(expenses.keys(), reverse=True)
    latest_fy = fiscal_years[0]
    return {
        "latest_fy": latest_fy,
        "expenses": expenses.get(latest_fy),
        "sales": sales.get(latest_fy),
        "all_expenses": expenses,
    }


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "Reliance Industries"
    print(f"\n=== Listed banding test :: {name} ===\n")

    match, _ = find_company_on_screener(name)
    if not match:
        print(f"[FAIL] '{name}' not found on Screener.")
        return
    url = match["url"]
    print(f"Resolved : {match['name']}")
    print(f"Screener : {url}\n")

    fin = get_expenses_from_screener(url)
    if not fin or fin["expenses"] is None:
        print("[FAIL] Could not read an 'Expenses' row from the P&L table.")
        if fin:
            print(f"       (rows seen by year: {fin['all_expenses']})")
        return

    category = band_by_expenses(fin["expenses"])
    print(f"Latest FY          : {fin['latest_fy']}")
    print(f"Sales (Rs Cr)        : {fin['sales']}")
    print(f"Expenses (Rs Cr)     : {fin['expenses']}   <-- banding metric X")
    print(f"Category (A/B/C/D) : {category}\n")

    print("Expenses by year (verify against Screener P&L 'Expenses' row):")
    for fy in sorted(fin["all_expenses"], reverse=True):
        print(f"   {fy}: {fin['all_expenses'][fy]}")
    print()


if __name__ == "__main__":
    main()
