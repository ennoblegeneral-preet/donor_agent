from flask import Flask, render_template, request, redirect, url_for, jsonify, send_file, session, abort
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from logger import logger
import csv
import io
import json
import os
import threading
import uuid
from urllib.parse import urlparse
from datetime import datetime, timezone
from bson import ObjectId
from concurrent.futures import ThreadPoolExecutor, TimeoutError

from db import (
    get_all_companies, get_company, update_company, create_company, delete_company, delete_companies,
    create_user, get_user_by_username, get_user_by_id, get_all_users, update_user,
    get_user_zoho_keys, update_user_zoho_keys,
    get_user_search_keys, update_user_search_keys, get_employee_stats,
    COMPANY_CATEGORIES, DEFAULT_COMPANY_CATEGORY
)
from search_tool import set_search_context, get_effective_search_config, start_search_tracking, get_tracked_search_usage
from auth import hash_password, verify_password, generate_random_password, login_required, admin_required
from research_agent import research_company
from compliance_agent import check_compliance
from scoring_agent import score_company
# from contact_discovery_agent import find_decision_makers_apollo  # Apollo disabled
from audit_logger import log_action
from pdf_service import generate_research_pdf, generate_research_filename
# from email_service import send_research_pdf
from email_service import send_research_excel, send_combined_research_excel
from zoho_upload import upload_company_to_zoho 
from models import CompanyResearch
from warm_connect_agent import find_warm_connect, recommend_outreach_channel
from message_drafting_agent import draft_outreach_message
from meeting_brief_agent import generate_meeting_brief
from research_agent import research_company_with_financials, list_available_csr_years, get_csr_spend_for_year
from error_utils import classify_error
from llm_service import start_tracking, get_tracked_usage

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY")
if not app.secret_key:
    raise RuntimeError("SECRET_KEY is not set in .env")
app.logger.handlers = logger.handlers
app.logger.setLevel(logger.level)

def get_rate_limit_key():
    """Rate limit by logged-in username if available, otherwise by client IP address."""
    return session.get('username') or get_remote_address()


def _prepare_committee_linkedin(company):
    """Build a normalized lookup for committee-member LinkedIn URLs.

    Older records may store the map inside csr_data, and names can differ only
    by whitespace/capitalization from the extracted committee member name.
    """
    raw = company.get("committee_members_linkedin") or {}
    if not raw:
        raw = (company.get("csr_data") or {}).get("committee_members_linkedin") or {}
    company["_committee_members_linkedin_lookup"] = {
        str(name).strip().casefold(): url
        for name, url in raw.items()
        if url
    }
    return company

limiter = Limiter(
    key_func=get_rate_limit_key,
    app=app,
    # This default applies as ONE SHARED counter across every route that
    # doesn't set its own @limiter.limit (dashboard, company detail, approve/
    # reject, CRM field edits, warm-connect, settings, admin actions, etc.) -
    # 15 per 10 minutes was easily exhausted by completely normal browsing
    # (a dashboard reload + a couple of company pages + one approve/reject
    # already gets close). The genuinely expensive routes (/research,
    # /research-bulk, /upload-crm-bulk) already have their own tighter,
    # dedicated limits below and are unaffected by this default.
    default_limits=["1000 per day", "150 per hour", "60 per 10 minutes"],
    storage_uri="memory://",
)

@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({
        "status": "error",
        "message":f"Rate limit exceeded:{e.description}. Please wait before trying again."
    }), 429

@app.template_filter("commas")
def format_with_commas(value):
    """Numeric figures ko comma-separated dikhata hai, e.g. 1234.5 -> '1,234.5', 0.535 -> '0.535'."""
    if value is None or value == "" or value == "-":
        return "-"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return value
    if num == int(num):
        return f"{int(num):,}"
    # Preserve exact decimal digits (e.g. 0.535 -> '0.535') without dropping precision
    return f"{num:,.4f}".rstrip("0").rstrip(".")

@app.template_filter("source_name")
def format_source_name(url):
    """URL se readable source name nikalta hai, e.g. https://www.tcs.com/newsroom/... -> tcs.com"""
    try:
        netloc = urlparse(url).netloc or url
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return url

pipeline_jobs = {}
pipeline_jobs_lock = threading.Lock()

zoho_bulk_jobs = {}
zoho_bulk_jobs_lock = threading.Lock()
MAX_ZOHO_BULK = 50

# Lead Generation: per-category extraction jobs, guarded by one lock. The job
# dict (lead_gen_jobs) and category config live near the routes below.
lead_gen_lock = threading.Lock()

# Caps how many companies' pipelines actually run (i.e. fire search/LLM calls) at
# once, regardless of how many were queued via bulk CSV upload. Each pipeline
# thread still starts immediately so progress tracking/UX is unaffected, but
# real work blocks on this until a slot frees up - without it, a bulk upload of
# N companies fires N companies' worth of concurrent Tavily searches at once,
# which blows through Tavily's rate limit even with the per-call semaphore in
# search_tool.py (that one only caps instantaneous concurrency, not sustained rate).
_pipeline_concurrency = threading.Semaphore(6)



def set_pipeline_progress(company_id, stage, message, state="running", percent=0):
    with pipeline_jobs_lock:
        pipeline_jobs[company_id] = {
            "stage": stage,
            "message": message,
            "state": state,
            "percent": percent,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }


COMPANY_RESEARCH_TIMEOUT_SECONDS = int(os.getenv("COMPANY_RESEARCH_TIMEOUT_SECONDS", "300"))  # Default 5 mins (300 seconds)


def _execute_company_pipeline_stages(company_id, company_name, website, username=None):
    """Run sequential pipeline stages for a single company."""
    start_tracking()
    start_search_tracking()
    # Set search context for the execution thread
    user_search_keys = get_user_search_keys(username) if username else {}
    set_search_context(user_search_keys)
    try:
        set_pipeline_progress(company_id, "research", "Researching public company and CSR information.", percent=20)
        research = research_company(company_id, company_name, website)
        if not research:
            company_doc = get_company(company_id) or {}
            cause = company_doc.get("last_error") or {}
            message = cause.get("message", "Research could not return enough information.")
            set_pipeline_progress(company_id, "research", message, "error", 100)
            return

        set_pipeline_progress(company_id, "financials", "Pulling turnover/PBT from Screener and calculating CSR budget.", percent=35)
        try:
            research_company_with_financials(company_id, company_name, website)
        except Exception as fin_error:
            print(f"[Pipeline Warning] Financial research failed for {company_name}: {fin_error}")

        set_pipeline_progress(company_id, "compliance", "Checking eligibility and compliance signals.", percent=50)
        compliance = check_compliance(company_id, research)
        if compliance.get("blocked"):
            set_pipeline_progress(company_id, "complete", "Pipeline finished: this company was blocked by compliance checks.", "complete", 100)
            return

        set_pipeline_progress(company_id, "scoring", "Running fit-check and partnership assessment.", percent=75)
        score_result = score_company(company_id)

        # Collect stage warnings
        company_doc = get_company(company_id) or {}
        warning_fields = [
            ("last_error", "Research"),
            ("education_fitment_error", "Education research"),
            ("financial_last_error", "Financials"),
            ("csr_extraction_error", "CSR extraction"),
            ("scoring_error", "Scoring"),
        ]
        warnings = []
        for field, label in warning_fields:
            err = company_doc.get(field)
            if err:
                warnings.append({"stage": label, "type": err.get("type"), "message": err.get("message")})
        update_company(company_id, {"pipeline_warnings": warnings})

        if warnings:
            summary = "; ".join(f"{w['stage']}: {w['message']}" for w in warnings)
            complete_message = f"Pipeline complete, but some steps had issues - data may be incomplete. {summary}"
        else:
            complete_message = "Pipeline complete. The lead is ready for review."

        set_pipeline_progress(company_id, "complete", complete_message, "complete", 100)
    finally:
        usage = get_tracked_usage()
        if usage and usage.get("calls"):
            update_company(company_id, {"token_usage_estimate": usage})
            print(
                f"[Token Usage] {company_name}: {usage['total_tokens']} tokens total "
                f"({usage['prompt_tokens']} prompt + {usage['completion_tokens']} completion) "
                f"across {usage['calls']} LLM calls"
            )
        search_usage = get_tracked_search_usage()
        if search_usage and search_usage.get("total_calls"):
            update_company(company_id, {"search_usage_estimate": search_usage})
            print(
                f"[Search Usage] {company_name}: {search_usage['total_calls']} web search credits used "
                f"({search_usage['serper_calls']} Serper + {search_usage['tavily_calls']} Tavily), "
                f"{search_usage['cache_hits']} more served from cache at no extra cost"
            )


