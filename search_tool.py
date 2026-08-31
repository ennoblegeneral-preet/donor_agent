from tavily import TavilyClient
from urllib.parse import urlparse
import os
import re
import time
import requests
from bs4 import BeautifulSoup
from datetime import date
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from error_utils import classify_error
from redis_cache import get_json, set_json, make_key

load_dotenv()

_active_search_keys = {}
_search_keys_lock = threading.Lock()

def set_search_context(search_keys: dict, username: str = None):
    """Set active search provider and API key."""
    if search_keys:
        with _search_keys_lock:
            _active_search_keys["current"] = search_keys
            if username:
                _active_search_keys[username] = search_keys

def get_search_context() -> dict:
    """Get active search configuration."""
    with _search_keys_lock:
        return _active_search_keys.get("current", {}) or {}

def get_effective_search_config(username: str = None) -> dict:
    """
    Returns effective search provider and API key for the current context/user,
    or falls back to active session / database / .env configuration.
    """
    ctx = get_search_context()
    user_provider = (ctx.get("provider") or "").lower().strip()
    user_api_key = (ctx.get("api_key") or "").strip()

    if user_provider and user_api_key:
        return {
            "provider": user_provider,
            "api_key": user_api_key,
            "source": "user",
            "configured": True,
        }

    # If not in cache, check username or active Flask session
    active_username = username
    if not active_username:
        try:
            from flask import has_request_context, session
            if has_request_context() and session.get("username"):
                active_username = session.get("username")
        except Exception:
            pass

    if active_username:
        try:
            from db import get_user_search_keys
            db_keys = get_user_search_keys(active_username) or {}
            db_provider = (db_keys.get("provider") or "serper").lower().strip()
            db_api_key = (db_keys.get("api_key") or "").strip()
            if db_provider and db_api_key:
                set_search_context(db_keys, username=active_username)
                return {
                    "provider": db_provider,
                    "api_key": db_api_key,
                    "source": "user_db",
                    "configured": True,
                }
        except Exception:
            pass

    # Fallback to system .env configuration
    env_provider = os.getenv("SEARCH_PROVIDER", "").lower().strip()
    serper_key = (os.getenv("SERPER_API_KEY") or os.getenv("serper_api_key") or "").strip().strip('"').strip("'")
    tavily_key = (os.getenv("TAVILY_API_KEY") or os.getenv("tavily_api_key") or "").strip().strip('"').strip("'")

    if not env_provider:
        env_provider = "serper" if serper_key else ("tavily" if tavily_key else "")

    if env_provider == "serper" and serper_key:
        return {
            "provider": "serper",
            "api_key": serper_key,
            "source": "env",
            "configured": True,
        }
    elif env_provider == "tavily" and tavily_key:
        return {
            "provider": "tavily",
            "api_key": tavily_key,
            "source": "env",
            "configured": True,
        }

    return {
        "provider": user_provider or env_provider or "serper",
        "api_key": "",
        "source": "none",
        "configured": False,
    }



tavily_api_key = os.getenv("TAVILY_API_KEY")
tavily = TavilyClient(api_key=tavily_api_key) if tavily_api_key else None
_tavily_semaphore = threading.Semaphore(3)  # Rate limit to ~3 concurrent Tavily calls

INDIA_DOMAINS = [".in", "india.", "bharat.", "gov.in", "mca.gov.in"]
INDIA_SOURCE_DOMAINS = ["csrbox.org", "linkedin.com", "zaubacorp.com", "tofler.in"]
NON_INDIA_INDICATORS = ["usa.", "uk.", "america.", "global", "worldwide", "en.wikipedia"]

def _is_india_result(url: str, content: str = "") -> bool:
    """Check if a URL/content is India-specific"""
    url_lower = url.lower()
    content_lower = content.lower()

    for domain in INDIA_DOMAINS:
        if domain in url_lower:
            return True

    for domain in INDIA_SOURCE_DOMAINS:
        if domain in url_lower:
            return True

    for indicator in NON_INDIA_INDICATORS:
        if indicator in url_lower:
            return False

    india_keywords = [
        "india", "indian", "mumbai", "delhi", "bengaluru", "hyderabad", "pune",
        "gurgaon", "noida", "chennai", "kolkata", "ahmedabad", "gurugram",
        "maharashtra", "karnataka", "gujarat", "tamil nadu", "telangana", "haryana", "uttar pradesh"
    ]
    if any(keyword in content_lower for keyword in india_keywords):
        return True

    return False


