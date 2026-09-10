import json
import os
import re
from dotenv import load_dotenv
from error_utils import classify_error
from llm_service import call_llm

load_dotenv()


CSR_FINANCIAL_KEYWORDS = {
    "csr", "annexure", "schedule vii", "spent", "spend", "unspent", "budget", "obligation",
    "crore", "lakh", "education", "school", "committee", "beneficiaries", "implementing",
    "partner", "program", "project", "percentage", "fy2", "fy 20", "prescribed", "amount",
    "section 135", "average net profit", "set off", "csr policy", "ongoing project",
    "unspent csr", "transfer", "relief fund", "consolidated", "standalone",
    "expenditure", "shortfall", "surplus", "action plan", "csr-1"
}

def filter_csr_annexure_text(text: str, max_chars: int = 15000) -> str:
    """Filters large annual report text to only paragraphs with CSR financial data and tables."""
    if not text or len(text) <= max_chars:
        return text or ""
    paragraphs = [p.strip() for p in text.split("\n") if len(p.strip()) > 10]
    selected = []
    seen = set()
    total_len = 0
    for p in paragraphs:
        p_lower = p.lower()
        if any(k in p_lower for k in CSR_FINANCIAL_KEYWORDS) or any(c.isdigit() for c in p):
            snip = p_lower[:60]
            if snip in seen:
                continue
            seen.add(snip)
            selected.append(p)
            total_len += len(p)
            if total_len >= max_chars:
                break
    if not selected:
        return text[:max_chars]
    return "\n\n".join(selected)


