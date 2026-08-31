from models import FitmentDecision


def check_csr_relevance(research: dict) -> bool:
    """Company CSR Focus ya Thematic Focus me education/school/child/infra relevance hai?"""
    focus = (research.get("company_csr_focus") or "").lower()
    themes = [t.lower() for t in research.get("thematic_focus", [])]
    keywords = ["education", "school", "child", "infrastructure", "anganwadi", "learning","holistic", "quality education", "school transformation"]
    return any(k in focus for k in keywords) or any(any(k in t for k in keywords) for t in themes)

def check_csr_program_fit(research: dict) -> bool:
    """Check if any of the specific CSR program flags are Yes (all 6 Ennoble programs)"""

    flags = [
        research.get("csr_school_infra_transformation", ""),
        research.get("csr_holistic_transformation", ""),
        research.get("csr_anganwadi_transformation", ""),
        research.get("csr_quality_education", ""),
        research.get("csr_stem_education", ""),              # Fix 3: was missing
        research.get("csr_model_school_transformation", ""),  # Fix 3: was missing
    ]
    return any("yes" in str(f).lower() for f in flags)

def check_ennoble_fit(program_fitment: dict) -> bool:
    """Kam se kam ek program High Fit ya Medium Fit hai?"""
    return any(v in ["High Fit", "Medium Fit"] for v in program_fitment.values())


def check_contact_route(research: dict) -> bool:
    contact = research.get("contact", {})
    has_name = contact.get("first_name", "Not publicly available") != "Not publicly available"
    has_foundation = research.get("has_company_foundation", "").lower() not in ["not found", "no", ""]
    return has_name or has_foundation


def decide_record_stage(research: dict, program_fitment: dict) -> FitmentDecision:
    csr_relevant = check_csr_relevance(research) or check_csr_program_fit(research)
    ennoble_fit = check_ennoble_fit(program_fitment)
    spend_priority = research.get("csr_spend_priority", "Low")
    geo_priority = research.get("geographical_priority", "Low")  # Fix: corrected field name
    contact_route = check_contact_route(research)

    # Fix 2: separate checks — general CSR presence vs education-specific relevance
    has_any_csr = bool(research.get("company_csr_focus")) and research.get("company_csr_focus") != "Not Found"
    has_description = has_any_csr

    checklist = {
        "csr_activity_visible": has_any_csr,           # company has any CSR activity at all
        "education_relevance": csr_relevant,            # CSR is education/school/child-specific
        "ennoble_fit_clear": ennoble_fit,
        "spend_priority_ok": spend_priority in ["High", "Medium"],
        "geography_priority_ok": geo_priority in ["High", "Medium"],
        "source_backed_description": has_description,
        "contact_route_exists": contact_route,
    }

    passed_count = sum(checklist.values())

    # --- Fixed decision rule (guideline ka Table 4) ---
    if not csr_relevant and not ennoble_fit:
        stage = "Disqualified"
        reasoning = "No visible CSR relevance or Ennoble program fit found."
    elif csr_relevant and ennoble_fit and passed_count >= 6:
        stage = "Prospect"
        reasoning = "Strong CSR relevance, clear Ennoble fit, and sufficient funding/geography potential."
    elif csr_relevant and (ennoble_fit or passed_count >= 4):
        stage = "Nurture"
        reasoning = "CSR relevance exists but timing, contact route, or geography fit is weak."
    else:
        stage = "Enriched Data"
        reasoning = "CSR relevance or Ennoble fit is unclear — keeping as researched data for now."

    return FitmentDecision(record_stage=stage, reasoning=reasoning, checklist=checklist)