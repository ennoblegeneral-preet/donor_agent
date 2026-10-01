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
import contextvars
from error_utils import classify_error
from redis_cache import get_json, set_json, make_key
from pdf_utils import _headers_for

load_dotenv()

_active_search_keys = {}
_search_keys_lock = threading.Lock()

# Web-search credit counter for the current pipeline run. Every real search
# call (contact search, CSR search, education search, etc.) actually happens
# inside a NESTED ThreadPoolExecutor worker thread for concurrency - a plain
# threading.local() tracker would be invisible to those worker threads (each
# gets its own separate thread-local storage), so nothing would ever get
# counted. contextvars.ContextVar, combined with submit_with_context() below
# to explicitly propagate the context into every executor.submit() call,
# makes the same tracker dict visible across all of a pipeline run's worker
# threads while still keeping concurrent company runs isolated from each
# other (each run's start_search_tracking() call creates its own dict in its
# own context). A lock guards the increments since multiple stage threads can
# now genuinely race on the same shared dict.
_search_tracker = contextvars.ContextVar("search_tracker", default=None)
_search_tracker_lock = threading.Lock()


def start_search_tracking():
    """Call at the start of a pipeline run to begin counting web-search API calls."""
    _search_tracker.set({"serper_calls": 0, "tavily_calls": 0, "cache_hits": 0, "total_calls": 0})


def get_tracked_search_usage():
    """Returns the accumulated search-usage dict for the current context, or
    None if start_search_tracking() was never called on it."""
    return _search_tracker.get()


def _record_search_call(provider: str):
    tracker = _search_tracker.get()
    if tracker is None:
        return
    key = f"{provider}_calls"
    with _search_tracker_lock:
        tracker[key] = tracker.get(key, 0) + 1
        tracker["total_calls"] += 1


def _record_search_cache_hit():
    tracker = _search_tracker.get()
    if tracker is None:
        return
    with _search_tracker_lock:
        tracker["cache_hits"] += 1


def submit_with_context(executor, fn, *args, **kwargs):
    """executor.submit() that propagates the CURRENT context (including the
    search-credit tracker ContextVar) into the worker thread. Plain
    executor.submit(fn, *args) does NOT inherit the submitting thread's
    context - each worker thread starts with a fresh, empty context - so any
    ContextVar-based tracking set up before the submit() call would otherwise
    silently vanish inside every concurrent search stage."""
    ctx = contextvars.copy_context()
    return executor.submit(ctx.run, fn, *args, **kwargs)


def set_search_context(search_keys: dict, username: str = None):
    """Set active search provider and API key."""
    if search_keys:
        sanitized = {
            "provider": (search_keys.get("provider") or "serper").lower().strip(),
            "api_key": (search_keys.get("api_key") or "").strip().strip('"').strip("'"),
            # "auto" (default) = deep-scrape ON for Serper, OFF for Tavily.
            # "on"/"off" = manual override regardless of provider.
            "deep_scrape": (search_keys.get("deep_scrape") or "auto").lower().strip(),
        }
        with _search_keys_lock:
            _active_search_keys["current"] = sanitized
            if username:
                _active_search_keys[username] = sanitized


def get_search_context(username: str = None) -> dict:
    """Get active search configuration."""
    with _search_keys_lock:
        if username and username in _active_search_keys:
            return _active_search_keys[username]
        return _active_search_keys.get("current", {}) or {}