def extract_csr_data(pdf_text: str, company_name: str):
    """
    Annual report PDF text se CSR-specific qualitative data extract karna
    (focus areas, committee, partners, beneficiaries, programs, education spend breakdown).

    Turnover/PBT/Net Profit/Net Worth yahan se NAHI aate - wo Screener ke
    structured Profit & Loss / Balance Sheet tables se seedha nikalte hain
    (search_tool.get_financials_from_screener), kyunki annual report PDFs
    100-300+ pages ke hote hain aur financial statements usually bahut
    peeche hote hain - LLM ko sirf pehle kuch pages dena unreliable hai.

    Returns (data_or_None, error_or_None) - error sirf tab set hota hai jab
    Groq API call hi fail hui (jaise rate limit), taaki caller "genuinely
    kuch nahi mila" aur "extraction service hi fail ho gayi" mein farak kar sake.
    """
    filtered_text = filter_csr_annexure_text(pdf_text, max_chars=15000)

    prompt = f"""
    From the CSR Annexure / Board's Report section of the annual report, extract the
    CSR data for {company_name}:

    1. CSR Focus Areas (list them - e.g. STEAM, Anganwadi, Healthcare, Education, Skill Development, etc.)
    2. CSR Committee Members (names)
    3. Implementation Partners (NGO names)
    4. Number of Beneficiaries
    5. Key CSR Programs
    6. Total CSR Spend (in ₹ Crores, current/latest reported year).
       IMPORTANT:
       - Prefer the CONSOLIDATED CSR spend if both Consolidated and Standalone figures exist.
       - If the amount is given in Lakhs (e.g. 1,357.28 Lakhs or 623.81 Lakhs), convert it to Crores (divide by 100, e.g. 13.57 or 6.24).
       - Include total spent from footnote details if stated (e.g. 'spent a total of `1,357.28 lakhs').
       - If no company-wide total exists anywhere, set csr_spend to null.
    7. CSR Spend Financial Year - the financial year that the csr_spend figure belongs to
       (e.g. "FY24" or "FY 2023-24"). ALWAYS include the "csr_spend_year" key in the JSON output.
    8. CSR Unspent Amount (in ₹ Crores).
       - Look for unspent amounts transferred to the Unspent CSR Account under Section 135(6), or unspent amounts under Section 135(5).
       - If multiple unspent sums are mentioned (e.g., 429.84 Lakhs + 24.75 Lakhs), sum them up and convert to Crores (e.g. 4.55).
       - If the company spent 100% of its CSR obligation and unspent is stated as Nil / 0 / None, return 0 (number 0, NOT null).
       - If the unspent section is genuinely missing or not found, return null.
    9. Previous years' CSR Spend (in ₹ Crores, as many years as appear in the table, e.g. {{"FY24": 13.57, "FY23": 10.2}}).
       If in Lakhs in the report, convert to Crores.
    10. Education Spend (current year, in ₹ Crores) - how much was spent on "Education" within
        the CSR focus-area breakdown table.
    11. Education Spend Percentage (current year) - education spend as a percentage of total CSR spend (e.g. 35.5).
    12. Education Spend History (previous years) - as many years as are available.
        Format: {{"FY24": {{"amount": <crores>, "percentage": <pct of that year's CSR spend>}}, ...}}

    Text:
    {filtered_text}

    Return the answer in JSON format. If data is not found, use null / an empty list /
    an empty object. Fill in only the actual focus areas / names / NGOs found in the text:
    {{
        "focus_areas": ["<text mein mile actual focus area, e.g. Education>", "..."],
        "committee_members": ["<text mein mila actual committee member ka naam>", "..."],
        "implementation_partners": ["<text mein mile actual NGO/partner ka naam>", "..."],
        "beneficiaries": <number or null>,
        "key_programs": ["<text mein mile actual program ka naam>", "..."],
        "csr_spend": <number in Crores or null>,
        "csr_spend_year": <string or null, e.g. "FY24">,
        "csr_unspent_amount": <number in Crores or null>,
        "csr_spend_history": {{"FY24": <number in Crores>, "FY23": <number in Crores>}},
        "education_spend": <number in Crores or null>,
        "education_spend_percentage": <number or null>,
        "education_spend_history": {{"FY24": {{"amount": <crores>, "percentage": <pct>}}, "FY23": {{...}}}}
    }}
    """

    try:
        data = call_llm(prompt, json_mode=True, timeout=60)

        # Safety net: LLM kabhi kabhi "csr_spend_year" key hi chod deta hai
        # (prompt instruction follow nahi karta) - isliye yaha se guarantee karte hain.
        data.setdefault("csr_spend_year", None)
        data.setdefault("education_spend", None)
        data.setdefault("education_spend_percentage", None)
        data.setdefault("education_spend_history", {})

        # Safety net: jin years ka number nahi mila unke liye LLM null value
        # bhej sakta hai instead of key ko omit karne ke - null entries hata dete hain
        # taaki frontend/test output mein "₹None Crore" jaisa kuch na dikhe.
        history = data.get("csr_spend_history") or {}
        data["csr_spend_history"] = {fy: amount for fy, amount in history.items() if amount is not None}

        # Same for education spend history - remove null amount entries but keep percentage as-is
        edu_history = data.get("education_spend_history") or {}
        data["education_spend_history"] = {
            fy: entry for fy, entry in edu_history.items()
            if isinstance(entry, dict) and entry.get("amount") is not None
        }

        return data, None

    except Exception as e:
        print(f"[Error] CSR extraction failed: {e}")
        return None, classify_error(e, context="llm")