def _recent_indian_fiscal_years(count: int = 3) -> list:
    """Indian FY runs Apr-Mar, e.g. 'FY25' = Apr 2024-Mar 2025."""
    today = date.today()
    latest_completed_fy_end_year = today.year if today.month >= 4 else today.year - 1
    return [f"FY{str(latest_completed_fy_end_year - i)[-2:]}" for i in range(count)]


def _serper_search(query: str, max_results: int = 5, api_key: str = None) -> dict:
    """Execute search using Google Serper API."""
    key = api_key or (os.getenv("SERPER_API_KEY") or os.getenv("serper_api_key") or "").strip().strip('"').strip("'")
    if not key:
        raise ValueError("No Serper API key found. Please add your Serper API key in Settings.")

    url = "https://google.serper.dev/search"
    headers = {
        "X-API-KEY": key,
        "Content-Type": "application/json"
    }
    payload = {
        "q": query,
        "num": max_results,
        "gl": "in",
        "hl": "en"
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    results = []
    kg = data.get("knowledgeGraph") or {}
    if kg.get("description"):
        desc = f"{kg.get('title', '')}: {kg.get('description', '')}"
        results.append({
            "title": kg.get("title", ""),
            "url": kg.get("website") or kg.get("descriptionUrl") or "",
            "content": desc,
            "raw_content": desc
        })

    for item in data.get("organic", []):
        snippet = item.get("snippet", "")
        sitelinks = " ".join([s.get("snippet", "") for s in item.get("sitelinks", []) if s.get("snippet")])
        full_content = f"{snippet} {sitelinks}".strip()
        results.append({
            "title": item.get("title", ""),
            "url": item.get("link", ""),
            "content": full_content,
            "raw_content": full_content
        })

    return {"results": results}


def _tavily_search_with_limit(query: str, max_results: int = 5, include_raw_content: bool = False, retries: int = 5, api_key: str = None) -> dict:
    """Wrapper around Tavily search with rate-limit semaphore and automatic retry."""
    key = api_key or tavily_api_key
    if not key:
        raise ValueError("No Tavily API key found. Please add your Tavily API key in Settings.")
    client = TavilyClient(api_key=key) if (key and key != tavily_api_key) else tavily
    if not client:
        client = TavilyClient(api_key=key)

    with _tavily_semaphore:
        for attempt in range(retries):
            try:
                return client.search(query, max_results=max_results, include_raw_content=include_raw_content)
            except Exception as e:
                if "429" in str(e) or "rate" in str(e).lower() or "excessive" in str(e).lower():
                    if attempt < retries - 1:
                        time.sleep(4 * (2 ** attempt))
                        continue
                raise e


def _execute_search(query: str, max_results: int = 5, include_raw_content: bool = True) -> dict:
    """Routes search to active provider (serper or tavily) using active user or env credentials."""
    cfg = get_effective_search_config()
    provider = cfg.get("provider", "serper")
    api_key = cfg.get("api_key", "")

    if not cfg.get("configured") or not api_key:
        raise ValueError("No Web Search API key added. Please add a Tavily or Serper API key in Settings.")

    cache_key = make_key("search", provider, query, max_results, include_raw_content)
    cached = get_json(cache_key)
    if cached is not None:
        print(f"[Search Cache] Hit: {query}")
        return cached

    if provider == "serper":
        result = _serper_search(query, max_results=max_results, api_key=api_key)
    else:
        result = _tavily_search_with_limit(query, max_results=max_results, include_raw_content=include_raw_content, api_key=api_key)

    set_json(cache_key, result)
    return result



def _search_stage_contact(stage: dict):
    """Execute a single contact search stage. Returns (results, error_or_None)."""
    try:
        results = _execute_search(stage["query"], max_results=5, include_raw_content=True)
        collected = []
        for r in results.get("results", []):
            url = r.get("url", "")
            text = r.get("raw_content") or r.get("content", "")

            if not _is_india_result(url, text):
                continue

            if text and len(text) > 150:
                collected.append({
                    "priority": stage["priority"],
                    "source_type": stage["source_type"],
                    "url": url,
                    "text": text[:4000]
                })
        return collected, None
    except Exception as e:
        print(f"[Search Warning] {stage['source_type']} search failed: {e}")
        return [], classify_error(e)


def search_contact_sources(company_name: str, website: str = None) -> dict:
    """ Target query design for contacts: company name + CSR Head / Sustainability / Head HR / CEO / HR / MD / Founder """
    domain = urlparse(website).netloc.replace("www.", "") if website else None
    collected = []

    search_stages = [
        # 1. Direct Company Website Search
        {
            "priority": 1,
            "source_type": "company_website",
            "query": f'site:{domain} ("CSR Head" OR "Sustainability" OR "Head HR" OR "HR Head" OR "CEO" OR "HR" OR contact OR leadership)' if domain else f'"{company_name}" ("CSR Head" OR "Sustainability" OR "Head HR" OR "HR Head" OR "CEO" OR "HR" OR contact)',
        },
        # 2. LinkedIn Leadership Profile Search
        {
            "priority": 2,
            "source_type": "LinkedIn",
            "query": f'site:linkedin.com/in "{company_name}" ("CSR Head" OR "Head of CSR" OR "CSR Lead" OR "Sustainability" OR "Sustainability Head" OR "Head HR" OR "HR Head" OR "CHRO" OR "CEO" OR "Managing Director" OR "HR" OR "Founder")',
        },
        # 3. Directories, MCA Filings & CSRBOX Contact Search
        {
            "priority": 3,
            "source_type": "Registry & Annual Report",
            "query": f'"{company_name}" ("CSR Head" OR "Sustainability" OR "Head HR" OR "HR Head" OR "CEO" OR "HR" OR "Managing Director" OR "CSR Committee") ("contact" OR email OR Zaubacorp OR Tofler OR "annual report" OR site:csrbox.org)',
        },
    ]

    errors = []
    with ThreadPoolExecutor(max_workers=len(search_stages)) as executor:
        futures = {executor.submit(_search_stage_contact, stage): stage for stage in search_stages}
        for future in as_completed(futures):
            results, error = future.result()
            collected.extend(results)
            if error:
                errors.append(error)

    result = {"sources": collected}
    if not collected and errors:
        result["error"] = errors[0]
    return result


def search_person_linkedin(person_name: str, company_name: str) -> str:
    """Best-effort LinkedIn profile search for a named person."""
    if not person_name or not person_name.strip():
        return None
    query = f'"{person_name}" "{company_name}" site:linkedin.com/in'
    try:
        results = _execute_search(query, max_results=3, include_raw_content=False)
        for r in results.get("results", []):
            url = r.get("url", "")
            if "linkedin.com/in/" in url.lower():
                return url
        return None
    except Exception as e:
        print(f"[Search Warning] LinkedIn search failed for '{person_name}': {e}")
        return None


def _search_stage_csr(stage: dict, company_name: str, seen_urls: set):
    """Execute a single CSR search stage. Returns (results, error_or_None)."""
    print(f"[Search] {company_name}: {stage['query']}")
    try:
        results = _execute_search(stage["query"], max_results=5, include_raw_content=True)
        collected = []
        for r in results.get("results", []):
            url = r.get("url", "")
            text = r.get("raw_content") or r.get("content", "")

            if not _is_india_result(url, text):
                continue

            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            collected.append(r)
        return collected, None
    except Exception as e:
        print(f"[Search Warning] CSR info search failed: {e}")
        return [], classify_error(e)


def search_company_csr_info(company_name: str, website: str = None):
    """ Multi-stage search covering CSR data fields """
    domain = urlparse(website).netloc.replace("www.", "") if website else None

    csr_stages = [
        {
            "priority": 1,
            "source_type": "Financial / CSR",
            "query": f'"{company_name}" CSR ("total CSR expenditure" OR "CSR spend" OR "CSR obligation" OR "unspent amount" OR "amount spent") (crore OR lakh)  ("FY25" OR "FY 2024-25" OR "FY24" OR "FY 2023-24" OR "FY23") {" ".join(_recent_indian_fiscal_years())} ("annual report" OR BRSR OR site:csrbox.org)',
        },
        {
            "priority": 1,
            "source_type": "CSR Overview",
            "query": f'"{company_name}" CSR (education OR school OR foundation OR "CSR project") (STEM OR infrastructure OR Anganwadi OR scholarship)',
        },
        # 3. Implementation Partners & Operational Geography
        {
            "priority": 2,
            "source_type": "Partners & Geography",
            "query": f'"{company_name}" CSR ("implementation partner" OR "implementation agency" OR "NGO partner" OR "executing agency" OR "foundation partner" OR "collaborating NGO") (education OR school OR NGO OR foundation OR beneficiaries)',
        },
    ]

    collected = []
    seen_urls = set()
    errors = []

    with ThreadPoolExecutor(max_workers=len(csr_stages)) as executor:
        futures = {executor.submit(_search_stage_csr, stage, company_name, seen_urls): stage for stage in csr_stages}
        for future in as_completed(futures):
            results, error = future.result()
            collected.extend(results)
            if error:
                errors.append(error)

    if not collected and website:
        try:
            fallback = _execute_search(f"{company_name} CSR report education spend 2024", max_results=3, include_raw_content=True)
            collected = fallback.get("results", [])
        except Exception as e:
            errors.append(classify_error(e))

    result = {"results": collected}
    if not collected and errors:
        result["error"] = errors[0]
    return result


SCREENER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


def _normalize_screener_company_name(company_name: str) -> str:
    """Remove periods that commonly appear in legal suffixes (for example LTD.)."""
    return re.sub(r"\s+", " ", (company_name or "").replace(".", "")).strip()


def find_company_on_screener(company_name: str):
    try:
        screener_query = _normalize_screener_company_name(company_name)
        response = requests.get(
            "https://www.screener.in/api/company/search/",
            params={"q": screener_query},
            headers=SCREENER_HEADERS,
            timeout=10
        )
        response.raise_for_status()
        results = response.json()

        if not results:
            print(f"[Screener] '{company_name}' Screener pe nahi mila")
            return None, None

        top = results[0]
        screener_url = "https://www.screener.in" + top["url"]
        print(f"[Screener] Company mila: {top.get('name')} -> {screener_url}")
        return {"name": top.get("name"), "url": screener_url}, None

    except Exception as e:
        print(f"[Screener Error] Company search failed: {e}")
        return None, classify_error(e)


_RELIABLE_REPORT_HOSTS = ("bseindia.com", "nseindia.com")


def _host_reliability_rank(href: str) -> int:
    host = urlparse(href).netloc.lower()
    return 0 if any(reliable in host for reliable in _RELIABLE_REPORT_HOSTS) else 1


def get_annual_report_pdfs_by_year(screener_url: str) -> dict:
    try:
        response = requests.get(screener_url, headers=SCREENER_HEADERS, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")

        documents_section = soup.find("section", id="documents")
        if not documents_section:
            print("[Screener] Documents section nahi mila is page pe")
            return {}

        annual_reports_box = documents_section.find(class_="annual-reports") or documents_section

        year_map = {}
        for link in annual_reports_box.find_all("a", href=True):
            href = link["href"].strip()
            if not href.lower().endswith(".pdf"):
                continue
            label = link.get_text(" ", strip=True)
            match = re.search(r"(20\d{2})", label)
            if not match:
                continue
            year = int(match.group(1))
            year_map.setdefault(year, []).append(href)

        for year, hrefs in year_map.items():
            year_map[year] = sorted(hrefs, key=_host_reliability_rank)

        return year_map

    except Exception as e:
        print(f"[Screener Error] Annual report year-map scrape failed: {e}")
        return {}


def get_annual_report_pdf_from_screener(screener_url: str) -> str:
    year_map = get_annual_report_pdfs_by_year(screener_url)
    if not year_map:
        print("[Screener] Is page par koi annual report PDF nahi mila")
        return None
    latest_year = max(year_map)
    href = year_map[latest_year][0]
    print(f"[Screener] Annual report PDF mila (FY{latest_year}): {href}")
    return href


def search_annual_report_pdf_via_screener(company_name: str) -> dict:
    company, error = find_company_on_screener(company_name)
    if not company:
        return {"error": error} if error else None

    year_map = get_annual_report_pdfs_by_year(company["url"])

    latest_year = max(year_map) if year_map else None
    latest_candidates = year_map.get(latest_year, []) if latest_year is not None else []
    pdf_url = latest_candidates[0] if latest_candidates else None

    previous_year = None
    previous_candidates = []
    if latest_year is not None:
        for yr in sorted(year_map, reverse=True):
            if yr < latest_year:
                previous_year = yr
                previous_candidates = year_map[yr]
                break
    previous_year_pdf_url = previous_candidates[0] if previous_candidates else None

    return {
        "screener_url": company["url"],
        "pdf_url": pdf_url,
        "pdf_url_candidates": latest_candidates,
        "latest_year": latest_year,
        "previous_year": previous_year,
        "previous_year_pdf_url": previous_year_pdf_url,
        "previous_year_pdf_url_candidates": previous_candidates,
        "pdf_url_by_year": year_map,
    }


def search_education_spend_data(company_name: str, website: str = None) -> dict:
    """[DISABLED to save 4 search credits per company]"""
    return {"education_sources": []}

# def search_education_spend_data(company_name: str, website: str = None) -> dict:
#     domain = urlparse(website).netloc.replace("www.", "") if website else None
#     education_stages = [
#         {"query": f"{company_name} India CSR education spend percentage breakup BRSR", "source_type": "Education Spend Percentage"},
#         {"query": f"{company_name} India CSR education allocation budget schools colleges scholarship", "source_type": "Education Program Budget"},
#         {"query": f"site:{domain} India CSR education spend annual report" if domain else f"{company_name} India CSR education spend annual report", "source_type": "Company Website Education"},
#         {"query": f"{company_name} India CSR education focus area percentage allocation BRSR CSR report", "source_type": "BRSR Education Metrics"},
#     ]
#     collected = []
#     seen_urls = set()
#     with ThreadPoolExecutor(max_workers=4) as executor:
#         futures = {executor.submit(_search_stage_education, stage, company_name, seen_urls): stage for stage in education_stages}
#         for future in as_completed(futures):
#             collected.extend(future.result())
#     return {"education_sources": collected}

EDUCATION_FIELD_QUERIES = {
    "csr_stem_education": [
        '{company} CSR (STEM OR "science lab" OR "computer lab" OR "digital learning" OR robotics OR coding OR "Atal Tinkering Lab" OR AI )',
    ],
    "csr_school_infra_transformation": [
        '{company} CSR ("school infrastructure" OR "school playground equipments" OR "classroom renovation" OR sanitation OR "drinking water" OR "smart classroom" OR "school building" OR "library")',
    ],
    "csr_holistic_transformation": [
        '{company} CSR ("school transformation" OR "whole school" OR "comprehensive school" OR "school adoption" OR "integrated school" OR "holistic school")',
    ],
    "csr_anganwadi_transformation": [
        '{company} CSR (Anganwadi OR "early childhood" OR Balwadi OR preschool OR "child nutrition" OR "maternal child")',
    ],
    "csr_quality_education": [
        '{company} CSR ("quality education" OR "learning outcomes" OR "teacher training" OR literacy OR numeracy OR scholarships OR "remedial education")',
    ],
    "csr_model_school_transformation": [
        '{company} CSR ("model school" OR "adarsh vidyalaya" OR "PM SHRI" OR "school upgradation" OR "cluster schools" OR "government school upgrade" OR "flagship school")',
    ],
    "csr_education_validation": [
        '{company} CSR education "annual report" (BRSR OR "amount spent" OR "implementation partner" OR beneficiaries)',
    ],
}

def search_education_fields(company_name: str, website: str = None) -> dict:
    domain = urlparse(website).netloc.replace("www.", "") if website else ""

    def search_one_field(field):
        sources = []
        seen_urls = set()
        errors = []
        templates = EDUCATION_FIELD_QUERIES[field]
        for attempt, template in enumerate(templates, start=1):
            query = template.format(company=company_name)
            if domain:
                query = f"{query} site:{domain}" if attempt == len(templates) else query
            print(f"[Education Search {attempt}/{len(templates)}] {field}: {query}")
            try:
                result = _execute_search(query, max_results=5, include_raw_content=True)
                for item in result.get("results", []):
                    url = item.get("url", "")
                    text = item.get("raw_content") or item.get("content", "")
                    if not url or url in seen_urls or not _is_india_result(url, text):
                        continue
                    seen_urls.add(url)
                    sources.append({
                        "url": url,
                        "title": item.get("title", ""),
                        "text": text[:7000],
                        "query": query,
                        "attempt": attempt,
                    })
            except Exception as exc:
                print(f"[Search Warning] Education field {field} failed: {exc}")
                errors.append(classify_error(exc))
        return field, {
            "sources": sources,
            "attempts": len(templates),
            "sources_checked": len(sources),
            "errors": errors,
        }

    output = {}
    with ThreadPoolExecutor(max_workers=len(EDUCATION_FIELD_QUERIES)) as executor:
        futures = [executor.submit(search_one_field, field) for field in EDUCATION_FIELD_QUERIES]
        for future in as_completed(futures):
            field, details = future.result()
            output[field] = details
    return output


def _search_stage_education(stage: dict, company_name: str, seen_urls: set) -> list:
    print(f"[Search] Education - {company_name}: {stage['query']}")
    try:
        results = _execute_search(stage["query"], max_results=5, include_raw_content=True)
        collected = []
        for r in results.get("results", []):
            url = r.get("url", "")
            text = r.get("raw_content") or r.get("content", "")

            if not _is_india_result(url, text):
                continue

            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            collected.append({
                "source_type": stage["source_type"],
                "url": url,
                "text": text[:5000]
            })
        return collected
    except Exception as e:
        print(f"[Search Warning] Education search failed: {e}")
        return []


def search_annual_report_pdf(company_name: str, website: str = None) -> str:
    try:
        domain = urlparse(website).netloc.replace("www.", "") if website else None

        query = f"{company_name} annual report filetype:pdf site:.in" if not domain else f"site:{domain} annual report filetype:pdf"

        results = _execute_search(query, max_results=3, include_raw_content=True)

        for r in results.get("results", []):
            url = r.get("url", "").lower()
            if url.endswith(".pdf") and ("annual" in url or "report" in url):
                print(f"[Search] Annual report PDF mila: {url}")
                return url

        return None

    except Exception as e:
        print(f"[Search Error] PDF search failed: {e}")
        return None


def _parse_screener_number(text: str):
    cleaned = text.replace(",", "").strip()
    if not cleaned or cleaned == "-":
        return None
    try:
        return float(cleaned) if "." in cleaned else int(cleaned)
    except ValueError:
        return None


def _column_header_to_fy_label(header: str) -> str:
    parts = header.strip().split()
    if len(parts) == 2 and parts[1].isdigit():
        return f"FY{parts[1][-2:]}"
    return header.strip()


def _extract_screener_table_row(table, row_label: str) -> dict:
    rows = table.find_all("tr")
    if not rows:
        return {}

    header_cells = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])]
    fy_labels = [_column_header_to_fy_label(h) for h in header_cells[1:]]

    for row in rows[1:]:
        cells = [c.get_text(strip=True) for c in row.find_all(["th", "td"])]
        if not cells:
            continue
        label = cells[0].replace("+", "").strip()
        if label.lower() == row_label.lower():
            values = cells[1:]
            return {
                fy: _parse_screener_number(val)
                for fy, val in zip(fy_labels, values)
                if fy != "TTM"
            }

    return {}


