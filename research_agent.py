import json
import os
import requests
import pdfplumber
import io
from search_tool import (
    search_company_csr_info,
    search_contact_sources,
    search_education_fields,
    search_company_geography,
    search_annual_report_pdf_via_screener,
    search_annual_report_pdf,
    get_financials_from_screener,
    get_annual_report_pdfs_by_year,
    search_education_spend_data,
    search_unlisted_company_financials,
    search_person_linkedin,
)
from extraction_tool import extract_research_with_contact, extract_education_fields, extract_geography_fields
from models import CompanyResearch
from financial_extractor import (
    extract_csr_data,
    check_prospect_criteria,
    calculate_csr_budget,
    calculate_net_profit_csr_budget,
    calculate_education_spend_percentage,
    extract_unlisted_financial_data,
)
from pdf_utils import extract_csr_section_text, _headers_for
from error_utils import no_data_error
from redis_cache import get_json, set_json, make_key


def extract_pdf_text(pdf_url: str) -> str:
    cache_key = make_key("pdf-text", pdf_url)
    cached = get_json(cache_key)
    if isinstance(cached, dict) and isinstance(cached.get("text"), str):
        print(f"[PDF Cache] Hit: {pdf_url}")
        return cached["text"]
    try:
        response = requests.get(pdf_url, headers=_headers_for(pdf_url), timeout=15, stream=True)
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "")
        if "pdf" not in content_type.lower() and not pdf_url.lower().endswith(".pdf"):
            raise ValueError(f"Response Content-Type is not PDF: {content_type}")

        # Limit to first 10MB to protect memory
        max_bytes = 10 * 1024 * 1024
        chunks = []
        size = 0
        for chunk in response.iter_content(chunk_size=128 * 1024):
            chunks.append(chunk)
            size += len(chunk)
            if size >= max_bytes:
                break
        raw_bytes = b"".join(chunks)

        text = ""
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(raw_bytes))
            for page in reader.pages[:15]:
                text += page.extract_text() or ""
        except Exception:
            with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
                for page in pdf.pages[:10]:
                    text += page.extract_text() or ""

        set_json(cache_key, {"text": text})
        return text
    except Exception as e:
        print(f"[PDF Error] PDF extraction failed: {e}")
        return ""


from db import create_company, update_company, get_company, get_user_search_keys
from audit_logger import log_action
from search_tool import set_search_context

from concurrent.futures import ThreadPoolExecutor