def run_company_pipeline(company_id, company_name, website, username=None):
    """Run the existing pipeline in the background and enforce strict execution timeout."""
    start_tracking()
    start_search_tracking()
    user_search_keys = get_user_search_keys(username) if username else {}
    set_search_context(user_search_keys)

    # Pre-flight check: verify web search API key is configured
    search_cfg = get_effective_search_config(username)
    if not search_cfg.get("configured") or not search_cfg.get("api_key"):
        err_msg = "No Web Search API key added. Please add a Tavily or Serper API key in Settings."
        print(f"[Pipeline Blocked] {company_name}: {err_msg}")
        update_company(company_id, {
            "status": "failed_research",
            "last_error": {"type": "missing_api_key", "message": err_msg}
        })
        set_pipeline_progress(company_id, "error", err_msg, "error", 100)
        return

    # Blocks here (not before the thread starts) so bulk-uploaded companies still
    # show up immediately with a "queued" status, but only _pipeline_concurrency
    # many of them actually fire search/LLM calls at once.
    set_pipeline_progress(company_id, "queued", "Waiting for a free pipeline slot...", percent=8)
    _pipeline_concurrency.acquire()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_execute_company_pipeline_stages, company_id, company_name, website, username)
            try:
                future.result(timeout=COMPANY_RESEARCH_TIMEOUT_SECONDS)
            except TimeoutError:
                mins = COMPANY_RESEARCH_TIMEOUT_SECONDS // 60
                print(f"[Pipeline Timeout] {company_name} exceeded {mins} minute(s) ({COMPANY_RESEARCH_TIMEOUT_SECONDS}s). Cancelling.")
                timeout_msg = f"Research timed out (exceeded {mins} minutes). Process automatically stopped."
                update_company(company_id, {
                    "status": "failed_research",
                    "last_error": {"type": "timeout", "message": timeout_msg}
                })
                set_pipeline_progress(company_id, "error", timeout_msg, "error", 100)
    except Exception as error:
        print(f"[Pipeline Error] {company_name}: {error}")
        cause = classify_error(error)
        set_pipeline_progress(company_id, "error", cause["message"], "error", 100)
    finally:
        usage = get_tracked_usage()
        if usage and usage["calls"]:
            update_company(company_id, {"token_usage_estimate": usage})
            print(
                f"[Token Usage] {company_name}: {usage['total_tokens']} tokens total "
                f"({usage['prompt_tokens']} prompt + {usage['completion_tokens']} completion) "
                f"across {usage['calls']} LLM calls"
            )
        search_usage = get_tracked_search_usage()
        if search_usage and search_usage.get("total_calls"):
            update_company(company_id, {"search_usage_estimate": search_usage})
            print(
                f"[Search Usage] {company_name}: {search_usage['total_calls']} web search credits used "
                f"({search_usage['serper_calls']} Serper + {search_usage['tavily_calls']} Tavily), "
                f"{search_usage['cache_hits']} more served from cache at no extra cost"
            )
        try:
            import psutil
            mem_mb = psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
            print(f"[Memory Usage] Active RAM after '{company_name}': {mem_mb:.2f} MB")
        except Exception:
            pass
        _pipeline_concurrency.release()


@app.route("/home", methods=["GET"])
@login_required
def home():
    """Post-login landing: choose between Lead Generation and Lead Research."""
    return render_template(
        "home.html",
        role=session.get("role"),
        username=session.get("username"),
        active_nav="home",
        impersonating=session.get("impersonated_by"),
    )


@app.route("/", methods=["GET"])
@login_required
def dashboard():
    try:
        username = session.get("username")
        role = session.get("role")
        companies = get_all_companies(username=username, role=role)
        db_error = None
    except Exception as e:
        companies = []
        db_error = "Database Connection Error: Please make sure MongoDB is running on localhost:27017, or configure your MONGODB_URI in your .env file."
        print(f"[Dashboard Error] Database exception: {e}")

    for c in companies:
        _prepare_committee_linkedin(c)
        c["id_str"] = str(c["_id"])
        # Ensure all template-accessed keys have safe defaults to prevent UndefinedError
        if "score" not in c:
            c["score"] = None
        if "tier" not in c:
            c["tier"] = None
        if "status" not in c:
            c["status"] = "pending"

    user_search_keys = get_user_search_keys(username) if username else {}
    set_search_context(user_search_keys)
    search_cfg = get_effective_search_config(username)
    search_configured = bool(search_cfg.get("configured") and search_cfg.get("api_key"))

    return render_template(
        "index.html",
        companies=companies,
        count=len(companies),
        db_error=db_error,
        search_configured=search_configured,
        search_provider=search_cfg.get("provider", "serper"),
        role=session.get("role"),
        username=session.get("username"),
        active_nav="dashboard",
        impersonating=session.get("impersonated_by"),
    )


# Slug -> (company_type stored on the doc, sidebar label, nav key, show financial stats on cards)
CATEGORY_PAGES = {
    "csr-corporates": ("CSR/Corporates", "CSR / Corporates", "csr_corporates", True),
    "institutional-donors": ("Institutional Donors", "Institutional Donors", "institutional_donors", False),
    "fcra": ("FCRA", "FCRA", "fcra", False),
    "hnis": ("HNIs", "HNIs", "hnis", False),
    "family-foundations": ("Family Foundations", "Family Foundations", "family_foundations", False),
}


def _render_category_page(slug):
    company_type, page_label, nav_key, show_financials = CATEGORY_PAGES[slug]
    try:
        username = session.get("username")
        role = session.get("role")
        all_companies = get_all_companies(username=username, role=role)
        companies = [c for c in all_companies if c.get("company_type") == company_type]
        db_error = None
    except Exception as e:
        companies = []
        db_error = "Database Connection Error: Please make sure MongoDB is running on localhost:27017, or configure your MONGODB_URI in your .env file."
        print(f"[{page_label} Error] Database exception: {e}")

    for c in companies:
        _prepare_committee_linkedin(c)
        c["id_str"] = str(c["_id"])
        if "score" not in c:
            c["score"] = None
        if "tier" not in c:
            c["tier"] = None
        if "status" not in c:
            c["status"] = "pending"

    username = session.get("username")
    user_search_keys = get_user_search_keys(username) if username else {}
    set_search_context(user_search_keys)
    search_cfg = get_effective_search_config(username)
    search_configured = bool(search_cfg.get("configured") and search_cfg.get("api_key"))

    return render_template(
        "category.html",
        companies=companies,
        count=len(companies),
        db_error=db_error,
        search_configured=search_configured,
        search_provider=search_cfg.get("provider", "serper"),
        role=session.get("role"),
        username=username,
        active_nav=nav_key,
        page_label=page_label,
        company_type=company_type,
        show_financials=show_financials,
        impersonating=session.get("impersonated_by"),
    )


for _slug in CATEGORY_PAGES:
    app.add_url_rule(
        f"/{_slug}",
        endpoint=f"category_{_slug.replace('-', '_')}",
        view_func=login_required(lambda slug=_slug: _render_category_page(slug)),
        methods=["GET"],
    )


# ---------------------------------------------------------------------------
# Lead Generation: one generator tab per category. CSR/Corporates and Family
# Foundations have working scrapers; the other three are pending a source.
# ---------------------------------------------------------------------------
# slug -> config: label, nav key, status, output file, download name
LEAD_GEN_CATEGORIES = {
    "csr-corporates": {
        "label": "CSR / Corporates", "nav": "gen_csr", "status": "ready",
        "file": "company_universe.xlsx", "download": "company_universe.xlsx",
    },
    "institutional-donors": {
        "label": "Institutional Donors", "nav": "gen_institutional",
        "status": "ready", "file": "institutional_donors.xlsx",
        "download": "institutional_donors.xlsx",
    },
    "fcra": {
        "label": "FCRA", "nav": "gen_fcra", "status": "ready",
        "file": "fcra_donors.xlsx", "download": "fcra_donors.xlsx",
    },
    "hnis": {
        "label": "HNIs", "nav": "gen_hnis", "status": "ready",
        "file": "hurun_philanthropists.xlsx", "download": "hurun_philanthropists.xlsx",
    },
    "family-foundations": {
        "label": "Family Foundations", "nav": "gen_family", "status": "ready",
        "file": "foundations.xlsx", "download": "foundations.xlsx",
    },
}