def get_effective_search_config(username: str = None) -> dict:
    """
    Returns effective search provider and API key for the current context/user,
    or falls back to active session / database / .env configuration.
    """
    # 1. Check active username from arg or Flask session
    active_username = username
    if not active_username:
        try:
            from flask import has_request_context, session
            if has_request_context() and session.get("username"):
                active_username = session.get("username")
        except Exception:
            pass

    # 2. Check user DB / context for active user
    if active_username:
        with _search_keys_lock:
            user_ctx = _active_search_keys.get(active_username)
        if user_ctx and user_ctx.get("api_key"):
            return {
                "provider": user_ctx.get("provider", "serper"),
                "api_key": user_ctx.get("api_key", ""),
                "deep_scrape": user_ctx.get("deep_scrape", "auto"),
                "source": "user_context",
                "configured": True,
            }
        try:
            from db import get_user_search_keys
            db_keys = get_user_search_keys(active_username) or {}
            db_provider = (db_keys.get("provider") or "serper").lower().strip()
            db_api_key = (db_keys.get("api_key") or "").strip().strip('"').strip("'")
            if db_provider and db_api_key:
                set_search_context(db_keys, username=active_username)
                return {
                    "provider": db_provider,
                    "api_key": db_api_key,
                    "deep_scrape": (db_keys.get("deep_scrape") or "auto").lower().strip(),
                    "source": "user_db",
                    "configured": True,
                }
        except Exception:
            pass

    # 3. Check generic current context
    ctx = get_search_context()
    user_provider = (ctx.get("provider") or "").lower().strip()
    user_api_key = (ctx.get("api_key") or "").strip().strip('"').strip("'")

    if user_provider and user_api_key:
        return {
            "provider": user_provider,
            "api_key": user_api_key,
            "deep_scrape": (ctx.get("deep_scrape") or "auto").lower().strip(),
            "source": "user",
            "configured": True,
        }

    # 4. Fallback to system .env configuration
    env_deep_scrape = (os.getenv("DEEP_SCRAPE") or "auto").lower().strip()
    env_provider = os.getenv("SEARCH_PROVIDER", "").lower().strip()
    serper_key = (os.getenv("SERPER_API_KEY") or os.getenv("serper_api_key") or "").strip().strip('"').strip("'")
    tavily_key = (os.getenv("TAVILY_API_KEY") or os.getenv("tavily_api_key") or "").strip().strip('"').strip("'")

    if not env_provider:
        env_provider = "serper" if serper_key else ("tavily" if tavily_key else "")

    if env_provider == "serper" and serper_key:
        return {
            "provider": "serper",
            "api_key": serper_key,
            "deep_scrape": env_deep_scrape,
            "source": "env",
            "configured": True,
        }
    elif env_provider == "tavily" and tavily_key:
        return {
            "provider": "tavily",
            "api_key": tavily_key,
            "deep_scrape": env_deep_scrape,
            "source": "env",
            "configured": True,
        }

    return {
        "provider": user_provider or env_provider or "serper",
        "api_key": "",
        "deep_scrape": env_deep_scrape,
        "source": "none",
        "configured": False,
    }



def should_deep_scrape(username: str = None) -> bool:
    """Decide whether Option A (fetch each result's full page via BeautifulSoup)
    is active for the current run.

    - deep_scrape == "on"  -> always ON  (manual override)
    - deep_scrape == "off" -> always OFF (manual override)
    - deep_scrape == "auto" (default) -> ON for Serper (whose results are just
      Google snippets and would otherwise miss in-page data), OFF for Tavily
      (which already returns full raw_content, so re-fetching is wasteful).
    """
    cfg = get_effective_search_config(username)
    mode = (cfg.get("deep_scrape") or "auto").lower().strip()
    if mode == "on":
        return True
    if mode == "off":
        return False
    return cfg.get("provider", "serper") == "serper"


# Tags that never carry article/body content - stripped before text extraction.
_JUNK_HTML_TAGS = [
    "script", "style", "noscript", "nav", "footer", "header", "aside",
    "form", "iframe", "svg", "button", "input", "select", "template",
]


def extract_html_text(url: str, max_chars: int = 8000) -> str:
    """Option A core: download a normal web page and return its cleaned visible
    text (boilerplate tags removed). Cached and best-effort - returns "" on any
    failure (blocked bot, timeout, JS-only page) so the caller can safely fall
    back to the Google snippet it already has. PDFs are NOT handled here - those
    go through research_agent.extract_pdf_text()."""
    if not url:
        return ""
    cache_key = make_key("html-text", url, max_chars)
    cached = get_json(cache_key)
    if isinstance(cached, dict) and isinstance(cached.get("text"), str):
        return cached["text"]

    text = ""
    try:
        resp = requests.get(url, headers=_headers_for(url), timeout=12)
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "").lower()
        if "html" not in ctype and "text" not in ctype:
            set_json(cache_key, {"text": ""})
            return ""
        soup = BeautifulSoup(resp.content, "html.parser")
        for tag in soup(_JUNK_HTML_TAGS):
            tag.decompose()
        raw = soup.get_text(separator=" ")
        text = " ".join(raw.split())[:max_chars]
    except Exception as e:
        print(f"[DeepScrape] Skipped {url}: {e}")
        text = ""

    set_json(cache_key, {"text": text})
    return text