def research_company(company_id: str, company_name: str, website: str = None):
    # Ensure search context is loaded from company creator if available
    try:
        company_doc = get_company(company_id)
        if company_doc and company_doc.get("created_by"):
            user_keys = get_user_search_keys(company_doc.get("created_by"))
            if user_keys:
                set_search_context(user_keys, username=company_doc.get("created_by"))
    except Exception:
        pass

    # Multi-stage search: Run contact search and CSR info search concurrently to save execution time
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_contact = executor.submit(search_contact_sources, company_name, website)
        future_csr = executor.submit(search_company_csr_info, company_name, website)
        
        sources_data = future_contact.result()
        csr_info = future_csr.result()



    sources = sources_data.get("sources", [])
    search_errors = [sources_data["error"]] if sources_data.get("error") else []

    if csr_info and csr_info.get("error"):
        search_errors.append(csr_info["error"])
    if csr_info and csr_info.get("results"):
        for item in csr_info["results"]:
            url = item.get("url", "")
            text = item.get("raw_content") or item.get("content", "")
            if url and text and len(text) > 100:
                if url.lower().endswith(".pdf"):
                    pdf_text = extract_pdf_text(url)
                    if pdf_text:
                        text = pdf_text
                sources.append({
                    "priority": 1,
                    "source_type": "CSR Report / Web",
                    "url": url,
                    "text": text[:6000]
                })

    if not sources:
        # Errors se pata chalta hai ki search genuinely khaali thi ya API/network/DB
        # fail hui - dono cases mein pehle "Not Found" dikhta tha, ab actual reason save hota hai.
        cause = search_errors[0] if search_errors else no_data_error()
        print(f"[Error] No search sources found for {company_name}: {cause['type']}")
        update_company(company_id, {"status": "failed_research", "last_error": cause})
        log_action(company_id, "research_failed", "ResearchAgent", details=cause["message"])
        return None

    # extract_research_with_contact() truncates the combined text before sending it to the
    # LLM, so put the highest-priority (richest CSR/financial) sources first - otherwise
    # chatty low-value LinkedIn/Media sources can crowd out the CSR Report/Annual Report
    # content the extraction prompt actually needs.
    sources.sort(key=lambda s: s.get("priority", 99))

    print(f"[ResearchAgent] Gathered {len(sources)} sources for {company_name}")

    research, llm_error = extract_research_with_contact(company_name, sources)

    # Dedicated education pass. Keep its audit evidence outside research_json,
    # and preserve an already verified value if a later run is sparse or fails.
    education_evidence = {}
    education_error = None
    try:
        education_search = search_education_fields(company_name, website)
        education_evidence, education_error = extract_education_fields(
            company_name, education_search
        )
        if not education_error:
            previous = get_company(company_id) or {}
            previous_research = previous.get("research_json") or {}
            merged = research.model_dump()
            education_fields = (
                "csr_stem_education",
                "csr_school_infra_transformation",
                "csr_holistic_transformation",
                "csr_anganwadi_transformation",
                "csr_quality_education",
                "csr_model_school_transformation",
            )
            missing_values = {"", "not found", "not publicly available", "none", "n/a", "na"}
            for field in education_fields:
                new_value = education_evidence.get(field, {}).get("value", "Not Found")
                old_value = previous_research.get(field, merged.get(field, "Not Found"))
                new_status = education_evidence.get(field, {}).get("status")
                if new_status == "found" or (
                    new_status == "exhausted"
                    and str(old_value).strip().lower() in missing_values
                ):
                    merged[field] = new_value
                else:
                    merged[field] = old_value
            research = CompanyResearch(**merged)
    except Exception as exc:
        education_error = {"type": "education_search_failed", "message": str(exc)}
        print(f"[Education Research Warning] Dedicated education pass failed: {exc}")

    # Dedicated geography fallback pass. The main CSR/contact/partner searches only
    # surface program-location info incidentally, so when program_district_state
    # came back empty, fire one targeted follow-up query instead of leaving the
    # priority-geography scoring override (scoring_agent.py) with nothing to match.
    missing_geo_values = {"", "not found", "not publicly available", "none", "n/a", "na"}
    if (research.program_district_state or "").strip().lower() in missing_geo_values:
        try:
            geo_search = search_company_geography(company_name, website)
            geo_fields, geo_error = extract_geography_fields(company_name, geo_search.get("sources", []))
            if not geo_error and geo_fields:
                merged = research.model_dump()
                for field in ("city", "state", "program_district_state", "geographical_priority"):
                    new_value = geo_fields.get(field)
                    if new_value and str(new_value).strip().lower() not in missing_geo_values:
                        merged[field] = new_value
                research = CompanyResearch(**merged)
        except Exception as exc:
            print(f"[Geography Research Warning] Dedicated geography fallback failed: {exc}")

    source_urls = "; ".join([s["url"] for s in sources[:5] if s.get("url")])

    # Save to MongoDB. Agar LLM extraction API hi fail hui thi (sources mile the,
    # par unhe parse nahi kar paye), to "researched" ke saath last_error bhi save
    # karte hain - taaki sparse/"Not Found" fields ka asli reason pata chale.
    update_fields = {
        "research_json": research.model_dump(),
        "status": "researched",
        "education_fitment_evidence": education_evidence,
        "education_fitment_error": education_error,
    }
    if llm_error:
        update_fields["last_error"] = llm_error
    else:
        update_fields["last_error"] = None
    update_company(company_id, update_fields)
    log_action(
        company_id, "research_completed", "ResearchAgent",
        source=source_urls,
        details=llm_error["message"] if llm_error else None,
    )

    return research


