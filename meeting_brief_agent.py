import json
from datetime import datetime

from models import MeetingBrief
from llm_service import call_llm_safe

# Ennoble's six programs, mapped to the research_json flag field that records
# whether the company already does that kind of work.
_PROGRAM_FLAG_FIELD = {
    "STEM Education": "csr_stem_education",
    "School Infrastructure Transformation": "csr_school_infra_transformation",
    "Holistic School Transformation": "csr_holistic_transformation",
    "Anganwadi Transformation": "csr_anganwadi_transformation",
    "Quality Education": "csr_quality_education",
    "Model School Transformation": "csr_model_school_transformation",
}

_MISSING = {"", "not found", "not publicly available", "none", "n/a", "na", "-"}


def _has_value(v) -> bool:
    return bool(v) and str(v).strip().lower() not in _MISSING


def _clean(v, default="Not available"):
    return str(v).strip() if _has_value(v) else default


def _build_fallback_points(company: dict, research: dict, fitment: dict) -> list:
    """Deterministic talking points straight from the research data - used when
    the LLM is unavailable, so a brief is ALWAYS produced from whatever we have."""
    points = []

    csr_focus = research.get("company_csr_focus")
    if _has_value(csr_focus):
        points.append(f"Their stated CSR focus: {_clean(csr_focus)}")

    themes = research.get("thematic_focus") or []
    themes = [t for t in themes if _has_value(t)]
    if themes:
        points.append(f"Thematic focus areas: {', '.join(themes)}")

    spend = research.get("csr_spend_previous_fy")
    if _has_value(spend):
        points.append(f"Recent annual CSR spend: {_clean(spend)} — sizing the partnership around this")

    edu_spend = research.get("education_csr_spend")
    if _has_value(edu_spend):
        points.append(f"Education CSR spend to date: {_clean(edu_spend)}")

    unspent = research.get("unspent_csr_amount")
    if _has_value(unspent):
        points.append(f"Unspent CSR of {_clean(unspent)} — an immediate deployment opportunity this FY")

    partners = research.get("existing_implementation_partners") or []
    partners = [p for p in partners if _has_value(p)]
    if partners:
        points.append(f"Existing implementation partners: {', '.join(partners)} — position Ennoble as complementary")

    geo = research.get("program_district_state") or research.get("geographical_priority")
    if _has_value(geo):
        points.append(f"Program geography: {_clean(geo)} — confirm overlap with Ennoble's operating states")

    prev = research.get("previous_education_projects")
    if _has_value(prev):
        points.append(f"Past education projects: {_clean(prev)}")

    high_fits = [k for k, v in fitment.items() if v == "High Fit"]
    med_fits = [k for k, v in fitment.items() if v == "Medium Fit"]
    if high_fits:
        points.append(f"Strongest Ennoble program fit(s): {', '.join(high_fits)} — lead the conversation here")
    elif med_fits:
        points.append(f"Possible program fit(s) to explore: {', '.join(med_fits)}")

    # Programs they already run (from the education flags) = warm entry points.
    already = [
        prog for prog, field in _PROGRAM_FLAG_FIELD.items()
        if str(research.get(field, "")).strip().lower() == "yes"
    ]
    if already:
        points.append(f"Already active in: {', '.join(already)} — build on proven interest")

    ticket = research.get("avg_ticket_size")
    if _has_value(ticket):
        points.append(f"Typical CSR ticket size: {_clean(ticket)}")

    contact = research.get("contact", {}) or {}
    name = f"{_clean(contact.get('first_name'), '')} {_clean(contact.get('last_name'), '')}".strip()
    desig = contact.get("designation")
    if name or _has_value(desig):
        who = name or "CSR decision-maker"
        if _has_value(desig):
            who += f" ({_clean(desig)})"
        points.append(f"Key contact to engage: {who}")

    if not points:
        points.append("Limited public CSR data found — use the call to discover CSR budget, focus areas and geography.")

    return points


