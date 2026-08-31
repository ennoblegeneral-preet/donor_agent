from models import CompanyResearch
from db import update_company
from audit_logger import log_action

def check_compliance(company_id: str, research: CompanyResearch) -> dict:
    issues = []

    if research.source_url == "Not Found" or not research.source_url:
        issues.append("Missing source URL")

    if research.education_csr_spend == "Not Found":
        issues.append("Education CSR spend not verified")

    confidence = "verified" if not issues else "unverified"
    # Per Lead Fitment Guidelines: weak/unverified signals should NOT be hard-dropped here.
    # The fit-check (decide_record_stage) routes such leads to Nurture / Enriched Data / Disqualified.
    blocked = False

    update_company(company_id, {
        "status": "compliance_checked",
        "compliance_issues": issues,
        "compliance_blocked": blocked
    })

    log_action(company_id, "compliance_checked", "ComplianceAgent",
               source=None)

    return {"confidence": confidence, "issues": issues, "blocked": blocked}