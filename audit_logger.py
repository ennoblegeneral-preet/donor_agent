from datetime import datetime
from db import audit_log_col

def log_action(company_id: str, action: str, agent_name: str, source: str = None, **kwargs):
    log_doc = {
        "company_id": company_id,
        "action": action,
        "agent_name": agent_name,
        "source": source,
        "timestamp": datetime.utcnow(),
    }
    log_doc.update(kwargs)
    audit_log_col.insert_one(log_doc)
    detail_suffix = f" - {kwargs['details']}" if kwargs.get("details") else ""
    print(f"[Audit Log] {agent_name} logged action '{action}' for company {company_id}{detail_suffix}")

def get_logs_for_company(company_id: str) -> list:
    return list(audit_log_col.find({"company_id": company_id}).sort("timestamp", -1))

def get_all_logs() -> list:
    return list(audit_log_col.find().sort("timestamp", -1))