def research_company_with_contact(company_id: str, company_name: str, website: str = None):
    search_result = search_contact_sources(company_name, website)

    if not search_result["sources"]:
        print(f"❌ No sources found for {company_name}")
        return None

    print(f"[Debug] Found {len(search_result['sources'])} sources for {company_name}")

    research, _llm_error = extract_research_with_contact(company_name, search_result["sources"])

    update_company(company_id, {
        "research_json": research.model_dump(),
        "status": "researched"
    })

    return research


def research_company_with_financials(company_id: str, company_name: str, website: str = None):
    """
    Screener se company dhundo, uske structured P&L/Balance Sheet tables se
    turnover/PBT/net worth/net profit nikalo (accurate, no LLM guessing),
    aur annual report PDF se CSR-specific qualitative data (focus areas,
    partners, etc.) LLM se extract karo. Unlisted companies ke liye web/PDF fallback.
    """
    try:
        company_doc = get_company(company_id)
        if company_doc and company_doc.get("created_by"):
            user_keys = get_user_search_keys(company_doc.get("created_by"))
            if user_keys:
                set_search_context(user_keys)
    except Exception:
        pass

    print(f"\n[🔍 Financial Research] {company_name} ke liye start...")

    # Step 1: Screener pe company dhundo
    print(f"[Step 1] Screener pe {company_name} dhundh rahe hain...")
    screener_result = search_annual_report_pdf_via_screener(company_name)

    screener_url = screener_result.get("screener_url") if screener_result else None
    report_url = screener_result.get("pdf_url") if screener_result else None

    previous_year = screener_result.get("previous_year") if screener_result else None
    previous_year_candidates = (screener_result.get("previous_year_pdf_url_candidates") if screener_result else None) or []
    latest_year_candidates = (screener_result.get("pdf_url_candidates") if screener_result else None) or []
    latest_year = screener_result.get("latest_year") if screener_result else None

    # CSR extraction tries the LATEST report first - it may already carry the final,
    # audited CSR annexure for the most recent FY - and only falls back to the
    # previous year's report if the latest one has no extractable CSR section
    # (not yet indexed, download blocked, or genuinely no CSR annexure pages found).
    csr_report_year_candidates = (
        [(latest_year, u) for u in latest_year_candidates]
        + [(previous_year, u) for u in previous_year_candidates]
    )
    if not csr_report_year_candidates and report_url:
        csr_report_year_candidates = [(None, report_url)]
    csr_report_candidates = [u for _, u in csr_report_year_candidates]
    csr_report_year = csr_report_year_candidates[0][0] if csr_report_year_candidates else None

    if not screener_url:
        print(f"[Unlisted Fallback] {company_name} Screener pe nahi mila. Web & PDF search for current year financials starting...")
        unlisted_search = search_unlisted_company_financials(company_name, website)
        results = unlisted_search.get("results", [])

        combined_text = ""
        pdf_url_found = None
        for r in results:
            url = r.get("url", "")
            if url.lower().endswith(".pdf"):
                pdf_text = extract_pdf_text(url)
                if pdf_text:
                    combined_text += f"\n{pdf_text}"
                    pdf_url_found = url
            else:
                combined_text += f"\n{r.get('raw_content') or r.get('content', '')}"

        if pdf_url_found and not csr_report_candidates:
            csr_report_candidates = [pdf_url_found]
            csr_report_year_candidates = [(None, pdf_url_found)]

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
        # Step 2: Screener ke structured tables se turnover/PBT/net worth nikalo
        print(f"[Step 2] Screener se financial numbers nikal rahe hain...")
        financial_data = get_financials_from_screener(screener_url, years=4)

        if not financial_data.get("fiscal_years"):
            financials_error = financial_data.get("error")
            status = "screener_error" if financials_error else "no_financials_found"
            cause = financials_error["message"] if financials_error else "No financial tables found on Screener."
            print(f"[❌] Screener par financial tables nahi mile: {cause}")
            update_company(company_id, {
                "financial_research_status": status,
                "financial_last_error": financials_error,
            })
            log_action(company_id, "financials_scrape_failed", "ResearchAgent", details=cause)
            return None

        print(f"[✅] Financial data mila for years: {financial_data['fiscal_years']}")

    # Step 2b: CSR budget calculate karo (current year exclude, pichle 3 saal ka avg PBT * 2%)
    csr_budget = calculate_csr_budget(financial_data)
    if csr_budget.get("csr_budget_2pct") is not None:
        print(f"[✅] CSR Budget: ₹{csr_budget['csr_budget_2pct']} Cr (2% of avg PBT {csr_budget['calc_years']})")
    else:
        print(f"[⚠️] CSR Budget calculate nahi ho saka: {csr_budget.get('note')}")

    # Step 2c: same calculation Net Profit ke average se bhi (PBT wala tarika, bas metric alag)
    csr_budget_net_profit = calculate_net_profit_csr_budget(financial_data)
    if csr_budget_net_profit.get("csr_budget_2pct_net_profit") is not None:
        print(f"[✅] CSR Budget (Net Profit basis): ₹{csr_budget_net_profit['csr_budget_2pct_net_profit']} Cr (2% of avg Net Profit {csr_budget_net_profit['calc_years']})")
    else:
        print(f"[⚠️] CSR Budget (Net Profit basis) calculate nahi ho saka: {csr_budget_net_profit.get('note')}")

    # Step 3: Annual report PDF na mile to general web search fallback.
    # CSR/education ke liye latest-year report pehle try karte hain (see
    # csr_report_year_candidates above); agar wo bhi na mila to general web search.
    if not csr_report_candidates:
        print(f"[Fallback] Screener se PDF nahi mila, general web search try kar rahe hain...")
        fallback_url = search_annual_report_pdf(company_name, website)
        csr_report_candidates = [fallback_url] if fallback_url else []
        csr_report_year_candidates = [(None, fallback_url)] if fallback_url else []
        csr_report_year = None
    report_url = report_url or (csr_report_candidates[0] if csr_report_candidates else None)  # DB mein save karne ke liye

    # Step 4: PDF se CSR data extract karo (optional - financial data ke bina bhi chal sakta hai)
    # Note: CSR Annexure section usually report ke aakhri hisse mein hota hai,
    # isliye pehle N pages ki jagah poore PDF mein CSR-keyword wale pages dhoondhte hain.
    #
    # Multiple candidate sources try karte hain (reliability-ordered - BSE/NSE
    # pehle, company-website copy baad mein): agar ek source WAF/403 se block ho
    # jaaye (jaisa eClerx ke apne domain-hosted PDF ke saath hua tha), to CSR data
    # poora skip karne ki jagah agla candidate try karte hain.
    csr_data = None
    csr_extraction_error = None
    education_spend_data = None
    csr_report_url = None
    committee_members_linkedin = {}
    pdf_text = ""
    if csr_report_year_candidates:
        for candidate_year, candidate_url in csr_report_year_candidates:
            year_label = f"FY{str(candidate_year)[-2:]}" if candidate_year else "latest available"
            print(f"[Step 4] CSR data {year_label} report se nikaal rahe hain ({candidate_url})...")
            pdf_text = extract_csr_section_text(candidate_url)
            if pdf_text:
                csr_report_url = candidate_url
                csr_report_year = candidate_year
                break
            print(f"[⚠️] Is source se PDF extract nahi ho saka, agla candidate source try kar rahe hain (agar hai)...")

        if pdf_text:
            print(f"[✅] CSR section text mila ({csr_report_url}) - {len(pdf_text)} characters")
            print(f"[Step 5] CSR data extract ho raha hai...")
            csr_data, csr_extraction_error = extract_csr_data(pdf_text, company_name)
            print(f"[Debug] Raw CSR data from LLM: {json.dumps(csr_data, indent=2)}")
            if csr_extraction_error:
                print(f"[⚠️] CSR extraction LLM call fail hui: {csr_extraction_error['message']}")

            # Calculate education spend percentage
            education_spend_data = calculate_education_spend_percentage(csr_data)
            print(f"[✅] Education spend data calculated: {json.dumps(education_spend_data, indent=2)}")

            # Step 5b: Best-effort LinkedIn lookup for each named CSR Committee Member -
            # the PDF text has no links, so this is a separate search per name. Name-only
            # matching is fuzzy (common names can return the wrong profile), so this is
            # NOT a verified identity match - just a starting point for manual outreach.
            committee_members = (csr_data or {}).get("committee_members") or []
            if committee_members:
                print(f"[Step 5b] {len(committee_members)} CSR Committee Member(s) ke LinkedIn profiles dhundh rahe hain...")
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = {
                        executor.submit(search_person_linkedin, member, company_name): member
                        for member in committee_members
                    }
                    for future in futures:
                        member = futures[future]
                        try:
                            committee_members_linkedin[member] = future.result()
                        except Exception as e:
                            print(f"[⚠️] LinkedIn lookup failed for {member}: {e}")
                            committee_members_linkedin[member] = None
                found_count = sum(1 for v in committee_members_linkedin.values() if v)
                print(f"[✅] {found_count}/{len(committee_members)} committee member LinkedIn profiles found (unverified matches)")
        else:
            print(f"[⚠️] Saare candidate sources se PDF extract nahi ho saka, CSR data skip ho raha hai")
    else:
        print(f"[⚠️] Annual report PDF nahi mila, CSR data skip ho raha hai")

    # Step 4b: Web search for education spend data (disabled to save 4 search credits per company)
    # edu_search_result = search_education_spend_data(company_name, website)
    # education_sources = edu_search_result.get("education_sources", [])
    education_sources = []

    # Step 6: Prospect criteria check karo
    print(f"[Step 6] Prospect criteria check ho raha hai...")
    is_prospect = check_prospect_criteria(financial_data)

    if is_prospect:
        print(f"[✅] {company_name} = PROSPECT")
    else:
        print(f"[❌] {company_name} = NOT A PROSPECT")

    # Step 7: Database mein save karo
    print(f"[Step 7] Database mein save ho raha hai...")
    update_company(company_id, {
        "screener_url": screener_url,
        "annual_report_url": report_url,
        "csr_report_url": csr_report_url,
        "csr_report_year": csr_report_year,
        "financial_data": financial_data,
        "csr_budget": csr_budget,
        "csr_budget_net_profit": csr_budget_net_profit,
        "csr_data": csr_data,
        "committee_members_linkedin": committee_members_linkedin,
        "education_spend": education_spend_data,
        # "education_sources": education_sources,  # Disabled
        "is_prospect": is_prospect,
        "financial_research_status": "done",
        # Screener/financials khud succeed ho gaye is run mein, isliye purani
        # (pichhle failed run ki) stale error clear karte hain.
        "financial_last_error": None,
        "csr_extraction_error": csr_extraction_error,
    })

    log_action(
        company_id,
        "financial_research_completed",
        "ResearchAgent",
        details=(
            f"Prospect: {is_prospect}, Education spend tracked"
            + (f"; CSR extraction warning: {csr_extraction_error['message']}" if csr_extraction_error else "")
        )
    )

    print(f"[✅ Complete] {company_name} research done")
    return {
        "financial": financial_data,
        "csr_budget": csr_budget,
        "csr_budget_net_profit": csr_budget_net_profit,
        "csr": csr_data,
        "education_spend": education_spend_data,
        "education_sources": education_sources
    }