def calculate_csr_budget(financial_data: dict) -> dict:
    """
    CSR budget calculation: current (latest completed) FY ko exclude karke,
    usse pehle ke 3 financial years ka PBT average nikalo, uska 2% CSR budget hai.

    Example: agar latest fetched FY = FY26 ("current", is calc mein use nahi hota),
    to FY25 + FY24 + FY23 ka PBT average lo, us average ka 2% = CSR budget.

    Isliye financial_data mein kam se kam 4 fiscal years hone chahiye
    (1 current + 3 previous) - search_tool.get_financials_from_screener()
    ko years=4 se call karo taaki ye kaam kare.

    financial_data ka shape:
    {
        "pbt": {"FY26": ..., "FY25": ..., "FY24": ..., "FY23": ...},
        "fiscal_years": ["FY26", "FY25", "FY24", "FY23"]  # newest first
    }
    """
    fiscal_years = financial_data.get("fiscal_years") or []
    pbt = financial_data.get("pbt") or {}

    if len(fiscal_years) < 4:
        return {
            "excluded_year": fiscal_years[0] if fiscal_years else None,
            "calc_years": [],
            "pbt_values": {},
            "average_pbt": None,
            "csr_budget_2pct": None,
            "note": "Is calculation ke liye kam se kam 4 saal ka data chahiye (1 current + 3 previous)"
        }

    excluded_year = fiscal_years[0]
    calc_years = fiscal_years[1:4]
    pbt_values = {fy: pbt.get(fy) for fy in calc_years}

    valid_values = [v for v in pbt_values.values() if v is not None]
    if len(valid_values) < 3:
        return {
            "excluded_year": excluded_year,
            "calc_years": calc_years,
            "pbt_values": pbt_values,
            "average_pbt": None,
            "csr_budget_2pct": None,
            "note": "In teen saalon ka poora PBT data Screener par nahi mila"
        }

    average_pbt = round(sum(valid_values) / len(valid_values), 2)
    csr_budget_2pct = round(average_pbt * 0.02, 2)

    return {
        "excluded_year": excluded_year,
        "calc_years": calc_years,
        "pbt_values": pbt_values,
        "average_pbt": average_pbt,
        "csr_budget_2pct": csr_budget_2pct,
        "note": None
    }


def calculate_net_profit_csr_budget(financial_data: dict) -> dict:
    """
    calculate_csr_budget() jaisa hi, lekin PBT ki jagah Net Profit ka average
    aur uska 2% nikalta hai (current year exclude, pichle 3 saal ka avg).
    """
    fiscal_years = financial_data.get("fiscal_years") or []
    net_profit = financial_data.get("net_profit") or {}

    if len(fiscal_years) < 4:
        return {
            "excluded_year": fiscal_years[0] if fiscal_years else None,
            "calc_years": [],
            "net_profit_values": {},
            "average_net_profit": None,
            "csr_budget_2pct_net_profit": None,
            "note": "Is calculation ke liye kam se kam 4 saal ka data chahiye (1 current + 3 previous)"
        }

    excluded_year = fiscal_years[0]
    calc_years = fiscal_years[1:4]
    net_profit_values = {fy: net_profit.get(fy) for fy in calc_years}

    valid_values = [v for v in net_profit_values.values() if v is not None]
    if len(valid_values) < 3:
        return {
            "excluded_year": excluded_year,
            "calc_years": calc_years,
            "net_profit_values": net_profit_values,
            "average_net_profit": None,
            "csr_budget_2pct_net_profit": None,
            "note": "In teen saalon ka poora Net Profit data Screener par nahi mila"
        }

    average_net_profit = round(sum(valid_values) / len(valid_values), 2)
    csr_budget_2pct_net_profit = round(average_net_profit * 0.02, 2)

    return {
        "excluded_year": excluded_year,
        "calc_years": calc_years,
        "net_profit_values": net_profit_values,
        "average_net_profit": average_net_profit,
        "csr_budget_2pct_net_profit": csr_budget_2pct_net_profit,
        "note": None
    }


def calculate_education_spend_percentage(csr_data: dict) -> dict:
    """
    CSR data mein se education spend ka percentage calculate karo.

    csr_data ka shape (extract_csr_data() se):
    {
        "csr_spend": <current year total>,
        "csr_spend_year": "FY24",
        "education_spend": <current year education>,
        "education_spend_percentage": <already extracted % or null>,
        "csr_spend_history": {"FY24": ..., "FY23": ..., "FY22": ...},
        "education_spend_history": {
            "FY24": {"amount": ..., "percentage": ...},
            "FY23": {"amount": ..., "percentage": ...}
        }
    }

    Returns education spend percentage data with calculated percentages where missing.
    """
    if not csr_data:
        return {
            "current_year": None,
            "current_education_spend": None,
            "current_education_percentage": None,
            "previous_years_breakdown": {},
            "note": "No CSR data available"
        }

    current_year = csr_data.get("csr_spend_year")
    current_spend = csr_data.get("csr_spend")
    current_edu_spend = csr_data.get("education_spend")
    current_edu_pct = csr_data.get("education_spend_percentage")

    # Calculate current year percentage if not provided
    if current_edu_pct is None and current_edu_spend and current_spend:
        current_edu_pct = round((current_edu_spend / current_spend) * 100, 2)

    # Build previous years breakdown
    previous_breakdown = {}
    edu_history = csr_data.get("education_spend_history") or {}
    csr_history = csr_data.get("csr_spend_history") or {}

    for fy, edu_entry in edu_history.items():
        if not isinstance(edu_entry, dict):
            continue

        edu_amount = edu_entry.get("amount")
        edu_pct = edu_entry.get("percentage")
        csr_amount = csr_history.get(fy)

        # Calculate percentage if not provided but both amounts available
        if edu_pct is None and edu_amount and csr_amount:
            edu_pct = round((edu_amount / csr_amount) * 100, 2)

        previous_breakdown[fy] = {
            "education_spend": edu_amount,
            "total_csr_spend": csr_amount,
            "education_percentage": edu_pct
        }

    return {
        "current_year": current_year,
        "current_education_spend": current_edu_spend,
        "current_education_percentage": current_edu_pct,
        "previous_years_breakdown": previous_breakdown,
        "note": None
    }