def _deep_scrape_enrich(items, get_url, get_text, set_text, max_chars=8000, max_workers=6):
    """For each item, fetch its full page text and replace the stored snippet
    when the fetch yields more content. No-op (returns items unchanged) unless
    deep scrape is active. Non-HTML/PDF urls and failed fetches keep the
    original snippet, so this can only add data, never lose it."""
    if not items or not should_deep_scrape():
        return items

    def work(item):
        url = get_url(item) or ""
        if not url or url.lower().endswith(".pdf"):
            return
        full = extract_html_text(url, max_chars=max_chars)
        if full and len(full) > len(get_text(item) or ""):
            set_text(item, full)

    with ThreadPoolExecutor(max_workers=min(max_workers, len(items))) as ex:
        futures = [submit_with_context(ex, work, it) for it in items]
        for _ in as_completed(futures):
            pass
    return items


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


def _serper_search(query: str, max_results: int = 5, api_key: str = None, retries: int = 3) -> dict:
    """Execute search using Google Serper API with automatic retries."""
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

    last_exc = None
    for attempt in range(retries):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=25)
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
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exc = exc
            if attempt < retries - 1:
                print(f"[Serper Warning] Network glitch on attempt {attempt+1}/{retries}, retrying in {attempt+1}s...")
                time.sleep(1 * (attempt + 1))
            else:
                raise last_exc
        except Exception as exc:
            raise exc

    if last_exc:
        raise last_exc
    return {"results": []}


def _tavily_search_with_limit(query: str, max_results: int = 5, include_raw_content: bool = False, retries: int = 5, api_key: str = None) -> dict:
    """Wrapper around Tavily search with rate-limit semaphore and automatic retry."""
    key = (api_key or tavily_api_key or "").strip().strip('"').strip("'")
    if not key:
        raise ValueError("No Tavily API key found. Please add your Tavily API key in Settings.")
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
        _record_search_cache_hit()
        return cached

    if provider == "serper":
        result = _serper_search(query, max_results=max_results, api_key=api_key)
    else:
        result = _tavily_search_with_limit(query, max_results=max_results, include_raw_content=include_raw_content, api_key=api_key)
    _record_search_call(provider)

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


def _run_contact_stages(stages: list) -> tuple:
    """Run a set of contact search stages concurrently. Returns (collected, errors)."""
    collected = []
    errors = []
    with ThreadPoolExecutor(max_workers=len(stages)) as executor:
        futures = {submit_with_context(executor, _search_stage_contact, stage): stage for stage in stages}
        for future in as_completed(futures):
            results, error = future.result()
            collected.extend(results)
            if error:
                errors.append(error)
    return collected, errors