def list_available_csr_years(company_id: str, max_years: int = 2) -> dict:
    """For the company detail page's 'View Previous Years' CSR Spend' button:
    returns up to `max_years` fiscal years BEFORE the year the main pipeline
    already extracted (company.csr_report_year), using the company's
    already-persisted screener_url (no re-search needed). This is a separate,
    on-demand add-on - it does not touch/replace the main pipeline's own
    current-year CSR extraction (research_company_with_financials)."""
    company = get_company(company_id)
    if not company:
        return {"status": "error", "message": "Company not found"}

    screener_url = company.get("screener_url")
    if not screener_url:
        return {"status": "not_available", "years": [], "message": "No Screener profile on record for this company."}

    year_map = get_annual_report_pdfs_by_year(screener_url)
    current_year = company.get("csr_report_year")
    years = sorted((y for y in year_map if y != current_year), reverse=True)[:max_years]
    return {"status": "success", "years": years}


def get_csr_spend_for_year(company_id: str, year: int) -> dict:
    """On-demand fetch for a SPECIFIC historical year's CSR spend/unspent/education
    figures - independent of the main research pipeline's current-year CSR data.
    Parses that year's own annual report PDF via Screener.

    Cached both in Redis and on the company's own Mongo doc
    (csr_year_history.<year>) since a published annual report's figures never
    change - once fetched for a company, it's free on every future click.
    """
    company = get_company(company_id)
    if not company:
        return {"status": "error", "message": "Company not found"}

    year_key = str(year)
    cached_history = company.get("csr_year_history") or {}
    if year_key in cached_history:
        return {"status": "success", "year": year, "cached": True, **cached_history[year_key]}

    screener_url = company.get("screener_url")
    if not screener_url:
        return {"status": "not_available", "message": "No Screener profile on record for this company."}

    cache_key = make_key("csr-year-data", screener_url, year)
    cached = get_json(cache_key)
    if isinstance(cached, dict):
        update_company(company_id, {f"csr_year_history.{year_key}": cached})
        return {"status": "success", "year": year, "cached": True, **cached}

    year_map = get_annual_report_pdfs_by_year(screener_url)
    candidates = year_map.get(year) or []
    if not candidates:
        return {"status": "not_available", "message": f"No annual report found for {year} on Screener."}

    pdf_text = ""
    used_url = None
    for candidate_url in candidates:
        pdf_text = extract_csr_section_text(candidate_url)
        if pdf_text:
            used_url = candidate_url
            break

    if not pdf_text:
        return {"status": "not_available", "message": "Report found but no extractable CSR section (possibly a scanned/image-only PDF)."}

    csr_data, err = extract_csr_data(pdf_text, company.get("company_name", ""))
    if err:
        return {"status": "error", "message": err.get("message", "CSR extraction failed")}

    education_data = calculate_education_spend_percentage(csr_data) or {}

    result = {
        "pdf_url": used_url,
        "csr_spend": csr_data.get("csr_spend"),
        "csr_spend_year": csr_data.get("csr_spend_year"),
        "csr_unspent_amount": csr_data.get("csr_unspent_amount"),
        "education_spend": education_data.get("current_education_spend"),
        "education_spend_percentage": education_data.get("current_education_percentage"),
    }

    set_json(cache_key, result, ttl_seconds=60 * 60 * 24 * 365)  # published filings never change
    update_company(company_id, {f"csr_year_history.{year_key}": result})

    return {"status": "success", "year": year, "cached": False, **result}


if __name__ == "__main__":
    with open("companies_input.json") as f:
        companies = json.load(f)
        
    os.makedirs("results", exist_ok=True)
    all_results = []

    for c in companies:
        print(f"\n--- Researching: {c['name']} ---")
        # Create or get company ID in MongoDB
        company_id = create_company(c["name"], c.get("website"))
        result = research_company(company_id, c["name"], c.get("website"))
        if result:
            try:
                print(result.model_dump_json(indent=2))
            except UnicodeEncodeError:
                print(result.model_dump_json(indent=2).encode('ascii', errors='replace').decode('ascii'))
            all_results.append(result.model_dump())
        else:
            print("No data found.")

    with open("results/research_output.json", "w") as f:
        json.dump(all_results, f, indent=2)

    print(f"\n[Done] {len(all_results)} companies saved to results/research_output.json")