def check_prospect_criteria(financial_data: dict) -> bool:
    """
    Check karo ki company prospect hai ya nahi, sabse recent completed
    fiscal year (financial_data['fiscal_years'][0]) ke numbers se.

    Criteria (Companies Act Sec 135 ke mutabik): Turnover >= 1000 Cr OR
    Net Worth >= 500 Cr OR Net Profit >= 5 Cr. Teeno mein se koi bhi ek
    threshold poora ho jaye to company legally CSR obligated hai - is liye
    OR use karte hain (pehle Net Worth+Net Profit AND the, jo law se strict
    tha aur genuinely obligated companies ko chhod deta tha).

    financial_data ka shape (search_tool.get_financials_from_screener se):
    {
        "turnover": {"FY26": ..., "FY25": ..., "FY24": ...},
        "pbt": {...}, "net_profit": {...}, "net_worth": {...},
        "fiscal_years": ["FY26", "FY25", "FY24"]
    }
    """
    try:
        if not financial_data:
            return False

        fiscal_years = financial_data.get("fiscal_years") or []
        if not fiscal_years:
            return False

        latest_fy = fiscal_years[0]

        turnover = (financial_data.get("turnover") or {}).get(latest_fy)
        net_worth = (financial_data.get("net_worth") or {}).get(latest_fy)
        net_profit = (financial_data.get("net_profit") or {}).get(latest_fy)

        return bool(
            (turnover and turnover >= 1000) or
            (net_worth and net_worth >= 500) or
            (net_profit and net_profit >= 5)
        )

    except Exception as e:
        print(f"[Error] Criteria check failed: {e}")
        return False
def extract_unlisted_financial_data(text: str, company_name: str) -> dict:
    """
    Extracts single-year financial numbers (Turnover, PBT, Net Profit, Net Worth)
    from scraped text or annual report PDFs for unlisted companies.
    """
    prompt = f"""You are a financial data extractor. Extract the LATEST / CURRENT financial year metrics for "{company_name}" from the text below:
    
    Look for:
    1. Financial Year (e.g., "FY26" or "FY25" or "2025-26")
    2. Turnover / Revenue / Total Income (in Crores INR)
    3. Profit Before Tax (PBT) (in Crores INR)
    4. Net Profit / Profit After Tax (PAT) (in Crores INR)
    5. Net Worth / Equity Capital + Reserves (in Crores INR)

    Text:
    {text[:12000]}

    Return ONLY a single valid JSON object. Do not include comments or trailing commas. If a field is unknown/not found, set it to null:
    {{
        "fiscal_year": "FY26",
        "turnover": 450.5,
        "pbt": 35.0,
        "net_profit": 25.0,
        "net_worth": 200.0
    }}
    """
    try:
        return call_llm(prompt, json_mode=True, timeout=60)
    except Exception as e:
        print(f"[Error] Unlisted financial extraction failed: {e}")
        return {}
    except Exception as e:
        print(f"[Error] Unlisted financial extraction failed: {e}")
        return {}