def search_contact_sources(company_name: str, website: str = None) -> dict:
    """Two-phase contact discovery: search for CSR / CSR-related people FIRST, and
    only fall back to HR / HR Head / leadership queries if the CSR phase finds no
    usable sources. This keeps HR contacts as a genuine fallback rather than
    surfacing them alongside (and sometimes ahead of) the preferred CSR contact."""
    domain = urlparse(website).netloc.replace("www.", "") if website else None

    # --- Phase 1: CSR / CSR-related roles ONLY ---
    csr_roles = '"CSR Head" OR "Head of CSR" OR "CSR Lead" OR "CSR Manager" OR "CSR Officer" OR "CSR Director" OR "Foundation Head" OR "Head Foundation" OR "Sustainability" OR "Sustainability Head" OR "ESG Head" OR "CSR Committee" OR "Social Consultant"'
    csr_stages = [
        {
            "priority": 1,
            "source_type": "company_website",
            "query": f'site:{domain} ({csr_roles})' if domain else f'"{company_name}" ({csr_roles})',
        },
        {
            "priority": 2,
            "source_type": "LinkedIn",
            "query": f'site:linkedin.com/in "{company_name}" ({csr_roles})',
        },
        {
            "priority": 3,
            "source_type": "Registry & Annual Report",
            "query": f'"{company_name}" ({csr_roles}) ("contact" OR email OR Zaubacorp OR Tofler OR "annual report" OR site:csrbox.org)',
        },
    ]

    collected, errors = _run_contact_stages(csr_stages)

    # --- Phase 2: HR / leadership FALLBACK (only if no CSR contact source found) ---
    if not collected:
        hr_roles = '"Head HR" OR "HR Head" OR "HR Manager", "Senior HR" OR '
        hr_stages = [
            {
                "priority": 4,
                "source_type": "company_website",
                "query": f'site:{domain} ({hr_roles} OR contact )' if domain else f'"{company_name}" ({hr_roles} OR contact)',
            },
            {
                "priority": 5,
                "source_type": "LinkedIn",
                "query": f'site:linkedin.com/in "{company_name}" ({hr_roles})',
            },
            {
                "priority": 6,
                "source_type": "Registry & Annual Report",
                "query": f'"{company_name}" ({hr_roles}) ("contact" OR email OR Zaubacorp OR Tofler OR "annual report")',
            },
        ]
        hr_collected, hr_errors = _run_contact_stages(hr_stages)
        collected.extend(hr_collected)
        errors.extend(hr_errors)

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
        # 1. Total CSR Expenditure & Previous Year Spend
        {
            "priority": 1,
            "source_type": "Financial / CSR Previous Year",
            "query": f'"{company_name}" CSR ("total CSR expenditure" OR "CSR spend" OR "CSR obligation" OR "actual spend" OR "amount spent") (crore OR lakh) ("FY25" OR "FY 2024-25" OR "FY24" OR "FY 2023-24" OR "FY23") {" ".join(_recent_indian_fiscal_years())} ("annual report" OR BRSR OR site:csrbox.org OR "National CSR Portal")',
        },
        # 2. Education Previous Year CSR Spend & Sector Breakdown
        # {
        #     "priority": 1,
        #     "source_type": "Education CSR Spend",
        #     "query": f'"{company_name}" CSR ("education spend" OR "spent on education" OR "education budget" OR "education sector" OR "promotion of education" OR "Schedule VII" OR "school education") (crore OR lakh OR "FY25" OR "FY24" OR "FY23" OR "FY 2023-24" OR "FY 2024-25" OR "annual report")',
        # },
        # # 3. Past 3 Years CSR Spend History & Trend
        # {
        #     "priority": 1,
        #     "source_type": "Past 3 Years CSR Spend",
        #     "query": f'"{company_name}" CSR ("past 3 years" OR "last 3 years" OR "three financial years" OR "3-year average" OR "average net profit" OR "CSR trend" OR "FY 2022-23" OR "FY 2023-24" OR "FY 2024-25" OR "FY23" OR "FY24" OR "FY25") (expenditure OR spend OR obligation OR crore)',
        # },
        # 4. CSR Overview & Initiatives
        {
            "priority": 2,
            "source_type": "CSR Overview",
            "query": f'"{company_name}" CSR (education OR school OR foundation OR "CSR project") (STEM OR infrastructure OR Anganwadi OR scholarship)',
        },
        # 5. Implementation Partners & Operational Geography
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
        futures = {submit_with_context(executor, _search_stage_csr, stage, company_name, seen_urls): stage for stage in csr_stages}
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


def search_company_geography(company_name: str, website: str = None) -> dict:
    """Fallback-only search fired when the main CSR/contact/partner searches didn't
    surface any text naming WHERE a company's CSR programs actually run (as opposed
    to just its HQ). Those searches only find program-location info incidentally,
    so this queries for beneficiary states/districts directly instead of relying on
    a lucky mention - kept as a separate opt-in call (not a always-on stage) since
    it costs another search credit per company."""
    query = (
        f'"{company_name}" CSR ("beneficiary states" OR "beneficiary districts" OR "program locations" OR '
        f'"operational geography" OR "states covered" OR "implemented in" OR "CSR interventions in" OR '
        f'"reach across" OR district) (education OR school OR community OR beneficiaries OR villages)'
    )
    try:
        results = _execute_search(query, max_results=5, include_raw_content=True)
    except Exception as e:
        print(f"[Search Warning] Geography fallback search failed for '{company_name}': {e}")
        return {"sources": [], "error": classify_error(e)}

    sources = []
    seen_urls = set()
    for r in results.get("results", []):
        url = r.get("url", "")
        text = r.get("raw_content") or r.get("content", "")
        if not url or url in seen_urls:
            continue
        if not _is_india_result(url, text):
            continue
        if text and len(text) > 150:
            seen_urls.add(url)
            sources.append({
                "priority": 1,
                "source_type": "Geography Fallback",
                "url": url,
                "text": text[:4000],
            })
    return {"sources": sources}


SCREENER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


def _normalize_screener_company_name(company_name: str) -> str:
    """Remove periods that commonly appear in legal suffixes (for example LTD.)."""
    return re.sub(r"\s+", " ", (company_name or "").replace(".", "")).strip()


_LEGAL_SUFFIXES_REGEX = re.compile(
    r"\b(pvt\.?|private|ltd\.?|limited|llp|inc\.?|corp\.?|corporation|holdings?|enterprises?|industries|technologies|tech|solutions|india|group|international)\b",
    re.IGNORECASE,
)