# slug -> live job state
lead_gen_jobs = {}


def _lead_gen_job(slug):
    with lead_gen_lock:
        return dict(lead_gen_jobs.get(slug, {"state": "idle", "message": "",
                                             "percent": 0, "stats": None}))


LISTING_FILTERS = ("all", "listed", "unlisted")


def _filter_by_listing(headers, data, listing):
    """Keep rows whose "Listing Status" matches `listing` (listed/unlisted).
    Files without that column, or listing="all", are returned unfiltered."""
    if listing not in ("listed", "unlisted") or "Listing Status" not in headers:
        return data
    idx = headers.index("Listing Status")
    return [r for r in data if str(r[idx] or "").strip().lower() == listing]


# Class (A/B/C/D/NA, or Review when verify_pbt_matches.py could not confirm the
# Screener match) for listed CSR companies, written by categorize_pbt.py.
# Listed companies not yet fetched by that script get a blank Class.
PBT_CACHE_FILE = "company_pbt_cache.jsonl"
PBT_CLASSES = ("A", "B", "C", "D", "NA", "Review")
PBT_CLASS_FILTERS = ("all",) + PBT_CLASSES
PBT_COLUMNS = ["Screener Name", "Class"]
PREVIEW_PAGE_SIZE = 25
_pbt_cache = {"mtime": None, "by_name": {}, "by_isin": {}}


