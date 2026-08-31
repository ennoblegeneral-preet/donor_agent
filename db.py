import certifi
import re
from pymongo import MongoClient
from datetime import datetime
import os
from dotenv import load_dotenv
from bson import ObjectId

load_dotenv()

client = MongoClient(os.getenv("MONGODB_URI"), tlsCAFile=certifi.where())
db = client["donor_agent"]

# Legacy flat collection (kept for backward compat with audit_logger etc.)
_legacy_companies_col = db["companies"]
companies_col = _legacy_companies_col  # alias used by audit_logger

audit_log_col = db["audit_log"]
users_col = db["users"]


def _safe_collection_name(username: str) -> str:
    """Convert username to a safe MongoDB collection name.
    e.g. 'john.doe' -> 'companies_john_doe'
    """
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", username.strip().lower())
    return f"companies_{safe}"


def _user_col(username: str):
    """Return the MongoDB collection for a specific user's companies."""
    return db[_safe_collection_name(username)]


try:
    users_col.create_index("username", unique=True)
except Exception as e:
    print(f"[MongoDB Warning] Could not connect to database or create index: {e}")

def create_user(username: str, password_hash: str, role: str = "user", must_change_password: bool = True) -> str:
    doc = {
        "username": username,
        "password_hash": password_hash,
        "role": role,
        "must_change_password": must_change_password,
        "is_active": True,
        "created_at": datetime.utcnow(),
        "last_login": None,
    }
    result = users_col.insert_one(doc)
    return str(result.inserted_id)


def get_user_by_username(username: str) -> dict:
    return users_col.find_one({"username": username})


def get_user_by_id(user_id: str) -> dict:
    from bson import ObjectId
    return users_col.find_one({"_id": ObjectId(user_id)})


def get_all_users() -> list:
    return list(users_col.find().sort("created_at", -1))


def update_user(user_id: str, updates: dict):
    from bson import ObjectId
    users_col.update_one({"_id": ObjectId(user_id)}, {"$set": updates})


def get_user_zoho_keys(username: str) -> dict:
    """Retrieve custom per-user Zoho API keys from MongoDB."""
    if not username:
        return {}
    user = get_user_by_username(username) or {}
    return user.get("zoho_keys") or {}


def update_user_zoho_keys(username: str, zoho_keys: dict):
    """Save custom per-user Zoho API keys to MongoDB."""
    if not username:
        return
    users_col.update_one(
        {"username": username},
        {"$set": {"zoho_keys": zoho_keys}}
    )


def get_user_search_keys(username: str) -> dict:
    """Retrieve custom per-user Web Search API keys from MongoDB."""
    if not username:
        return {}
    user = get_user_by_username(username) or {}
    return user.get("search_keys") or {}


def update_user_search_keys(username: str, search_keys: dict):
    """Save custom per-user Web Search API keys to MongoDB."""
    if not username:
        return
    users_col.update_one(
        {"username": username},
        {"$set": {"search_keys": search_keys}}
    )



# ─────────────────────────────────────────────
# Company functions — per-user collections
# ─────────────────────────────────────────────
def _create_user_col_index(col):
    try:
        col.create_index("company_name", unique=True)
    except Exception:
        pass


def create_company(company_name: str, website: str = None, created_by: str = None) -> str:
    """
    Insert company into the user's own collection: companies_<username>.
    If already exists in that user's collection, return its existing ID.
    """
    col = _user_col(created_by) if created_by else _legacy_companies_col
    _create_user_col_index(col)

    existing = col.find_one({"company_name": company_name})
    if existing:
        return str(existing["_id"])

    doc = {
        "company_name": company_name,
        "website": website,
        "status": "new",
        "research_json": None,
        "contacts_json": [],
        "scoring": None,
        "crm": {
            "record_stage": "New",
            "lead_owner": created_by,
            "lead_source": "AI Research Agent",
            "lead_status": "open - not contacted",
            "next_followups_date": None,
            "immediate_action": None,
            "description": None,
            "decision_maker_name" : None,
            "decision_maker_phone" : None,
            "decision_maker_email" : None,
        },
        "approval_status": "pending",
        "upload_status": "not_uploaded",
        "created_at": datetime.utcnow(),
        "created_by": created_by,
    }
    result = col.insert_one(doc)
    return str(result.inserted_id)


def _find_company_col(company_id: str):
    """
    Search through all per-user company collections (companies_*) and the
    legacy flat collection to find which one holds this company_id.
    Returns (collection, document) or (None, None).
    """
    oid = ObjectId(company_id)
    for col_name in db.list_collection_names():
        if col_name.startswith("companies_"):
            col = db[col_name]
            doc = col.find_one({"_id": oid})
            if doc:
                return col, doc
    # Fallback: legacy flat companies collection
    doc = _legacy_companies_col.find_one({"_id": oid})
    if doc:
        return _legacy_companies_col, doc
    return None, None


def get_company(company_id: str) -> dict:
    """Find a company by ID across all user collections."""
    _, doc = _find_company_col(company_id)
    return doc


def update_company(company_id: str, updates: dict):
    """Update a company in whichever user collection it belongs to."""
    col, _ = _find_company_col(company_id)
    if col is not None:
        col.update_one({"_id": ObjectId(company_id)}, {"$set": updates})


def delete_company(company_id: str):
    """Delete a company from whichever user collection it belongs to."""
    col, _ = _find_company_col(company_id)
    if col is not None:
        col.delete_one({"_id": ObjectId(company_id)})


def delete_companies(company_ids: list) -> int:
    """Delete many companies at once. Returns how many were actually deleted.
    Each id is resolved to its own collection (same as delete_company), so this
    works across the per-user collections. Bad/duplicate ids are skipped safely."""
    deleted = 0
    for company_id in company_ids or []:
        try:
            col, doc = _find_company_col(company_id)
            if col is not None and doc is not None:
                col.delete_one({"_id": ObjectId(company_id)})
                deleted += 1
        except Exception:
            # invalid ObjectId / already gone - skip, keep deleting the rest
            continue
    return deleted


def get_all_companies(username: str = None, role: str = None) -> list:
    """
    Admin: returns all companies merged from ALL user collections.
    Regular user: returns only companies from their own collection (companies_<username>).
    """
    if role == "admin":
        all_companies = []
        for col_name in sorted(db.list_collection_names()):
            if col_name.startswith("companies_"):
                all_companies.extend(list(db[col_name].find()))
        # Include legacy unassigned companies
        all_companies.extend(list(_legacy_companies_col.find()))
        all_companies.sort(key=lambda c: c.get("created_at", datetime.min), reverse=True)
        return all_companies

    if username:
        return list(_user_col(username).find().sort("created_at", -1))

    return []


def get_tier_a_companies(username: str = None, role: str = None) -> list:
    query = {"score": {"$gte": 85}, "status": "scored"}
    if role == "admin":
        results = []
        for col_name in db.list_collection_names():
            if col_name.startswith("companies_"):
                results.extend(list(db[col_name].find(query)))
        results.extend(list(_legacy_companies_col.find(query)))
        return results
    if username:
        return list(_user_col(username).find(query))
    return []