def _clean_base_company_name(name: str) -> str:
    """Extract clean base brand name for search fallback."""
    cleaned = re.sub(r"[\.,\-&/()'\"]+", " ", name or "")
    cleaned = _LEGAL_SUFFIXES_REGEX.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned



def find_company_on_screener(company_name: str):
    """
    Multi-stage Screener resolution with automatic typo/spelling correction:
    1. Direct search on Screener API
    2. Suffix-stripped base brand name on Screener API
    3. Google/Serper spell-corrected search for site:screener.in/company/
    4. First-two tokens query fallback
    """
    if not company_name or not company_name.strip():
        return None, None

    # Step 1: Direct Screener API Search
    try:
        norm_query = _normalize_screener_company_name(company_name)
        response = requests.get(
            "https://www.screener.in/api/company/search/",
            params={"q": norm_query},
            headers=SCREENER_HEADERS,
            timeout=10,
        )
        if response.ok:
            results = response.json()
            if results:
                top = results[0]
                screener_url = "https://www.screener.in" + top["url"]
                print(f"[Screener] Direct match mila: {top.get('name')} -> {screener_url}")
                return {"name": top.get("name"), "url": screener_url}, None
    except Exception as e:
        print(f"[Screener Warning] Direct search failed: {e}")

    # Step 2: Suffix-stripped base brand name
    base_name = _clean_base_company_name(company_name)
    if base_name and len(base_name) >= 3 and base_name.lower() != company_name.lower():
        try:
            print(f"[Screener] Trying base brand name: '{base_name}'")
            response = requests.get(
                "https://www.screener.in/api/company/search/",
                params={"q": base_name},
                headers=SCREENER_HEADERS,
                timeout=10,
            )
            if response.ok:
                results = response.json()
                if results:
                    top = results[0]
                    screener_url = "https://www.screener.in" + top["url"]
                    print(f"[Screener] Base name match mila: {top.get('name')} -> {screener_url}")
                    return {"name": top.get("name"), "url": screener_url}, None
        except Exception as e:
            print(f"[Screener Warning] Base name search failed: {e}")

    # Step 3: Google/Serper Spell-Corrected Search Fallback (Handles typos like 'Dalmya Bharat' or 'Infosis')
    try:
        print(f"[Screener] Searching Google/Serper with spell-correction: '{company_name}'")
        search_query = f"{company_name} screener.in company"
        google_results = _execute_search(search_query, max_results=3, include_raw_content=False)
        for r in google_results.get("results", []):
            url = r.get("url", "")
            match = re.search(r"https://www\.screener\.in/company/([A-Za-z0-9_-]+)/?", url, re.IGNORECASE)
            if match:
                canonical_url = f"https://www.screener.in/company/{match.group(1).upper()}/consolidated/"
                title = r.get("title", "")
                resolved_name = title.split("-")[0].split("share price")[0].split("|")[0].strip() or company_name
                print(f"[Screener] Google fuzzy/spell-corrected match mila: '{resolved_name}' -> {canonical_url}")
                return {"name": resolved_name, "url": canonical_url}, None
    except Exception as e:
        print(f"[Screener Warning] Google fallback search failed: {e}")

    # Step 4: First two words fallback
    words = [w for w in re.findall(r"\w+", company_name) if len(w) > 2]
    if len(words) >= 2:
        short_query = " ".join(words[:2])
        try:
            print(f"[Screener] Trying short 2-token query: '{short_query}'")
            response = requests.get(
                "https://www.screener.in/api/company/search/",
                params={"q": short_query},
                headers=SCREENER_HEADERS,
                timeout=10,
            )
            if response.ok:
                results = response.json()
                if results:
                    top = results[0]
                    screener_url = "https://www.screener.in" + top["url"]
                    print(f"[Screener] Short query match mila: {top.get('name')} -> {screener_url}")
                    return {"name": top.get("name"), "url": screener_url}, None
        except Exception as e:
            print(f"[Screener Warning] Short query search failed: {e}")

    print(f"[Screener] '{company_name}' Screener pe kisi bhi method se nahi mila")
    return None, None



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
        '{company} CSR (STEM OR "science lab" OR "computer lab" OR "digital learning" OR "robotics" OR "coding" OR "Atal Tinkering Lab" OR "AI")',
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
    "csr_education_spend_history": [
        '{company} CSR ("education spend" OR "spent on education" OR "education budget" OR "promotion of education") (crore OR lakh OR "FY25" OR "FY24" OR "FY23" OR "2024" OR "2023")',
    ],
    "csr_past_3yr_spend": [
        '{company} CSR ("average CSR" OR "3 years CSR" OR "past three years" OR "CSR spend trend" OR "FY 2023-24" OR "FY 2024-25") (expenditure OR obligation OR spent)',
    ],
    "csr_education_validation": [
        '{company} CSR education "annual report" (BRSR OR "amount spent" OR "implementation partner" OR beneficiaries)',
    ],
}