def get_financials_from_screener(screener_url: str, years: int = 3) -> dict:
    try:
        response = requests.get(screener_url, headers=SCREENER_HEADERS, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")

        turnover, pbt, net_profit, net_worth = {}, {}, {}, {}

        pnl_section = soup.find("section", id="profit-loss")
        if pnl_section:
            pnl_table = pnl_section.find("table")
            if pnl_table:
                turnover = _extract_screener_table_row(pnl_table, "Sales")
                pbt = _extract_screener_table_row(pnl_table, "Profit before tax")
                net_profit = _extract_screener_table_row(pnl_table, "Net Profit")

        bs_section = soup.find("section", id="balance-sheet")
        if bs_section:
            bs_table = bs_section.find("table")
            if bs_table:
                equity = _extract_screener_table_row(bs_table, "Equity Capital")
                reserves = _extract_screener_table_row(bs_table, "Reserves")
                for fy in set(equity) | set(reserves):
                    e, r = equity.get(fy), reserves.get(fy)
                    if e is not None and r is not None:
                        net_worth[fy] = e + r

        all_years = sorted(
            set(turnover) | set(pbt) | set(net_profit) | set(net_worth),
            reverse=True
        )
        recent_years = all_years[:years]

        def _pick(data):
            return {fy: data.get(fy) for fy in recent_years}

        return {
            "turnover": _pick(turnover),
            "pbt": _pick(pbt),
            "net_profit": _pick(net_profit),
            "net_worth": _pick(net_worth),
            "fiscal_years": recent_years,
        }

    except Exception as e:
        print(f"[Screener Error] Financials scrape failed: {e}")
        return {
            "turnover": {}, "pbt": {}, "net_profit": {}, "net_worth": {}, "fiscal_years": [],
            "error": classify_error(e),
        }


def search_unlisted_company_financials(company_name: str, website: str = None) -> dict:
    domain = urlparse(website).netloc.replace("www.", "") if website else None
    queries = [
        f"site:{domain} annual report financial statements revenue profit" if domain else f"{company_name} India annual report revenue profit",
        f"{company_name} India financial performance turnover net profit FY24 OR FY25",
        f"{company_name} annual report filetype:pdf site:.in"
    ]
    collected_results = []
    for query in queries:
        res = _execute_search(query, max_results=2, include_raw_content=True)
        for r in res.get("results", []):
            if r.get("url") and (r.get("raw_content") or r.get("content")):
                collected_results.append(r)

    return {
        "results": collected_results
    }

