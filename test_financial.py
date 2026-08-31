import os
import sys
import io
import json

# Windows console (cp1252) crashes on emoji in print() - force UTF-8 stdout/stderr
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from financial_extractor import (
    extract_csr_data,
    check_prospect_criteria,
    calculate_csr_budget,
    calculate_net_profit_csr_budget,
    calculate_education_spend_percentage,
    extract_unlisted_financial_data,
)
from search_tool import (
    search_annual_report_pdf_via_screener,
    search_annual_report_pdf,
    get_financials_from_screener,
    search_education_spend_data,
    search_unlisted_company_financials,
)
# pdf_utils.py db.py import nahi karta, isliye is standalone test script mein
# safely import ho sakta hai (Mongo/DNS unreachable hone par bhi crash nahi hoga)
from pdf_utils import extract_csr_section_text

load_dotenv()


def test_financial_extraction(company_name: str, website: str = None):
    """
    Test financial data extraction for a company:
    Screener se turnover/PBT/net worth (structured tables), PDF se CSR data (LLM)
    """
    print(f"\n{'='*60}")
    print(f"🔍 Testing Financial Extraction for: {company_name}")
    print(f"{'='*60}\n")

    # Step 1: Screener pe company dhundo
    print(f"[Step 1] Screener pe {company_name} dhundh rahe hain...")
    screener_result = search_annual_report_pdf_via_screener(company_name)

    screener_url = screener_result.get("screener_url") if screener_result else None
    report_url = screener_result.get("pdf_url") if screener_result else None

    # CSR/education PREVIOUS completed FY (FY-1) ke report se lete hain - zyada
    # reliable, aur CSR budget logic ke saath consistent. Fallback = latest report.
    csr_report_url = (screener_result.get("previous_year_pdf_url") if screener_result else None) or report_url
    csr_report_year = (
        screener_result.get("previous_year")
        if screener_result and screener_result.get("previous_year_pdf_url")
        else (screener_result.get("latest_year") if screener_result else None)
    )

    if not screener_url:
        print(f"⚠️  {company_name} Screener pe nahi mila (Unlisted Company)")
        print(f"[Unlisted Fallback] Web & PDF search for current year financials starting...")
        unlisted_search = search_unlisted_company_financials(company_name, website)
        results = unlisted_search.get("results", [])

        combined_text = ""
        pdf_url_found = None
        for r in results:
            url = r.get("url", "")
            if url.lower().endswith(".pdf"):
                pdf_text = extract_csr_section_text(url)
                if pdf_text:
                    combined_text += f"\n{pdf_text}"
                    pdf_url_found = url
            else:
                combined_text += f"\n{r.get('raw_content') or r.get('content', '')}"

        if pdf_url_found and not csr_report_url:
            csr_report_url = pdf_url_found

        if combined_text:
            extracted = extract_unlisted_financial_data(combined_text, company_name)
            fy = extracted.get("fiscal_year") or "FY24"
            financial_data = {
                "turnover": {fy: extracted.get("turnover")} if extracted.get("turnover") else {},
                "pbt": {fy: extracted.get("pbt")} if extracted.get("pbt") else {},
                "net_profit": {fy: extracted.get("net_profit")} if extracted.get("net_profit") else {},
                "net_worth": {fy: extracted.get("net_worth")} if extracted.get("net_worth") else {},
                "fiscal_years": [fy] if any([extracted.get("turnover"), extracted.get("pbt"), extracted.get("net_profit")]) else [],
                "is_unlisted": True
            }
        else:
            financial_data = {
                "turnover": {}, "pbt": {}, "net_profit": {}, "net_worth": {}, "fiscal_years": [],
                "is_unlisted": True
            }
    else:
        print(f"✅ Screener profile mila: {screener_url}")
        if screener_result.get("pdf_url_by_year"):
            print(f"   Available report years: {sorted(screener_result['pdf_url_by_year'], reverse=True)}")
        fy_lbl = f"FY{str(csr_report_year)[-2:]}" if csr_report_year else "latest"
        print(f"   CSR/education ke liye {fy_lbl} report use ho raha hai\n")

        # Step 2: Screener ke structured tables se turnover/PBT/net worth nikalo
        print(f"[Step 2] Screener se financial numbers nikal rahe hain...")
        financial_data = get_financials_from_screener(screener_url, years=4)

    if not financial_data.get("fiscal_years"):
        print(f"❌ Screener par financial tables nahi mile")
        return None

    print(f"✅ Financial data mila\n")

    # Step 3: Annual report PDF (CSR data ke liye) - previous year report prefer karte hain
    if not csr_report_url:
        print(f"[Fallback] Screener se PDF nahi mila, general web search try kar rahe hain...")
        csr_report_url = search_annual_report_pdf(company_name, website)
        csr_report_year = None

    csr_data = None
    education_spend_data = None
    education_sources = []

    if csr_report_url:
        fy_lbl = f"FY{str(csr_report_year)[-2:]}" if csr_report_year else "latest available"
        print(f"✅ CSR report ({fy_lbl}): {csr_report_url}")
        print(f"[Step 3] PDF ke CSR-relevant pages dhoondhe ja rahe hain...")
        pdf_text = extract_csr_section_text(csr_report_url)

        if pdf_text:
            print(f"✅ CSR section text mila - {len(pdf_text)} characters")
            print(f"[Step 4] Groq se CSR data extract ho raha hai...")
            csr_data, csr_extraction_error = extract_csr_data(pdf_text, company_name)
            if csr_extraction_error:
                print(f"⚠️  CSR extraction LLM call fail hui: {csr_extraction_error['message']}")

            # Calculate education spend percentage
            if csr_data:
                education_spend_data = calculate_education_spend_percentage(csr_data)
                print(f"✅ Education spend percentage calculated")
        else:
            print(f"⚠️  PDF extract nahi ho saka, CSR data skip")
    else:
        print(f"⚠️  Annual report PDF nahi mila, CSR data skip")

    # Step 3b: Search for education spend data
    print(f"[Step 3b] Education spend breakdown search ja rahe hain...")
    edu_search_result = search_education_spend_data(company_name, website)
    education_sources = edu_search_result.get("education_sources", [])
    if education_sources:
        print(f"✅ {len(education_sources)} education spend sources mile\n")

    # Step 4: Display financial results
    print(f"\n{'='*60}")
    print(f"📊 FINANCIAL DATA (Screener)")
    print(f"{'='*60}\n")

    fiscal_years = financial_data.get("fiscal_years", [])
    turnover = financial_data.get("turnover", {})
    pbt = financial_data.get("pbt", {})
    net_profit = financial_data.get("net_profit", {})
    net_worth = financial_data.get("net_worth", {})

    def _print_row(label, data):
        print(f"{label}:")
        for fy in fiscal_years:
            print(f"   {fy}: {data.get(fy, 'N/A')}")
        print()

    _print_row("💰 ANNUAL TURNOVER (₹ Crore)", turnover)
    _print_row("📈 PBT - PROFIT BEFORE TAX (₹ Crore)", pbt)
    _print_row("💹 NET PROFIT (₹ Crore)", net_profit)
    _print_row("🏦 NET WORTH / EQUITY (₹ Crore)", net_worth)

    # Step 5: Check prospect criteria
    print(f"{'='*60}")
    print(f"✅ PROSPECT CRITERIA CHECK")
    print(f"{'='*60}\n")

    is_prospect = check_prospect_criteria(financial_data)

    print(f"Required Criteria (either one clears it):")
    print(f"  ✓ Turnover >= ₹1,000 Crore")
    print(f"  ✓ Net Worth >= ₹500 Crore AND Net Profit >= ₹5 Crore (both together)\n")

    latest_fy = fiscal_years[0] if fiscal_years else None
    if latest_fy:
        turnover_latest = turnover.get(latest_fy)
        net_worth_latest = net_worth.get(latest_fy)
        net_profit_latest = net_profit.get(latest_fy)

        print(f"Actual Values ({latest_fy}):")
        print(f"  Turnover: {turnover_latest} Crore - {'✅ PASS' if turnover_latest and turnover_latest >= 1000 else '❌ FAIL'}")
        print(f"  Net Worth: {net_worth_latest} Crore - {'✅ PASS' if net_worth_latest and net_worth_latest >= 500 else '❌ FAIL'}")
        print(f"  Net Profit: {net_profit_latest} Crore - {'✅ PASS' if net_profit_latest and net_profit_latest >= 5 else '❌ FAIL'}\n")

    print(f"{'='*60}")
    if is_prospect:
        print(f"🎉 RESULT: ✅ YES - This is a PROSPECT")
    else:
        print(f"⚠️  RESULT: ❌ NO - Does NOT meet criteria")
    print(f"{'='*60}\n")

    # Step 5b: CSR Budget calculation (2% of avg PBT of the 3 years before the current one)
    print(f"{'='*60}")
    print(f"📐 CSR BUDGET CALCULATION")
    print(f"{'='*60}\n")

    csr_budget = calculate_csr_budget(financial_data)

    if csr_budget.get("csr_budget_2pct") is not None:
        print(f"Current year (excluded from average): {csr_budget['excluded_year']}")
        print(f"\nPBT of previous 3 years (used for average):")
        for fy in csr_budget["calc_years"]:
            print(f"   {fy}: ₹{csr_budget['pbt_values'].get(fy)} Crore")
        print(f"\nAverage PBT (3 years): ₹{csr_budget['average_pbt']} Crore")
        print(f"CSR Budget (2% of average PBT): ₹{csr_budget['csr_budget_2pct']} Crore")
    else:
        print(f"⚠️  Could not calculate: {csr_budget.get('note')}")

    print(f"\n{'='*60}\n")

    # Step 5c: CSR Budget calculation (2% of avg Net Profit of the 3 years before the current one)
    print(f"{'='*60}")
    print(f"📐 CSR BUDGET CALCULATION (NET PROFIT BASIS)")
    print(f"{'='*60}\n")

    csr_budget_net_profit = calculate_net_profit_csr_budget(financial_data)

    if csr_budget_net_profit.get("csr_budget_2pct_net_profit") is not None:
        print(f"Current year (excluded from average): {csr_budget_net_profit['excluded_year']}")
        print(f"\nNet Profit of previous 3 years (used for average):")
        for fy in csr_budget_net_profit["calc_years"]:
            print(f"   {fy}: ₹{csr_budget_net_profit['net_profit_values'].get(fy)} Crore")
        print(f"\nAverage Net Profit (3 years): ₹{csr_budget_net_profit['average_net_profit']} Crore")
        print(f"CSR Budget (2% of average Net Profit): ₹{csr_budget_net_profit['csr_budget_2pct_net_profit']} Crore")
    else:
        print(f"⚠️  Could not calculate: {csr_budget_net_profit.get('note')}")

    print(f"\n{'='*60}\n")

    # Step 6: Display CSR data if available
    if csr_data:
        print(f"📋 CSR DATA EXTRACTED:")
        print(f"\n[Debug] Raw CSR data from LLM: {json.dumps(csr_data, indent=2)}\n")

        print(f"\nFocus Areas:")
        for area in (csr_data.get("focus_areas") or []):
            print(f"  • {area}")

        print(f"\nImplementation Partners:")
        for partner in (csr_data.get("implementation_partners") or []):
            print(f"  • {partner}")

        if csr_data.get("beneficiaries"):
            print(f"\nBeneficiaries: {csr_data.get('beneficiaries')}")

        if csr_data.get("csr_spend") is not None:
            csr_year = csr_data.get("csr_spend_year")
            year_label = f" ({csr_year})" if csr_year else " (year not identified)"
            print(f"CSR Spend: ₹{csr_data.get('csr_spend')} Crore{year_label}")

        if csr_data.get("csr_unspent_amount") is not None:
            print(f"CSR Unspent Amount: ₹{csr_data.get('csr_unspent_amount')} Crore")
        else:
            print(f"CSR Unspent Amount: not found in this report")

        csr_spend_history = csr_data.get("csr_spend_history") or {}
        if csr_spend_history:
            print(f"\nPrevious Years' CSR Spend:")
            for fy, amount in csr_spend_history.items():
                print(f"   {fy}: ₹{amount} Crore")
        else:
            print(f"Previous Years' CSR Spend: not found in this report")

    # Step 7: Display education spend data
    if education_spend_data:
        print(f"\n{'='*60}")
        print(f"🎓 EDUCATION SPEND ANALYSIS")
        print(f"{'='*60}\n")

        report_fy = f"FY{str(csr_report_year)[-2:]}" if csr_report_year else "latest available"
        print(f"Source report: {report_fy} annual report")

        current_year = education_spend_data.get("current_year")
        current_edu_spend = education_spend_data.get("current_education_spend")
        current_edu_pct = education_spend_data.get("current_education_percentage")

        print(f"Reported CSR Year: {current_year}")
        if current_edu_spend is not None:
            print(f"Education Spend: ₹{current_edu_spend} Crore")
        else:
            print(f"Education Spend: not disclosed per-sector in this report "
                  f"(company reports only total CSR; use MCA/data.gov.in for education split)")

        if current_edu_pct is not None:
            print(f"Education % of Total CSR: {current_edu_pct}%")
        else:
            print(f"Education % of Total CSR: not calculated")

        previous_breakdown = education_spend_data.get("previous_years_breakdown", {})
        if previous_breakdown:
            print(f"\nEducation Spend Trend (Previous Years):")
            for fy, data in sorted(previous_breakdown.items(), reverse=True):
                edu_spend = data.get("education_spend")
                total_csr = data.get("total_csr_spend")
                edu_pct = data.get("education_percentage")

                pct_str = f" ({edu_pct}%)" if edu_pct is not None else " (% not available)"
                print(f"   {fy}: ₹{edu_spend} Crore out of ₹{total_csr} Crore{pct_str}")
        else:
            print(f"\nEducation Spend Trend: not found for previous years")

        print(f"\n{'='*60}\n")

    # Step 8: Display education sources if available
    if education_sources:
        print(f"{'='*60}")
        print(f"📚 EDUCATION SPEND SOURCES")
        print(f"{'='*60}\n")
        for i, source in enumerate(education_sources[:5], 1):  # Show top 5
            print(f"{i}. {source.get('source_type', 'Unknown')}")
            print(f"   URL: {source.get('url', 'N/A')}")
            print(f"   Text: {source.get('text', 'N/A')[:200]}...\n")

    return {
        "financial": financial_data,
        "csr_budget": csr_budget,
        "csr_budget_net_profit": csr_budget_net_profit,
        "csr": csr_data,
        "education_spend": education_spend_data,
        "education_sources": education_sources,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python test_financial.py 'Company Name'")
        print("  python test_financial.py 'Company Name' 'https://www.website.com'")
        print("\nExample:")
        print("  python test_financial.py 'TCS'")
        print("  python test_financial.py 'Accenture' 'https://www.accenture.com'")
        sys.exit(1)

    company_name = sys.argv[1]
    website = sys.argv[2] if len(sys.argv) > 2 else None

    test_financial_extraction(company_name, website)
