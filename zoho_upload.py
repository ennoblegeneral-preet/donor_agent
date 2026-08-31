import json
import os
from datetime import datetime

import requests
from dotenv import load_dotenv

from audit_logger import log_action
from crm_mapper import map_to_zoho_lead
from db import get_company, update_company, get_user_zoho_keys

load_dotenv()

CLIENT_ID = os.getenv("ZOHO_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET", "").strip()
REFRESH_TOKEN = os.getenv("ZOHO_REFRESH_TOKEN", "").strip()
API_DOMAIN = os.getenv("ZOHO_API_DOMAIN", "https://www.zohoapis.in").strip()
ACCOUNTS_URL = os.getenv("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.in").strip()


def datetime_now_str():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def get_access_token(user_zoho_keys: dict = None) -> tuple:
    """Generate an OAuth access token using custom user credentials or .env fallback.
    Returns (access_token, api_domain)."""
    keys = user_zoho_keys or {}
    client_id = keys.get("client_id") or CLIENT_ID
    client_secret = keys.get("client_secret") or CLIENT_SECRET
    refresh_token = keys.get("refresh_token") or REFRESH_TOKEN
    accounts_url = keys.get("accounts_url") or ACCOUNTS_URL
    api_domain = keys.get("api_domain") or API_DOMAIN

    credentials = {
        "ZOHO_CLIENT_ID": client_id,
        "ZOHO_CLIENT_SECRET": client_secret,
        "ZOHO_REFRESH_TOKEN": refresh_token,
    }
    missing = [name for name, value in credentials.items() if not value]
    if missing:
        print(f"[Zoho Auth] Missing credentials: {', '.join(missing)}")
        return None, api_domain

    try:
        response = requests.post(
            f"{accounts_url}/oauth/v2/token",
            params={
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
            },
            timeout=10,
        )
        data = response.json()
        if data.get("access_token"):
            return data["access_token"], api_domain
        print(f"[Zoho Auth] Token request failed: {json.dumps(data)}")
    except (requests.RequestException, ValueError) as error:
        print(f"[Zoho Auth] Token request failed: {error}")
    return None, api_domain


def upload_company_to_zoho(company_id: str, username: str = None) -> dict:
    """Upload an eligible company as a record in Zoho CRM's Leads module."""
    company = get_company(company_id, username=username) if username else get_company(company_id)
    if not company:
        return {"status": "error", "message": "Company not found in database"}
    # Tier A and Tier B can be uploaded directly. Tier C still requires approval.
    if company.get("tier") == "Tier C" and company.get("approval_status") != "approved":
        return {"status": "error", "message": "Tier C company must be approved before CRM upload"}

    user_zoho_keys = get_user_zoho_keys(username) if username else {}
    lead_payload = map_to_zoho_lead(company)
    access_token, target_api_domain = get_access_token(user_zoho_keys)

    if not access_token:
        print(f"[Zoho Simulation] Uploading Lead for user '{username}': {json.dumps(lead_payload, indent=2)}")
        update_company(company_id, {
            "upload_status": "uploaded",
            "zoho_lead_id": "SIM_LEAD_12345",
            "zoho_uploaded_at": datetime_now_str(),
        }, username=username)
        log_action(company_id, "crm_upload_simulated", "ZohoUploadHandler", details="Simulated Lead upload.")
        return {"status": "simulated", "message": "Simulated Lead upload complete"}

    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    lead_id = None
    try:
        search_response = requests.get(
            f"{target_api_domain}/crm/v6/Leads/search",
            headers=headers,
            params={"word": company["company_name"]},
            timeout=15,
        )
        if search_response.status_code == 200:
            records = search_response.json().get("data") or []
            lead_id = next((record["id"] for record in records if record.get("Company") == company["company_name"]), None)
    except (requests.RequestException, ValueError) as error:
        print(f"[Zoho] Lead duplicate check warning: {error}")

    if lead_id:
        try:
            update_payload = dict(lead_payload)
            update_payload["id"] = lead_id
            response = requests.put(
                f"{API_DOMAIN}/crm/v6/Leads",
                headers=headers,
                json={"data": [update_payload]},
                timeout=15,
            )
            response_data = response.json()
            result = (response_data.get("data") or [{}])[0]
            if result.get("status") != "success":
                raise RuntimeError(f"HTTP {response.status_code}: {json.dumps(response_data)}")
            print(f"[Zoho] Updated Lead '{company['company_name']}' with ID: {lead_id}")
        except (requests.RequestException, ValueError, KeyError, RuntimeError) as error:
            message = f"Failed to update Lead: {error}"
            print(f"[Zoho] {message}")
            update_company(company_id, {"upload_status": "failed"})
            log_action(company_id, "crm_upload_failed", "ZohoUploadHandler", details=message)
            return {"status": "error", "message": message}
    else:
        try:
            response = requests.post(
                f"{API_DOMAIN}/crm/v6/Leads",
                headers=headers,
                json={"data": [lead_payload]},
                timeout=15,
            )
            response_data = response.json()
            result = (response_data.get("data") or [{}])[0]
            if result.get("status") != "success":
                raise RuntimeError(f"HTTP {response.status_code}: {json.dumps(response_data)}")
            lead_id = result["details"]["id"]
            print(f"[Zoho] Created Lead '{company['company_name']}' with ID: {lead_id}")
        except (requests.RequestException, ValueError, KeyError, RuntimeError) as error:
            message = f"Failed to create Lead: {error}"
            print(f"[Zoho] {message}")
            update_company(company_id, {"upload_status": "failed"})
            log_action(company_id, "crm_upload_failed", "ZohoUploadHandler", details=message)
            return {"status": "error", "message": message}

    update_company(company_id, {
        "upload_status": "uploaded",
        "zoho_lead_id": lead_id,
        "zoho_uploaded_at": datetime_now_str(),
    })
    log_action(company_id, "crm_uploaded", "ZohoUploadHandler", details="Uploaded Lead.")
    return {"status": "success", "message": "Lead uploaded successfully"}  


    