def generate_meeting_brief(company: dict) -> MeetingBrief:
    """Build a meeting brief (bullet talking points) from the company's research
    data. Tries the LLM for natural, conversational points and falls back to a
    deterministic brief drawn from the same data if the LLM is unavailable."""
    research = company.get("research_json", {}) or {}
    fitment = company.get("program_fitment", {}) or {}
    contact = research.get("contact", {}) or {}

    high_fits = [k for k, v in fitment.items() if v == "High Fit"]

    company_name = _clean(company.get("company_name"), "This company")
    snapshot = (
        f"{company_name} — {_clean(research.get('industry'))}, "
        f"HQ: {_clean(research.get('city'), '-')}, {_clean(research.get('state'), '-')}"
    )
    csr_priorities = _clean(research.get("company_csr_focus"))
    fallback_points = _build_fallback_points(company, research, fitment)
    pitch_angle = (
        f"Strongest program fit: {', '.join(high_fits)}"
        if high_fits else "To be assessed in the discovery call"
    )

    # Everything we know, handed to the LLM so the talking points draw on the
    # FULL research picture rather than a couple of fields.
    brief_data = {
        "company_name": company_name,
        "category_score": company.get("category"),
        "industry": research.get("industry"),
        "hq_city": research.get("city"),
        "hq_state": research.get("state"),
        "csr_focus": research.get("company_csr_focus"),
        "thematic_focus": research.get("thematic_focus"),
        "csr_spend_previous_fy": research.get("csr_spend_previous_fy"),
        "csr_spend_previous_3fy": research.get("csr_spend_previous_3fy"),
        "education_csr_spend": research.get("education_csr_spend"),
        "unspent_csr_amount": research.get("unspent_csr_amount"),
        "avg_ticket_size": research.get("avg_ticket_size"),
        "has_company_foundation": research.get("has_company_foundation"),
        "existing_implementation_partners": research.get("existing_implementation_partners"),
        "program_geography": research.get("program_district_state") or research.get("geographical_priority"),
        "previous_education_projects": research.get("previous_education_projects"),
        "education_program_flags": {
            prog: research.get(field) for prog, field in _PROGRAM_FLAG_FIELD.items()
        },
        "program_fitment": fitment,
        "contact": {
            "name": f"{contact.get('first_name', '')} {contact.get('last_name', '')}".strip(),
            "designation": contact.get("designation"),
        },
    }

    prompt = f"""You are a partnerships lead at Ennoble Social Innovation Foundation, an
education-focused CSR implementation partner in India. Prepare a MEETING BRIEF of crisp
bullet points that our team can actually SPEAK from during a first meeting/call with the
prospect company below.

Use ONLY the research data provided - never invent facts. Where a field says "Not Found"
or is empty, do not fabricate; either skip it or frame it as something to discover on the call.

Ennoble's six programs: STEM Education, School Infrastructure Transformation, Holistic School
Transformation, Anganwadi Transformation, Quality Education, Model School Transformation.

Write in plain, professional Indian English. Each talking point must be one short spoken line
(what the rep would say or raise), specific to THIS company's data. Cover, where data allows:
their CSR focus and budget/spend, unspent CSR opportunity, existing partners, program geography,
which Ennoble program(s) fit best and why, any programs they already run, and the right person to engage.

Research data (JSON):
{json.dumps(brief_data, ensure_ascii=False, default=str)}

Return ONLY valid JSON:
{{
  "company_snapshot": "one-line snapshot of the company",
  "csr_priorities": "one or two lines on their CSR priorities",
  "ennoble_pitch_angle": "the single best angle for Ennoble to lead with",
  "key_talking_points": ["bullet 1", "bullet 2", "bullet 3", "..."]
}}
"""

    data, error = call_llm_safe(prompt, json_mode=True, temperature=0.3, timeout=120)

    if error or not isinstance(data, dict):
        # LLM unavailable - still return a full brief from the deterministic builder.
        return MeetingBrief(
            company_snapshot=snapshot,
            csr_priorities=csr_priorities,
            ennoble_pitch_angle=pitch_angle,
            key_talking_points=fallback_points,
            generated_at=datetime.utcnow().isoformat(),
        )

    points = data.get("key_talking_points")
    if not isinstance(points, list) or not points:
        points = fallback_points
    else:
        points = [str(p).strip() for p in points if str(p).strip()]

    return MeetingBrief(
        company_snapshot=str(data.get("company_snapshot") or snapshot),
        csr_priorities=str(data.get("csr_priorities") or csr_priorities),
        ennoble_pitch_angle=str(data.get("ennoble_pitch_angle") or pitch_angle),
        key_talking_points=points or fallback_points,
        generated_at=datetime.utcnow().isoformat(),
    )