# Generic corporate suffixes / filler words that don't identify a specific
# company - stripped before matching so "Biocon Ltd" still matches a page that
# only says "Biocon".
_COMPANY_STOPWORDS = {
    "ltd", "limited", "pvt", "private", "inc", "llp", "plc", "corp",
    "corporation", "co", "company", "companies", "foundation", "trust",
    "industries", "india", "indian", "enterprises", "group", "holdings",
    "and", "the",
}


def _company_identifiers(company_name: str) -> list:
    """Distinctive lowercase brand tokens that identify this company, with
    generic corporate suffixes removed."""
    cleaned = re.sub(r"[^a-z0-9 ]", " ", (company_name or "").lower())
    return [t for t in cleaned.split() if len(t) >= 3 and t not in _COMPANY_STOPWORDS]


def _mentions_company(company_name: str, *texts) -> bool:
    """True if any distinctive company token appears in the given text(s).

    Used to drop generic third-party/vendor pages (e.g. an 'Atal Tinkering Lab'
    vendor's marketing page) that match the CSR keywords but never actually name
    the company being researched - which was letting fields like STEM be marked
    "Yes" on evidence that has nothing to do with the company."""
    identifiers = _company_identifiers(company_name)
    if not identifiers:
        return True  # can't determine reliably -> don't over-filter
    haystack = " ".join(t for t in texts if t).lower()
    return any(token in haystack for token in identifiers)


def search_education_fields(company_name: str, website: str = None) -> dict:
    domain = urlparse(website).netloc.replace("www.", "") if website else ""

    def _run_query(query, attempt, sources, seen_urls, errors):
        """Execute one education query and append India + company-relevant hits
        to `sources`. Returns the number of new sources added."""
        added = 0
        try:
            result = _execute_search(query, max_results=10, include_raw_content=True)
            for item in result.get("results", []):
                url = item.get("url", "")
                text = item.get("raw_content") or item.get("content", "")
                if not url or url in seen_urls or not _is_india_result(url, text):
                    continue
                # Drop pages that never name the company - generic vendor /
                # keyword-match pages that would otherwise be mistaken for the
                # company's own CSR evidence.
                if not _mentions_company(company_name, item.get("title", ""), text):
                    print(f"[Education Search] Dropped off-topic (no company mention): {url}")
                    continue
                seen_urls.add(url)
                sources.append({
                    "url": url,
                    "title": item.get("title", ""),
                    "text": text[:8000],
                    "query": query,
                    "attempt": attempt,
                })
                added += 1
        except Exception as exc:
            print(f"[Search Warning] Education field {field} failed: {exc}")
            errors.append(classify_error(exc))
        return added

    def search_one_field(field):
        sources = []
        seen_urls = set()
        errors = []
        templates = EDUCATION_FIELD_QUERIES[field]

        # Primary pass: open-web search (no site: restriction) so BRSR filings,
        # csrbox.org, annual reports and news are all reachable.
        attempt = 0
        for attempt, template in enumerate(templates, start=1):
            query = template.format(company=company_name)
            print(f"[Education Search {attempt}/{len(templates)}] {field}: {query}")
            _run_query(query, attempt, sources, seen_urls, errors)

        # Fallback pass: only if the open web found nothing, retry scoped to the
        # company's own website to catch anything indexed only there.
        if not sources and domain:
            attempt += 1
            query = f"{templates[0].format(company=company_name)} site:{domain}"
            print(f"[Education Search {attempt} (site fallback)] {field}: {query}")
            _run_query(query, attempt, sources, seen_urls, errors)

        return field, {
            "sources": sources,
            "attempts": attempt,
            "sources_checked": len(sources),
            "errors": errors,
        }

    output = {}
    with ThreadPoolExecutor(max_workers=len(EDUCATION_FIELD_QUERIES)) as executor:
        futures = [submit_with_context(executor, search_one_field, field) for field in EDUCATION_FIELD_QUERIES]
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