def _load_pbt_classes():
    """(by_name, by_isin) lookups from the categorize_pbt.py cache, re-read on change."""
    if not os.path.exists(PBT_CACHE_FILE):
        return {}, {}
    mtime = os.path.getmtime(PBT_CACHE_FILE)
    if _pbt_cache["mtime"] != mtime:
        by_name, by_isin = {}, {}
        with open(PBT_CACHE_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue  # partial line while the script is still writing
                by_name[rec["company_name"]] = rec
                if rec.get("isin"):
                    by_isin[rec["isin"]] = rec
        _pbt_cache.update(mtime=mtime, by_name=by_name, by_isin=by_isin)
    return _pbt_cache["by_name"], _pbt_cache["by_isin"]


def _attach_pbt_columns(headers, data):
    """Append PBT_COLUMNS to CSR universe rows (blank for unlisted / not yet fetched)."""
    by_name, by_isin = _load_pbt_classes()
    i_name, i_isin = headers.index("Company Name"), headers.index("ISIN")
    i_status = headers.index("Listing Status")
    out = []
    for r in data:
        rec = None
        if str(r[i_status] or "").strip().lower() == "listed":
            rec = by_name.get(r[i_name]) or by_isin.get(r[i_isin])
        out.append(tuple(r) + ((rec.get("screener_name"), rec.get("category")) if rec
                               else (None, None)))
    return headers + PBT_COLUMNS, out


def _filter_by_pbt_class(headers, data, cls):
    """Keep rows whose "Class" matches `cls`; "all" or no column → unfiltered."""
    if cls not in PBT_CLASSES or "Class" not in headers:
        return data
    idx = headers.index("Class")
    return [r for r in data if r[idx] == cls]


def _row_key_fn(headers):
    """Stable per-row selection key for the CSR universe (ISIN, else company name),
    or None for files without those columns."""
    if "ISIN" not in headers or "Company Name" not in headers:
        return None
    i_name, i_isin = headers.index("Company Name"), headers.index("ISIN")
    return lambda r: str(r[i_isin] or "").strip() or str(r[i_name] or "").strip()


def _read_universe_rows(filename):
    """Return (headers, data rows) from a generated Excel, or None if absent/empty."""
    if not filename or not os.path.exists(filename):
        return None
    import openpyxl
    wb = openpyxl.load_workbook(filename, read_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not rows:
        return None
    headers = [str(h) if h is not None else "" for h in rows[0]]
    if filename == LEAD_GEN_CATEGORIES["csr-corporates"]["file"] and "Listing Status" in headers:
        return _attach_pbt_columns(headers, rows[1:])
    return headers, rows[1:]


def _read_universe_stats(filename, listing="all", cls="all", q="", page=1):
    """Generic reader for any generated Excel — returns total, headers, one
    25-row preview page (rows as lists, plus selection keys for CSR) and
    generated_at, or None if the file is absent. `listing` / `cls` filter by the
    "Listing Status" / "Class" columns when the file has them; `q` searches the
    first (name) column."""
    try:
        parsed = _read_universe_rows(filename)
        if not parsed:
            return None
        headers, data = parsed
        counts = {f: len(_filter_by_listing(headers, data, f)) for f in LISTING_FILTERS}
        data = _filter_by_listing(headers, data, listing)
        class_counts = None
        if "Class" in headers:
            idx = headers.index("Class")
            class_counts = {c: sum(1 for r in data if r[idx] == c) for c in PBT_CLASSES}
            class_counts["all"] = len(data)
            data = _filter_by_pbt_class(headers, data, cls)
        if q:
            data = [r for r in data if q.lower() in str(r[0] or "").lower()]
        pages = max(1, -(-len(data) // PREVIEW_PAGE_SIZE))
        page = min(max(1, page), pages)
        start = (page - 1) * PREVIEW_PAGE_SIZE
        page_rows = data[start:start + PREVIEW_PAGE_SIZE]
        preview = [[("" if v is None else v) for v in r] for r in page_rows]
        key_fn = _row_key_fn(headers)
        keys = [key_fn(r) for r in page_rows] if key_fn else None
        generated_at = datetime.fromtimestamp(
            os.path.getmtime(filename), tz=timezone.utc
        ).isoformat()
        return {"total": len(data), "headers": headers, "listing": listing,
                "counts": counts, "class": cls, "class_counts": class_counts,
                "q": q, "page": page, "pages": pages, "keys": keys,
                "preview": preview, "generated_at": generated_at}
    except Exception as e:
        print(f"[LeadGen] stats read error: {e}")
        return None


def _run_generation(slug):
    """Background worker for a category's extraction."""
    cfg = LEAD_GEN_CATEGORIES[slug]

    def _set(state, message, percent, stats=None):
        with lead_gen_lock:
            lead_gen_jobs[slug] = {"state": state, "message": message,
                                   "percent": percent, "stats": stats}

    try:
        if slug == "csr-corporates":
            import extract_company as ec
            import extract_mca as emca
            records = []
            _set("running", "Fetching NSE listed companies…", 15)
            records += ec.fetch_nse()
            _set("running", "Fetching BSE listed companies…", 35)
            records += ec.fetch_bse()
            _set("running", "Scraping unlisted sources…", 55)
            records += ec.scrape_sharescart() + ec.scrape_unlistedzone() + ec.scrape_stockify()
            _set("running", "Fetching MCA Maharashtra registry…", 70)
            records += emca.pipeline_records()
            _set("running", "Deduplicating…", 85)
            merged = ec.dedupe(records)
            _set("running", "Removing lowest paid-up unlisted companies…", 90)
            merged = ec.drop_lowest_unlisted(merged)
            _set("running", "Writing Excel…", 95)
            ec.write_excel(merged, cfg["file"])
        elif slug == "family-foundations":
            import extract_foundation as ef
            records = []
            _set("running", "Scraping NGObase foundations…", 40)
            for path, label, ftype in ef.TARGETS:
                records += ef.scrape_ngobase(path, label, ftype)
            _set("running", "Scraping CSRBox foundations…", 70)
            records += ef.scrape_csrbox()
            _set("running", "Deduplicating…", 85)
            merged = ef.dedupe(records)
            _set("running", "Writing Excel…", 95)
            ef.write_excel(merged, cfg["file"])
        elif slug == "institutional-donors":
            import extract_institutional as ei
            import extract_iati as eiat
            _set("running", "Loading curated donor list…", 25)
            list_records = ei.curated_records()
            _set("running", "Querying World Bank projects…", 45)
            list_records += ei.scrape_worldbank()
            _set("running", "Querying IATI (India education donors)…", 60)
            for r in eiat.scrape_iati():
                list_records.append(ei.make_record(r["donor_name"], source="IATI"))
            # Dedupe the donor-LIST sources to unique donors.
            list_records = ei.dedupe(list_records)
            _set("running", "Fetching OECD CRS (all rows, live)…", 80)
            # Keep ALL OECD rows (donor x year x amount), no dedupe. Fallback if API down.
            oecd = ei.oecd_all_records()
            merged = list_records + (oecd if oecd else ei.oecd_dac_records())
            _set("running", "Writing Excel…", 95)
            ei.write_excel(merged, cfg["file"])
        elif slug == "hnis":
            import extract_hurun as eh
            _set("running", "Loading curated philanthropists…", 40)
            records = eh.curated_records()
            _set("running", "Scraping Hurun Rich List (top 100)…", 70)
            records += eh.scrape_richlist()
            _set("running", "Deduplicating…", 90)
            merged = eh.dedupe(records)
            _set("running", "Writing Excel…", 95)
            eh.write_excel(merged, cfg["file"])
        elif slug == "fcra":
            import extract_fcra as efc
            records = []
            _set("running", "Loading foreign aid agencies…", 25)
            records += efc.curated_foreign_aid()
            _set("running", "Querying IATI (foreign donors)…", 45)
            records += efc.scrape_iati_foreign()
            _set("running", "Scraping NGObase global foundations…", 70)
            records += efc.scrape_ngobase_global()
            _set("running", "Deduplicating…", 90)
            merged = efc.dedupe(records)
            _set("running", "Writing Excel…", 95)
            efc.write_excel(merged, cfg["file"])
        else:
            raise RuntimeError("No data source configured for this category yet.")

        stats = _read_universe_stats(cfg["file"])
        _set("complete", f"Done — {len(merged)} records extracted.", 100, stats)
    except Exception as e:
        print(f"[LeadGen:{slug}] extraction failed: {e}")
        _set("error", f"Extraction failed: {e}", 100)


@app.route("/lead-generation", methods=["GET"])
@admin_required
def lead_generation():
    # Landing → first category tab.
    return redirect(url_for("lead_generation_category", slug="csr-corporates"))


@app.route("/lead-generation/<slug>", methods=["GET"])
@admin_required
def lead_generation_category(slug):
    cfg = LEAD_GEN_CATEGORIES.get(slug)
    if not cfg:
        abort(404)
    job = _lead_gen_job(slug)
    stats = job.get("stats") or _read_universe_stats(cfg["file"])
    return render_template(
        "lead_generation.html",
        slug=slug,
        label=cfg["label"],
        status=cfg["status"],
        stats=stats,
        job_state=job.get("state", "idle"),
        role=session.get("role"),
        username=session.get("username"),
        active_nav=cfg["nav"],
        impersonating=session.get("impersonated_by"),
    )


@app.route("/lead-generation/<slug>/run", methods=["POST"])
@admin_required
def lead_generation_run(slug):
    cfg = LEAD_GEN_CATEGORIES.get(slug)
    if not cfg:
        abort(404)
    if cfg["status"] != "ready":
        return jsonify({"status": "error",
                        "message": "No data source configured for this category yet."}), 400
    with lead_gen_lock:
        if lead_gen_jobs.get(slug, {}).get("state") == "running":
            return jsonify({"status": "already_running",
                            "message": "Extraction already in progress."}), 409
        lead_gen_jobs[slug] = {"state": "running", "message": "Starting…",
                               "percent": 0, "stats": None}
    threading.Thread(target=_run_generation, args=(slug,), daemon=True).start()
    return jsonify({"status": "started"}), 202


@app.route("/lead-generation/<slug>/progress", methods=["GET"])
@admin_required
@limiter.exempt
def lead_generation_progress(slug):
    if slug not in LEAD_GEN_CATEGORIES:
        abort(404)
    return jsonify(_lead_gen_job(slug))


@app.route("/lead-generation/<slug>/stats", methods=["GET"])
@admin_required
def lead_generation_stats(slug):
    cfg = LEAD_GEN_CATEGORIES.get(slug)
    if not cfg:
        abort(404)
    listing = request.args.get("listing", "all").lower()
    if listing not in LISTING_FILTERS:
        listing = "all"
    cls = request.args.get("class", "all")
    if cls not in PBT_CLASS_FILTERS:
        cls = "all"
    q = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int)
    stats = _read_universe_stats(cfg["file"], listing, cls, q, page)
    if not stats:
        return jsonify({"status": "error", "message": "No generated data yet."}), 404
    return jsonify(stats)


@app.route("/lead-generation/<slug>/download", methods=["GET", "POST"])
@admin_required
def lead_generation_download(slug):
    """GET: whole file, or rows matching ?listing= / ?class=.
    POST {"keys": [...]}: only the selected rows (CSR selection keys)."""
    cfg = LEAD_GEN_CATEGORIES.get(slug)
    if not cfg or not cfg["file"] or not os.path.exists(cfg["file"]):
        abort(404)
    listing = request.args.get("listing", "all").lower()
    if listing not in ("listed", "unlisted"):
        listing = "all"
    cls = request.args.get("class", "all")
    if cls not in PBT_CLASSES:
        cls = "all"
    selected = None
    if request.method == "POST":
        selected = set((request.get_json(silent=True) or {}).get("keys") or [])
        if not selected:
            return jsonify({"status": "error", "message": "No rows selected."}), 400
    elif listing == "all" and slug != "csr-corporates":
        return send_file(cfg["file"], as_attachment=True, download_name=cfg["download"])
    # Filtered / selected download (or CSR with Class columns): rebuild a workbook.
    import openpyxl
    headers, data = _read_universe_rows(cfg["file"])
    if selected is not None:
        key_fn = _row_key_fn(headers)
        if not key_fn:
            abort(400)
        rows = [r for r in data if key_fn(r) in selected]
        suffix = "_selected"
    else:
        rows = _filter_by_pbt_class(headers, _filter_by_listing(headers, data, listing), cls)
        suffix = "".join(f"_{s}" for s in (listing, cls) if s != "all")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(headers)
    for r in rows:
        ws.append(list(r))
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    base, ext = os.path.splitext(cfg["download"])
    return send_file(out, as_attachment=True, download_name=f"{base}{suffix}{ext}",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/lead-generation/<slug>/delete", methods=["POST"])
@admin_required
def lead_generation_delete(slug):
    cfg = LEAD_GEN_CATEGORIES.get(slug)
    if not cfg:
        abort(404)
    with lead_gen_lock:
        if lead_gen_jobs.get(slug, {}).get("state") == "running":
            return jsonify({"status": "error",
                            "message": "Extraction is running — wait for it to finish."}), 409
    try:
        if cfg["file"] and os.path.exists(cfg["file"]):
            os.remove(cfg["file"])
        with lead_gen_lock:
            lead_gen_jobs[slug] = {"state": "idle", "message": "", "percent": 0, "stats": None}
        return jsonify({"status": "deleted"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/company/<company_id>", methods=["GET"])
@login_required
def company_detail(company_id):
    c = get_company(company_id)
    if not c:
        abort(404)
    _prepare_committee_linkedin(c)
    c["id_str"] = str(c["_id"])
    # Ensure all template-accessed keys have safe defaults to prevent UndefinedError
    if "score" not in c:
        c["score"] = None
    if "tier" not in c:
        c["tier"] = None
    if "status" not in c:
        c["status"] = "pending"
    return render_template(
        "company_detail.html",
        c=c,
        role=session.get("role"),
        username=session.get("username"),
        active_nav="dashboard",
        impersonating=session.get("impersonated_by"),
    )


@app.route("/research", methods=["POST"])
@login_required
@limiter.limit("5 per minute; 30 per hour")
def add_company():
    company_name = request.form.get("company_name", "").strip()
    website = request.form.get("website", "").strip() or None
    company_type = request.form.get("company_type", DEFAULT_COMPANY_CATEGORY).strip()
    if company_type not in COMPANY_CATEGORIES:
        company_type = DEFAULT_COMPANY_CATEGORY
    username = session.get("username")

    if not company_name:
        return jsonify({"status": "error", "message": "Company name is required"}), 400

    # Pre-flight check: verify web search API key is configured
    user_search_keys = get_user_search_keys(username) if username else {}
    set_search_context(user_search_keys)
    search_cfg = get_effective_search_config(username)
    if not search_cfg.get("configured") or not search_cfg.get("api_key"):
        return jsonify({
            "status": "error",
            "message": "No Web Search API key added. Please add a Tavily or Serper API key in Settings first."
        }), 400

    # 1. Create company in MongoDB (status: 'new')
    company_id = create_company(company_name, website, created_by=username, company_type=company_type)

    set_pipeline_progress(company_id, "queued", "Company added. Preparing the research pipeline.", percent=5)
    threading.Thread(
        target=run_company_pipeline, args=(company_id, company_name, website, username), daemon=True
    ).start()
    return jsonify({"status": "started", "company_id": company_id}), 202


MAX_BULK_ROWS = 200
MAX_EMAIL_BULK = 50

@app.route("/research/financial", methods=["POST"])
@login_required
@limiter.limit("5 per minute; 30 per hour")
def research_financial():
    """
    Company ka financial research start karne ke liye
    POST request: {"company_name": "TCS", "website": "https://www.tcs.com"}
    """
    try:
        data = request.json
        company_name = data.get("company_name")
        website = data.get("website")
        username = session.get("username")

        if not company_name:
            return {"error": "company_name required"}, 400

        user_search_keys = get_user_search_keys(username) if username else {}
        set_search_context(user_search_keys)
        search_cfg = get_effective_search_config(username)
        if not search_cfg.get("configured") or not search_cfg.get("api_key"):
            return {"error": "No Web Search API key added. Please add a Tavily or Serper API key in Settings first."}, 400

        # Create company record
        company_id = create_company(company_name, website, created_by=username)

        # Research start karo
        result = research_company_with_financials(company_id, company_name, website)

        if result:
            return {"success": True, "data": result}, 200
        else:
            return {"success": False, "error": "Research failed"}, 400

    except Exception as e:
        return {"error": str(e)}, 500


@app.route("/research-bulk", methods=["POST"])
@login_required
@limiter.limit("15 per minute; 60 per hour")
def add_companies_bulk():
    upload = request.files.get("csv_file")
    username = session.get("username")
    company_type = request.form.get("company_type", DEFAULT_COMPANY_CATEGORY).strip()
    if company_type not in COMPANY_CATEGORIES:
        company_type = DEFAULT_COMPANY_CATEGORY
    if not upload or not upload.filename:
        return jsonify({"status": "error", "message": "Please choose a CSV or Excel file to upload."}), 400

    user_search_keys = get_user_search_keys(username) if username else {}
    set_search_context(user_search_keys)
    search_cfg = get_effective_search_config(username)
    if not search_cfg.get("configured") or not search_cfg.get("api_key"):
        return jsonify({
            "status": "error",
            "message": "No Web Search API key added. Please add a Tavily or Serper API key in Settings before running bulk research."
        }), 400

    filename = upload.filename.lower()
    rows_data = []

    # 1. Handle Excel files (.xlsx, .xls)
    if filename.endswith(".xlsx") or filename.endswith(".xls"):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(upload, data_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            if not rows or len(rows) < 2:
                return jsonify({"status": "error", "message": "Excel file is empty or missing data rows."}), 400
            
            headers = [str(h or "").strip().lower() for h in rows[0]]
            name_idx = None
            web_idx = None

            for idx, h in enumerate(headers):
                if h in ["company_name", "company name", "company", "name", "organization", "organisation"]:
                    name_idx = idx
                elif h in ["website", "website url", "url", "web", "domain", "link"]:
                    web_idx = idx

            if name_idx is None:
                return jsonify({
                    "status": "error",
                    "message": "Excel sheet must have a 'company_name' or 'company' column."
                }), 400

            for r in rows[1:]:
                if not r or len(r) <= name_idx:
                    continue
                c_name = str(r[name_idx] or "").strip()
                w_url = str(r[web_idx] or "").strip() if (web_idx is not None and len(r) > web_idx) else ""
                if c_name and c_name.lower() not in ["none", "nan", "null"]:
                    rows_data.append({"company_name": c_name, "website": w_url})
        except Exception as e:
            return jsonify({"status": "error", "message": f"Could not read Excel file: {str(e)}"}), 400

    # 2. Handle CSV files with multi-encoding fallback
    else:
        content_bytes = upload.read()
        raw_text = None
        for enc in ["utf-8-sig", "utf-8", "latin-1", "cp1252", "iso-8859-1"]:
            try:
                raw_text = content_bytes.decode(enc)
                break
            except UnicodeDecodeError:
                continue

        if raw_text is None:
            raw_text = content_bytes.decode("utf-8", errors="replace")

        reader = csv.DictReader(io.StringIO(raw_text))
        if not reader.fieldnames:
            return jsonify({"status": "error", "message": "CSV file appears empty."}), 400

        name_key = None
        website_key = None
        for col in reader.fieldnames:
            clean_col = (col or "").strip().lower()
            if clean_col in ["company_name", "company name", "company", "name", "organization", "organisation"]:
                name_key = col
            elif clean_col in ["website", "website url", "url", "web", "domain", "link"]:
                website_key = col

        if not name_key:
            return jsonify({
                "status": "error",
                "message": "CSV must have a 'company_name' or 'company' column header (website is optional)."
            }), 400

        for row in reader:
            c_name = (row.get(name_key) or "").strip()
            w_url = (row.get(website_key) or "").strip() if website_key else ""
            if c_name:
                rows_data.append({"company_name": c_name, "website": w_url})

    started = []
    seen_names = set()
    skipped = 0

    for item in rows_data:
        if len(started) >= MAX_BULK_ROWS:
            skipped += 1
            continue

        company_name = item["company_name"]
        website = item["website"]
        dedupe_key = company_name.lower()
        if not company_name or dedupe_key in seen_names:
            skipped += 1
            continue
        seen_names.add(dedupe_key)

        company_id = create_company(company_name, website or None, created_by=username, company_type=company_type)
        set_pipeline_progress(company_id, "queued", "Company added. Preparing the research pipeline.", percent=5)
        threading.Thread(
            target=run_company_pipeline, args=(company_id, company_name, website or None, username), daemon=True
        ).start()
        started.append({"company_id": company_id, "company_name": company_name})

    if not started:
        return jsonify({"status": "error", "message": "No valid company names found in the file."}), 400

    return jsonify({"status": "started", "started": started, "skipped": skipped}), 202



@app.route("/research-progress/<company_id>", methods=["GET"])
@login_required
@limiter.exempt
def research_progress(company_id):
    with pipeline_jobs_lock:
        progress = pipeline_jobs.get(company_id)
    if not progress:
        # Progress is held in memory, so it disappears if the app process is
        # restarted. Fall back to the persisted company record so bulk polling
        # does not turn an existing company into a 404 after a refresh/restart.
        company = get_company(company_id)
        if not company:
            return jsonify({"status": "error", "message": "Company not found."}), 404

        saved_status = company.get("status")
        if saved_status in ("researched", "scored"):
            progress = {
                "stage": "complete",
                "message": "Research already completed. Open the company to review it.",
                "state": "complete",
                "percent": 100,
            }
        elif saved_status == "failed_research":
            cause = company.get("last_error") or {}
            progress = {
                "stage": "research",
                "message": cause.get("message", "Research failed for this company."),
                "state": "error",
                "percent": 100,
            }
        else:
            progress = {
                "stage": "queued",
                "message": "Progress was interrupted. Open the company or start its research again.",
                "state": "error",
                "percent": 0,
            }
    return jsonify(progress)

@app.route("/approve/<company_id>", methods=["POST"])
@login_required
def approve_company(company_id):
    company = get_company(company_id)
    if not company or company.get("tier") != "Tier C":
        return redirect(url_for("dashboard"))
    update_company(company_id, {"approval_status": "approved", "approved_by": session.get("username")})
    return redirect(url_for("dashboard"))

@app.route("/reject/<company_id>", methods=["POST"])
@login_required
def reject_company(company_id):
    company = get_company(company_id)
    if not company or company.get("tier") != "Tier C":
        return redirect(url_for("dashboard"))
    update_company(company_id, {"approval_status": "rejected"})
    return redirect(url_for("dashboard"))

@app.route("/delete/<company_id>", methods=["POST"])
@login_required
def remove_company(company_id):
    delete_company(company_id)
    return redirect(url_for("dashboard"))


@app.route("/delete-bulk", methods=["POST"])
@login_required
def remove_companies_bulk():
    payload = request.get_json(silent=True) or {}
    company_ids = payload.get("company_ids") or []
    if not isinstance(company_ids, list) or not company_ids:
        return jsonify({"message": "No companies selected."}), 400
    deleted = delete_companies(company_ids)
    return jsonify({"deleted": deleted, "requested": len(company_ids)})

@app.route("/settings/search", methods=["GET", "POST"])
@login_required
def settings_search():
    username = session.get("username")
    if request.method == "POST":
        provider = request.form.get("search_provider", "serper").strip().lower()
        api_key = request.form.get("api_key", "").strip().strip('"').strip("'")
        deep_scrape = request.form.get("deep_scrape", "auto").strip().lower()

        if provider not in ("serper", "tavily"):
            provider = "serper"
        if deep_scrape not in ("auto", "on", "off"):
            deep_scrape = "auto"

        search_keys = {
            "provider": provider,
            "api_key": api_key,
            "deep_scrape": deep_scrape,
        }
        update_user_search_keys(username, search_keys)
        set_search_context(search_keys, username=username)
        success_msg = f"Your personal {provider.capitalize()} API key has been saved!" if api_key else "Web Search API settings updated."
        return render_template(
            "settings_search.html",
            success=success_msg,
            search_keys=search_keys,
            username=username,
            role=session.get("role"),
            active_nav="settings_search",
            impersonating=session.get("impersonated_by"),
        )

    search_keys = get_user_search_keys(username)
    return render_template(
        "settings_search.html",
        search_keys=search_keys,
        username=username,
        role=session.get("role"),
        active_nav="settings_search",
        impersonating=session.get("impersonated_by"),
    )


@app.route("/api/settings/test-search", methods=["POST"])
@login_required
def api_test_search_key():
    data = request.json or {}
    provider = data.get("provider", "serper").strip().lower()
    api_key = data.get("api_key", "").strip().strip('"').strip("'")

    if not api_key:
        return jsonify({"valid": False, "message": "API key is required to run a test."}), 400

    try:
        if provider == "tavily":
            from search_tool import _tavily_search_with_limit
            res = _tavily_search_with_limit("India CSR overview", max_results=1, api_key=api_key)
            results_count = len(res.get("results", []))
            return jsonify({"valid": True, "message": f"Tavily connected successfully! (Returned {results_count} test result)"})
        elif provider == "serper":
            from search_tool import _serper_search
            res = _serper_search("India CSR overview", max_results=1, api_key=api_key)
            results_count = len(res.get("results", []))
            return jsonify({"valid": True, "message": f"Google Serper connected successfully! (Returned {results_count} test result)"})
        else:
            return jsonify({"valid": False, "message": "Unknown provider selected."}), 400
    except Exception as exc:
        err_str = str(exc)
        if "Unauthorized" in err_str or "invalid" in err_str.lower() or "401" in err_str:
            return jsonify({
                "valid": False,
                "message": f"Authentication Failed (401): The {provider.capitalize()} API key was rejected as invalid. For Tavily, ensure the key starts with 'tvly-' and is active on app.tavily.com."
            }), 400
        elif "429" in err_str or "rate" in err_str.lower():
            return jsonify({
                "valid": False,
                "message": f"Rate Limit / Quota Exceeded (429): Your {provider.capitalize()} account may have reached its request quota or concurrency limit."
            }), 400
        return jsonify({"valid": False, "message": f"Test failed: {err_str}"}), 400


@app.route("/settings/zoho", methods=["GET", "POST"])
@login_required
def settings_zoho():
    username = session.get("username")
    if request.method == "POST":
        client_id = request.form.get("client_id", "").strip()
        client_secret = request.form.get("client_secret", "").strip()
        refresh_token = request.form.get("refresh_token", "").strip()
        api_domain = request.form.get("api_domain", "https://www.zohoapis.in").strip()
        accounts_url = request.form.get("accounts_url", "https://accounts.zoho.in").strip()

        zoho_keys = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "api_domain": api_domain,
            "accounts_url": accounts_url,
        }
        update_user_zoho_keys(username, zoho_keys)
        return render_template(
            "settings_zoho.html",
            success="Your personal Zoho CRM API credentials have been saved!",
            zoho_keys=zoho_keys,
            username=username,
            role=session.get("role"),
            active_nav="settings_zoho",
            impersonating=session.get("impersonated_by"),
        )

    zoho_keys = get_user_zoho_keys(username)
    return render_template(
        "settings_zoho.html",
        zoho_keys=zoho_keys,
        username=username,
        role=session.get("role"),
        active_nav="settings_zoho",
        impersonating=session.get("impersonated_by"),
    )



@app.route("/upload-crm/<company_id>", methods=["POST"])
@login_required
def upload_crm(company_id):
    username = session.get("username")
    result = upload_company_to_zoho(company_id, username=username)
    if result.get("status") in ("success", "simulated"):
        update_company(company_id, {"crm_uploaded_by": username}, username=username)
    return redirect(url_for("dashboard"))


def run_bulk_zoho_upload(job_id, company_ids, uploaded_by):
    """Sequentially uploads each company to Zoho (same per-record call/duplicate-check
    as the single upload) and records per-company results for the progress poller."""
    with zoho_bulk_jobs_lock:
        zoho_bulk_jobs[job_id] = {
            "state": "running", "total": len(company_ids),
            "completed": 0, "succeeded": 0, "failed": 0, "results": [],
        }

    for company_id in company_ids:
        company = get_company(company_id, username=uploaded_by) if uploaded_by else get_company(company_id)
        company_name = company.get("company_name") if company else company_id

        if not company:
            result = {"status": "error", "message": "Company not found in database"}
        else:
            result = upload_company_to_zoho(company_id, username=uploaded_by)
            if result.get("status") in ("success", "simulated"):
                update_company(company_id, {"crm_uploaded_by": uploaded_by}, username=uploaded_by)

        with zoho_bulk_jobs_lock:
            job = zoho_bulk_jobs[job_id]
            job["completed"] += 1
            if result.get("status") in ("success", "simulated"):
                job["succeeded"] += 1
            else:
                job["failed"] += 1
            job["results"].append({
                "company_id": company_id, "company_name": company_name,
                "status": result.get("status"), "message": result.get("message"),
            })

    with zoho_bulk_jobs_lock:
        zoho_bulk_jobs[job_id]["state"] = "complete"


@app.route("/upload-crm-bulk", methods=["POST"])
@login_required
@limiter.limit("3 per minute")
def upload_crm_bulk():
    data = request.get_json(silent=True) or {}
    company_ids = [cid for cid in (data.get("company_ids") or []) if cid]

    if not company_ids:
        return jsonify({"status": "error", "message": "No companies selected."}), 400
    if len(company_ids) > MAX_ZOHO_BULK:
        return jsonify({"status": "error", "message": f"Select at most {MAX_ZOHO_BULK} companies at a time."}), 400

    job_id = str(uuid.uuid4())
    threading.Thread(
        target=run_bulk_zoho_upload, args=(job_id, company_ids, session.get("username")), daemon=True
    ).start()
    return jsonify({"status": "started", "job_id": job_id, "total": len(company_ids)}), 202


@app.route("/upload-crm-bulk-progress/<job_id>", methods=["GET"])
@login_required
@limiter.exempt
def upload_crm_bulk_progress(job_id):
    with zoho_bulk_jobs_lock:
        job = zoho_bulk_jobs.get(job_id)
    if not job:
        return jsonify({"status": "error", "message": "Job not found."}), 404
    return jsonify(job)

@app.route("/api/companies", methods=["GET"])
@login_required
def api_companies():
    try:
        username = session.get("username")
        role = session.get("role")
        companies = get_all_companies(username=username, role=role)
        for c in companies:
            c["_id"] = str(c["_id"])
        return jsonify(companies)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/update-crm-fields/<company_id>", methods=["POST"])
@login_required
def update_crm_fields(company_id):
    """Update CRM fields for a company"""
    updates = {}
    
    lead_owner = request.form.get("lead_owner", "").strip()
    lead_status = request.form.get("lead_status", "").strip()
    next_followup_date = request.form.get("next_followup_date", "").strip()
    immediate_action = request.form.get("immediate_action", "").strip()
    description = request.form.get("description", "").strip()
    
    if "lead_owner" in request.form:
        updates["crm.lead_owner"] = request.form.get("lead_owner", "").strip()
    if "lead_status" in request.form:
        updates["crm.lead_status"] = request.form.get("lead_status", "").strip()
    if "next_followup_date" in request.form:
        updates["crm.next_followup_date"] = request.form.get("next_followup_date", "").strip()
    if "immediate_action" in request.form:
        updates["crm.immediate_action"] = request.form.get("immediate_action", "").strip()
    if "description" in request.form:
        updates["crm.description"] = request.form.get("description", "").strip()
    if "decision_maker_name" in request.form:
        updates["crm.decision_maker_name"] = request.form.get("decision_maker_name","").strip()
    if "decision_maker_email" in request.form:
        updates["crm.decision_maker_email"] = request.form.get("decision_maker_email", "").strip()
    if "decision_maker_phone" in request.form:
        updates["crm.decision_maker_phone"] = request.form.get("decision_maker_phone", "").strip()
    if "company_type" in request.form:
        company_type = request.form.get("company_type", "").strip()
        if company_type in COMPANY_CATEGORIES:
            updates["company_type"] = company_type

    if updates:
        try:
            update_company(company_id, updates)
            log_action(company_id, "crm_fields_updated", "Dashboard", 
                      details=f"Updated fields: {', '.join(updates.keys())}")
            return jsonify({"status": "updated", "message": "CRM fields updated successfully"})
        except Exception as e:
            print(f"[Error] Failed to update CRM fields: {e}")
            return jsonify({"status": "error", "message": str(e)}), 500
    
    return jsonify({"status": "no_updates", "message": "No fields provided to update"})

@app.route("/download-research/<company_id>", methods=["GET"])
@login_required
def download_research(company_id):
    """Download research data as PDF"""
    try:
        company = get_company(company_id)
        if not company:
            return jsonify({"status": "error", "message": "Company not found"}), 404
        
        # Generate PDF
        pdf_buffer = generate_research_pdf(company)
        filename = generate_research_filename(company["company_name"])
        
        log_action(company_id, "research_pdf_downloaded", "Dashboard", 
                  details=f"Downloaded as {filename}")
        
        return send_file(
            pdf_buffer,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=filename
        )
    except Exception as e:
        print(f"[Error] PDF generation failed: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/email-research/<company_id>", methods=["POST"])
@login_required
def email_research(company_id):
    """Send research data via email"""
    try:
        recipient_email = request.form.get("recipient_email", "").strip()
        recipient_name = request.form.get("recipient_name", "").strip()
        
        if not recipient_email:
            return jsonify({"status": "error", "message": "Email address required"}), 400
        
        # Send email with PDF
        result = send_research_excel(
            company_id,
            recipient_email,
            recipient_name
        )
        
        if result["success"]:
            log_action(company_id, "research_pdf_emailed", "Dashboard",
                      details=f"Sent to {recipient_email}")
            return jsonify({"status": "success", "message": result["message"]})
        else:
            return jsonify({"status": "error", "message": result["message"]}), 500
            
    except Exception as e:
        print(f"[Error] Email sending failed: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/email-research-bulk", methods=["POST"])
@login_required
def email_research_bulk():
    try:
        payload = request.get_json(silent=True) or {}
        company_ids = [cid for cid in (payload.get("company_ids") or []) if cid]
        recipient_email = (payload.get("recipient_email") or "").strip()
        recipient_name = (payload.get("recipient_name") or "").strip()
        if not company_ids:
            return jsonify({"status": "error", "message": "Select at least one company."}), 400
        if len(company_ids) > MAX_EMAIL_BULK:
            return jsonify({"status": "error", "message": f"Select at most {MAX_EMAIL_BULK} companies."}), 400
        if not recipient_email:
            return jsonify({"status": "error", "message": "Email address required."}), 400

        result = send_combined_research_excel(company_ids, recipient_email, recipient_name)
        if result["success"]:
            return jsonify({"status": "success", "message": result["message"]})
        return jsonify({"status": "error", "message": result["message"]}), 500
    except Exception as e:
        print(f"[Error] Bulk email sending failed: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


def _extract_decision_maker_info(company: dict) -> dict:
    """Extracts company name, decision maker name, position, and LinkedIn URL cleanly."""
    research = company.get("research_json") or {}
    contact = research.get("contact") or {}
    crm = company.get("crm") or {}
    csr = company.get("csr_data") or {}

    # Company name
    company_name = company.get("company_name", "Unknown Company")

    def _clean_val(v):
        if not v or str(v).strip().lower() in ("not found", "none", "n/a", "not publicly available", "-", "null"):
            return ""
        return str(v).strip()

    # Decision Maker Name
    dm_name = _clean_val(crm.get("decision_maker_name"))
    if not dm_name:
        fname = _clean_val(contact.get("first_name"))
        lname = _clean_val(contact.get("last_name"))
        dm_name = f"{fname} {lname}".strip()
    if not dm_name:
        comm = csr.get("committee_members") or company.get("committee_members") or []
        if comm and isinstance(comm, list):
            dm_name = _clean_val(comm[0])
    if not dm_name:
        dm_name = "Not Available"

    # Position / Designation
    pos_candidates = [
        crm.get("decision_maker_designation"),
        crm.get("designation"),
        contact.get("designation"),
        contact.get("title"),
        contact.get("role")
    ]
    position = ""
    for pc in pos_candidates:
        c = _clean_val(pc)
        if c:
            position = c
            break
    if not position:
        if csr.get("committee_members") or company.get("committee_members"):
            position = "CSR Committee Member"
        else:
            position = "Key Official / CSR Lead"

    # LinkedIn URL - iterate through all possible candidate keys individually
    li_candidates = [
        crm.get("decision_maker_linkedin"),
        crm.get("linkedin_url"),
        crm.get("linkedin"),
        contact.get("linkedin_url"),
        contact.get("linkedin"),
        research.get("linkedin_url"),
        research.get("linkedin"),
        company.get("linkedin_url"),
        company.get("linkedin"),
    ]
    li_url = ""
    for cand in li_candidates:
        c = _clean_val(cand)
        if c and "linkedin.com" in c.lower():
            li_url = c
            break

    # Committee / member lookup fallback
    comm_lookup = company.get("committee_members_linkedin") or csr.get("committee_members_linkedin") or {}
    if not li_url and isinstance(comm_lookup, dict):
        # 1. Match by decision maker name
        for k, v in comm_lookup.items():
            if v and "linkedin.com" in str(v).lower():
                if dm_name and dm_name != "Not Available" and (dm_name.lower() in str(k).lower() or str(k).lower() in dm_name.lower()):
                    li_url = str(v).strip()
                    break
        # 2. Match first available committee member link
        if not li_url:
            for v in comm_lookup.values():
                if v and "linkedin.com" in str(v).lower():
                    li_url = str(v).strip()
                    break

    if li_url:
        if li_url.startswith("www."):
            li_url = "https://" + li_url
        elif not li_url.startswith("http"):
            li_url = "https://" + li_url.lstrip("/")

    # Collect ALL LinkedIn URLs found for the company (Decision Maker + Committee Members + CSR Leads)
    all_li_list = []
    if li_url and dm_name and dm_name != "Not Available":
        all_li_list.append({"name": dm_name, "url": li_url, "role": position})


    comm_dict = company.get("committee_members_linkedin") or csr.get("committee_members_linkedin") or {}
    if isinstance(comm_dict, dict):
        for name, u in comm_dict.items():
            if u and "linkedin.com" in str(u).lower():
                clean_u = str(u).strip()
                if clean_u.startswith("www."):
                    clean_u = "https://" + clean_u
                elif not clean_u.startswith("http"):
                    clean_u = "https://" + clean_u.lstrip("/")
                if not any(item["url"].lower() == clean_u.lower() for item in all_li_list):
                    all_li_list.append({"name": str(name).strip(), "url": clean_u, "role": "Committee Member"})

    all_li_str = "; ".join([f"{item['name']} ({item['url']})" for item in all_li_list]) if all_li_list else (li_url or "Not Available")

    return {
        "id": company.get("id_str") or str(company.get("_id", "")),
        "company_name": company_name,
        "decision_maker_name": dm_name,
        "position": position,
        "linkedin_url": li_url,
        "all_linkedin_urls": all_li_str,
        "all_linkedin_list": all_li_list
    }


@app.route("/decision-makers-bulk", methods=["POST"])
@login_required
def decision_makers_bulk():
    try:
        username = session.get("username")
        role = session.get("role")
        payload = request.get_json(silent=True) or {}
        company_ids = [str(cid) for cid in (payload.get("company_ids") or []) if cid]

        all_companies = get_all_companies(username=username, role=role)
        if company_ids:
            target_companies = [c for c in all_companies if str(c.get("id_str") or c.get("_id")) in company_ids]
        else:
            target_companies = all_companies

        results = [_extract_decision_maker_info(c) for c in target_companies]
        return jsonify({"status": "success", "data": results, "total": len(results)})
    except Exception as e:
        print(f"[Error] Failed to fetch decision makers: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/export-decision-makers", methods=["POST"])
@login_required
def export_decision_makers():
    try:
        import io
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from flask import send_file

        username = session.get("username")
        role = session.get("role")
        payload = request.get_json(silent=True) or {}
        company_ids = [str(cid) for cid in (payload.get("company_ids") or []) if cid]

        all_companies = get_all_companies(username=username, role=role)
        if company_ids:
            target_companies = [c for c in all_companies if str(c.get("id_str") or c.get("_id")) in company_ids]
        else:
            target_companies = all_companies

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Decision Makers"

        headers = ["Company Name", "Decision Maker Name", "Position", "LinkedIn URL", "All Available LinkedIn URLs"]
        ws.append(headers)

        header_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
        header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")

        for col_num in range(1, 6):
            cell = ws.cell(row=1, column=col_num)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="left", vertical="center")

        for c in target_companies:
            info = _extract_decision_maker_info(c)
            ws.append([
                info["company_name"],
                info["decision_maker_name"],
                info["position"],
                info["linkedin_url"],
                info["all_linkedin_urls"]
            ])

        for col in ws.columns:
            max_len = max(len(str(cell.value or "")) for cell in col)
            col_letter = openpyxl.utils.get_column_letter(col[0].column)
            ws.column_dimensions[col_letter].width = min(max(max_len + 4, 18), 65)

        out = io.BytesIO()
        wb.save(out)
        out.seek(0)

        return send_file(
            out,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name="Decision_Makers_List.xlsx"
        )
    except Exception as e:
        print(f"[Error] Failed to export decision makers: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500



@app.route("/warm-connect/<company_id>", methods=["POST"])
@login_required
def warm_connect(company_id):
    company = get_company(company_id)
    warm_connect = find_warm_connect(company["company_name"])
    channel_rec = recommend_outreach_channel(
        company.get("category", "Tier C"),
        warm_connect,
        company.get("research_json", {}).get("csr_spend_priority","Low")

    )
    update_company(company_id,{
        "warm_connect": warm_connect.model_dump(),
        "channel_recommendation" : channel_rec
    })
    return jsonify({"warm_connect": warm_connect.model_dump(), "channel": channel_rec})

@app.route("/draft-message/<company_id>", methods=["POST"])
@login_required
def draft_message(company_id):
    company = get_company(company_id)
    channel = request.form.get("channel", "email")
    draft = draft_outreach_message(company, channel)
    update_company(company_id, {"drafted_message": draft.model_dump()})
    return jsonify(draft.model_dump())


@app.route("/meeting-brief/<company_id>", methods=["POST"])
@login_required
def meeting_brief(company_id):
    company = get_company(company_id)
    brief = generate_meeting_brief(company)
    update_company(company_id, {"meeting_brief": brief.model_dump()})
    return jsonify(brief.model_dump())

@app.route("/copy-gpt-table/<company_id>", methods=["GET"])
@login_required
def copy_gpt_table(company_id):
    from crm_mapper import format_gpt_horizontal_table
    company = get_company(company_id)
    if not company:
        return jsonify({"status": "error", "message": "Company not found"}), 404
    table_markdown = format_gpt_horizontal_table(company)
    return jsonify({"status": "success", "table": table_markdown})

@app.route("/csr-available-years/<company_id>", methods=["GET"])
@login_required
def csr_available_years(company_id):
    result = list_available_csr_years(company_id)
    status_code = 404 if result.get("status") == "error" else 200
    return jsonify(result), status_code

@app.route("/csr-history/<company_id>/<int:year>", methods=["GET"])
@login_required
def csr_history_year(company_id, year):
    result = get_csr_spend_for_year(company_id, year)
    status_code = 404 if result.get("status") == "error" else 200
    return jsonify(result), status_code

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    username = request.form.get("username", "").strip().lower()
    password = request.form.get("password", "")
    login_as = request.form.get("login_as", "user")
    if login_as not in ("admin", "user"):
        login_as = "user"

    user = get_user_by_username(username)
    if not user or not user.get("is_active", True) or not verify_password(password, user["password_hash"]):
        return render_template("login.html", error="Invalid credentials", login_as=login_as)

    if user["role"] != login_as:
        return render_template("login.html", error=f"This account is not registered as {login_as}", login_as=login_as)

    session.clear()
    session["user_id"] = str(user["_id"])
    session["username"] = user["username"]
    session["role"] = user["role"]

    update_user(session["user_id"], {"last_login": datetime.utcnow()})

    if user.get("must_change_password"):
        return redirect(url_for("change_password"))
    if user["role"] == "admin":
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("home"))


@app.route("/logout", methods=["GET"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "GET":
        return render_template("change_password.html")

    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    if len(new_password) < 8:
        return render_template("change_password.html", error="Password must be at least 8 characters")
    if new_password != confirm_password:
        return render_template("change_password.html", error="Passwords do not match")

    update_user(session["user_id"], {
        "password_hash": hash_password(new_password),
        "must_change_password": False,
    })
    return redirect(url_for("dashboard"))


@app.route("/admin", methods=["GET"])
@admin_required
def admin_dashboard():
    users = get_all_users()
    for u in users:
        u["id_str"] = str(u["_id"])

    employee_stats = []
    for u in users:
        if u["role"] == "admin":
            continue
        stats = get_employee_stats(u["username"])
        employee_stats.append({"username": u["username"], **stats})

    return render_template(
        "admin.html",
        users=users,
        employee_stats=employee_stats,
        impersonating=session.get("impersonated_by"),
        role=session.get("role"),
        username=session.get("username"),
        active_nav="admin",
    )


@app.route("/admin/users/create", methods=["POST"])
@admin_required
def admin_create_user():
    username = request.form.get("username", "").strip().lower()
    role = request.form.get("role", "user")
    if role not in ("admin", "user"):
        role = "user"

    if not username or get_user_by_username(username):
        return redirect(url_for("admin_dashboard"))

    temp_password = generate_random_password()
    create_user(username, hash_password(temp_password), role=role, must_change_password=True)
    log_action(None, "user_created", "Admin", details=f"Created user {username} with role {role}")

    users = get_all_users()
    for u in users:
        u["id_str"] = str(u["_id"])
    return render_template(
        "admin.html",
        users=users,
        new_username=username,
        new_password=temp_password,
        role=session.get("role"),
        username=session.get("username"),
        active_nav="admin",
    )


@app.route("/admin/users/<user_id>/toggle-active", methods=["POST"])
@admin_required
def admin_toggle_active(user_id):
    user = get_user_by_id(user_id)
    if user:
        update_user(user_id, {"is_active": not user.get("is_active", True)})
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/users/<user_id>/reset-password", methods=["POST"])
@admin_required
def admin_reset_password(user_id):
    target_check = get_user_by_id(user_id)
    if not target_check or target_check.get("role") == "admin":
        abort(403)

    temp_password = generate_random_password()
    update_user(user_id, {"password_hash": hash_password(temp_password), "must_change_password": True})

    users = get_all_users()
    for u in users:
        u["id_str"] = str(u["_id"])
    target = get_user_by_id(user_id)
    return render_template(
        "admin.html",
        users=users,
        new_username=target["username"],
        new_password=temp_password,
        role=session.get("role"),
        username=session.get("username"),
        active_nav="admin",
    )


@app.route("/admin/impersonate/<user_id>", methods=["POST"])
@admin_required
def admin_impersonate(user_id):
    target = get_user_by_id(user_id)
    if not target or not target.get("is_active", True):
        return redirect(url_for("admin_dashboard"))

    log_action(None, "impersonate_start", "Admin",
               details=f"Admin {session['username']} impersonating {target['username']}")

    session["impersonated_by"] = session["user_id"]
    session["impersonated_by_username"] = session["username"]
    session["user_id"] = str(target["_id"])
    session["username"] = target["username"]
    session["role"] = target["role"]
    return redirect(url_for("dashboard"))


@app.route("/admin/stop-impersonating", methods=["GET"])
@login_required
def stop_impersonating():
    if "impersonated_by" not in session:
        return redirect(url_for("dashboard"))

    log_action(None, "impersonate_end", "Admin",
               details=f"Admin {session.get('impersonated_by_username')} stopped impersonating {session.get('username')}")

    session["user_id"] = session.pop("impersonated_by")
    session["username"] = session.pop("impersonated_by_username")
    session["role"] = "admin"
    return redirect(url_for("admin_dashboard"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
