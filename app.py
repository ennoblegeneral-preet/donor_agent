from flask import Flask, render_template, request, redirect, url_for, jsonify, send_file, session, abort
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from logger import logger
import csv
import io
import os
import threading
import uuid
from urllib.parse import urlparse
from datetime import datetime, timezone
from bson import ObjectId
from concurrent.futures import ThreadPoolExecutor, TimeoutError

from db import (
    get_all_companies, get_company, update_company, create_company, delete_company, delete_companies, companies_col,
    create_user, get_user_by_username, get_user_by_id, get_all_users, update_user,
    get_user_zoho_keys, update_user_zoho_keys,
    get_user_search_keys, update_user_search_keys
)
from search_tool import set_search_context, get_effective_search_config
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
from research_agent import research_company_with_financials
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
    default_limits=["200 per day", "50 per hour", "15 per 10 minutes"],
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

# Caps how many companies' pipelines actually run (i.e. fire search/LLM calls) at
# once, regardless of how many were queued via bulk CSV upload. Each pipeline
# thread still starts immediately so progress tracking/UX is unaffected, but
# real work blocks on this until a slot frees up - without it, a bulk upload of
# N companies fires N companies' worth of concurrent Tavily searches at once,
# which blows through Tavily's rate limit even with the per-call semaphore in
# search_tool.py (that one only caps instantaneous concurrency, not sustained rate).
_pipeline_concurrency = threading.Semaphore(10)


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


def run_company_pipeline(company_id, company_name, website, username=None):
    """Run the existing pipeline in the background and enforce strict execution timeout."""
    start_tracking()
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
        _pipeline_concurrency.release()

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
    company_id = create_company(company_name, website, created_by=username)

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
@limiter.limit("2 per minute")
def add_companies_bulk():
    upload = request.files.get("csv_file")
    username = session.get("username")
    if not upload or not upload.filename:
        return jsonify({"status": "error", "message": "Please choose a CSV file to upload."}), 400

    user_search_keys = get_user_search_keys(username) if username else {}
    set_search_context(user_search_keys)
    search_cfg = get_effective_search_config(username)
    if not search_cfg.get("configured") or not search_cfg.get("api_key"):
        return jsonify({
            "status": "error",
            "message": "No Web Search API key added. Please add a Tavily or Serper API key in Settings before running bulk research."
        }), 400

    try:
        raw = upload.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return jsonify({"status": "error", "message": "Could not read the file. Please upload a UTF-8 CSV."}), 400

    reader = csv.DictReader(io.StringIO(raw))
    if not reader.fieldnames or not any(
        (name or "").strip().lower() == "company_name" for name in reader.fieldnames
    ):
        return jsonify({
            "status": "error",
            "message": "CSV must have a 'company_name' column (a 'website' column is optional)."
        }), 400

    field_map = {(name or "").strip().lower(): name for name in reader.fieldnames}
    name_key = field_map["company_name"]
    website_key = field_map.get("website")

    started = []
    seen_names = set()
    skipped = 0

    for row in reader:
        if len(started) >= MAX_BULK_ROWS:
            skipped += 1
            continue

        company_name = (row.get(name_key) or "").strip()
        website = (row.get(website_key) or "").strip() if website_key else ""

        dedupe_key = company_name.lower()
        if not company_name or dedupe_key in seen_names:
            skipped += 1
            continue
        seen_names.add(dedupe_key)

        company_id = create_company(company_name, website or None, created_by=username)
        set_pipeline_progress(company_id, "queued", "Company added. Preparing the research pipeline.", percent=5)
        threading.Thread(
            target=run_company_pipeline, args=(company_id, company_name, website or None, username), daemon=True
        ).start()
        started.append({"company_id": company_id, "company_name": company_name})

    if not started:
        return jsonify({"status": "error", "message": "No valid company names found in the CSV."}), 400

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
        api_key = request.form.get("api_key", "").strip()

        if provider not in ("serper", "tavily"):
            provider = "serper"

        search_keys = {
            "provider": provider,
            "api_key": api_key,
        }
        update_user_search_keys(username, search_keys)
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
    upload_company_to_zoho(company_id, username=username)
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
    return redirect(url_for("dashboard"))


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
        employee_stats.append({
            "username": u["username"],
            "searched": companies_col.count_documents({"created_by": u["username"]}),
            "approved": companies_col.count_documents({"approved_by": u["username"]}),
            "crm_added": companies_col.count_documents({"crm_uploaded_by": u["username"]}),
        })

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
