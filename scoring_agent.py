import requests
import json
import os
from dotenv import load_dotenv
from db import get_company, update_company
from audit_logger import log_action
from fitment_agents import decide_record_stage
from error_utils import classify_error
from llm_service import call_llm_safe
load_dotenv()

WEIGHTS = {
    "education_focus": 30,
    "spend_capacity": 30,
    "geography_match": 20,
    "strategic_fit": 20,
    "decision_maker_access": 0,
    "urgency_signal": 0,
    "governance_quality": 0,
    "warm_connection": 0,
}


# WEIGHTS_NO_FINANCIALS = {
#     "education_focus": 60,   # Absorbs spend_capacity weight
#     "spend_capacity": 0,
#     "geography_match": 20,
#     "strategic_fit": 20,
# }

PROGRAMS = [
    "STEM Education",
    "School Infrastructure Transformation",
    "Holistic School Transformation",
    "Anganwadi Transformation",
    "Quality Education",
    "Model School Transformation"
]

def rate_scoring_and_fitment(research_json: dict):
    """
    Evaluates both scoring factors (0.0 to 1.0) and Ennoble program fitments
    in a SINGLE unified LLM call, reducing LLM token consumption by ~50% and doubling speed.
    Returns (ratings_dict, program_fit_dict, error_or_None).
    """
    fallback = compute_fallback_program_fitment(research_json)
    prompt = f"""You are Ennoble CSR Lead Scoring Agent. Based ONLY on the research data below, evaluate both
the scoring factors and the program fitments for this company.

STRICT GROUNDING RULES:
- Base every rating and fitment strictly on the research data provided below. Do not guess.
- If data for a factor is "Not Found" or missing, rate 0.0-0.2 and state so in the reason (e.g. "No spend data found").
- Only rate above 0.5 if the data explicitly supports it.
- Geography match rule: High (3+ states/pan-India) -> 0.8-1.0; Medium (2 states) -> 0.4-0.6; Low/empty -> 0.0-0.2.
- Program Fitment: Mark High Fit, Medium Fit, Low Fit, or Not Evident for each program.

Research Data:
{json.dumps(research_json)}

Return ONLY a JSON object with this exact structure:
{{
  "factors": {{
    "education_focus": {{"rating": 0.0, "reason": "..."}},
    "spend_capacity": {{"rating": 0.0, "reason": "..."}},
    "geography_match": {{"rating": 0.0, "reason": "..."}},
    "strategic_fit": {{"rating": 0.0, "reason": "..."}},
    "decision_maker_access": {{"rating": 0.0, "reason": "..."}},
    "urgency_signal": {{"rating": 0.0, "reason": "..."}},
    "governance_quality": {{"rating": 0.0, "reason": "..."}},
    "warm_connection": {{"rating": 0.0, "reason": "..."}}
  }},
  "program_fitment": {{
    "STEM Education": "High Fit / Medium Fit / Low Fit / Not Evident",
    "School Infrastructure Transformation": "High Fit / Medium Fit / Low Fit / Not Evident",
    "Holistic School Transformation": "High Fit / Medium Fit / Low Fit / Not Evident",
    "Anganwadi Transformation": "High Fit / Medium Fit / Low Fit / Not Evident",
    "Quality Education": "High Fit / Medium Fit / Low Fit / Not Evident",
    "Model School Transformation": "High Fit / Medium Fit / Low Fit / Not Evident"
  }}
}}
"""
    data, err = call_llm_safe(prompt, json_mode=True, timeout=300)
    if err or not isinstance(data, dict):
        return {}, fallback, err

    ratings = data.get("factors") if isinstance(data.get("factors"), dict) else {}
    program_fit = data.get("program_fitment") if isinstance(data.get("program_fitment"), dict) else {}

    # Merge fallback for any missing or Not Evident program fits
    for k, v in fallback.items():
        if k not in program_fit or program_fit[k] == "Not Evident":
            program_fit[k] = v

    return ratings, program_fit, None


def rate_factors(research_json: dict):
    """Backward-compatible wrapper."""
    ratings, _, err = rate_scoring_and_fitment(research_json)
    return ratings, err


_NOT_FOUND_VALUES = {"", "not found", "not publicly available", "none", "n/a", "na"}

# Each Ennoble program's dedicated Yes/No CSR flag from extraction, when present, is a
# far more reliable fitment signal than keyword-guessing free text.
_PROGRAM_FLAG_FIELD = {
    "STEM Education": "csr_stem_education",
    "School Infrastructure Transformation": "csr_school_infra_transformation",
    "Holistic School Transformation": "csr_holistic_transformation",
    "Anganwadi Transformation": "csr_anganwadi_transformation",
    "Quality Education": "csr_quality_education",
    "Model School Transformation": "csr_model_school_transformation",
}

_PROGRAM_KEYWORDS = {
    "STEM Education": (
        # High-signal: specific terms for STEM/digital interventions
        ["stem", "science lab", "digital learning", "computer lab", "coding", "robotics", "digital literacy",
         "smart class", "ai lab", "ict", "information technology education", "computer education",
         "skill development", "digital education", "technology education", "maker lab", "innovation center",
         "tinkering lab", "atal tinkering", "steam", "e-learning", "digital classroom"],
        # Medium-signal: broader tech/education overlap
        ["technology", "innovation", "computer", "science", "digital", "tech", "software", "it skills",
         "vocational training", "employability", "skilling", "digital india"]
    ),
    "School Infrastructure Transformation": (
        ["school infrastructure", "sanitation", "classroom renovation", "school building", "drinking water",
         "toilets", "school construction", "school facility", "school renovation", "washroom",
         "water facility", "hygiene", "school maintenance", "school development", "school support"],
        ["renovation", "water", "building", "solar school", "infrastructure", "school", "facility",
         "construction", "repair", "upgrade"]
    ),
    "Holistic School Transformation": (
        ["holistic school", "whole school transformation", "school transformation", "school development",
         "adopt school", "school adoption", "school improvement", "model government school",
         "comprehensive school", "school excellence", "school reform"],
        ["school development", "holistic development", "child development", "school program",
         "school initiative", "school support", "education program", "student development"]
    ),
    "Anganwadi Transformation": (
        ["anganwadi", "early childhood", "preschool", "maternal health", "balwadi", "early learning",
         "pre-primary", "icds", "child care center", "creche", "early education"],
        ["nutrition", "child care", "pre-primary", "maternal", "infant", "toddler", "0-6 years"]
    ),
    "Quality Education": (
        ["quality education", "teacher training", "learning outcome", "literacy program", "scholarship",
         "foundational learning", "remedial education", "education support", "school program",
         "student support", "academic support", "education initiative", "school children",
         "primary education", "secondary education", "education access", "girl education",
         "dropout", "enrolment", "reading program", "numeracy", "education ngo"],
        ["learning", "education", "literacy", "scholarship", "schools", "students", "academic",
         "teachers", "children", "community learning", "social education", "welfare"]
    ),
    "Model School Transformation": (
        ["model school", "district-level education", "district level education", "government school upgrade",
         "hub school", "cluster school", "school of excellence", "benchmark school"],
        ["state-wide education", "district", "cluster", "government school", "school upgrade"]
    ),
}


def compute_fallback_program_fitment(research_json: dict) -> dict:
    """Only used to fill in gaps the LLM left as 'Not Evident' (or skipped). Must judge
    strictly on field VALUES - never on json.dumps() of the whole dict, whose own key
    names (e.g. 'csr_stem_education') contain these same trigger words regardless of
    what was actually found, which used to make every program come back as a false
    High/Medium Fit even for a completely empty record."""

    # Free text drawn only from values relevant to program fit - never field names.
    free_text_parts = [
        research_json.get("company_csr_focus", ""),
        " ".join(research_json.get("thematic_focus", []) or []),
        research_json.get("previous_education_projects", ""),
        research_json.get("program_district_state", ""),
        " ".join(research_json.get("existing_implementation_partners", []) or []),
        research_json.get("duration_past_projects", ""),
    ]
    text = " ".join(
        str(part) for part in free_text_parts
        if part and str(part).strip().lower() not in _NOT_FOUND_VALUES
    ).lower()

    result = {}
    for program, flag_field in _PROGRAM_FLAG_FIELD.items():
        flag_value = str(research_json.get(flag_field, "") or "").strip().lower()
        if flag_value == "yes":
            result[program] = "High Fit"
            continue
        if flag_value == "no":
            result[program] = "Low Fit"
            continue

        high_keywords, medium_keywords = _PROGRAM_KEYWORDS[program]
        if any(k in text for k in high_keywords):
            result[program] = "High Fit"
        elif any(k in text for k in medium_keywords):
            result[program] = "Medium Fit"
        else:
            result[program] = "Not Evident"

    return result


def rate_program_fitment(research_json: dict):
    """Returns (fitment_dict, error_or_None). error set only when the LLM call itself
    failed - the fallback keyword-based fitment is still returned/used either way."""
    fallback = compute_fallback_program_fitment(research_json)
    prompt = f"""Based ONLY on this company's CSR focus, thematic focus, past education projects, and spending
as given in the Research Data below - do not use prior/general knowledge about this company from
training - determine fitment (High Fit, Medium Fit, Low Fit, or Not Evident) for each Ennoble program.
If the research data has no evidence at all for a program, mark it "Not Evident" rather than guessing.

- STEM Education: Is there a STEM, science labs, computer labs, robotics, coding, or digital learning fit?
- School Infrastructure Transformation: Is there school infrastructure, sanitation, toilets, drinking water, classrooms, or school building relevance?
- Holistic School Transformation: Is there scope for whole-school development, school adoption, or comprehensive school transformation?
- Anganwadi Transformation: Is there early childhood education, Anganwadi center, pre-school, or maternal/child nutrition relevance?
- Quality Education: Is there general education, quality education, teacher training, scholarships, literacy, or learning outcome relevance?
- Model School Transformation: Is there a possibility for a model school, government school upgrade, or district/cluster-level approach?

Research Data:
{json.dumps(research_json)}

Return ONLY JSON in this format:
{{
  "STEM Education": "High Fit / Medium Fit / Low Fit / Not Evident",
  "School Infrastructure Transformation": "High Fit / Medium Fit / Low Fit / Not Evident",
  "Holistic School Transformation": "High Fit / Medium Fit / Low Fit / Not Evident",
  "Anganwadi Transformation": "High Fit / Medium Fit / Low Fit / Not Evident",
  "Quality Education": "High Fit / Medium Fit / Low Fit / Not Evident",
  "Model School Transformation": "High Fit / Medium Fit / Low Fit / Not Evident"
}}
"""
    data, err = call_llm_safe(prompt, json_mode=True, timeout=60)
    if err:
        return fallback, err
    # Merge with fallback if any key is missing or Not Evident
    for k, v in fallback.items():
        if k not in data or data[k] == "Not Evident":
            data[k] = v
    return data, None


def calculate_final_score(ratings: dict) -> int:
    total = 0
    for factor, weight in WEIGHTS.items():
        rating = ratings.get(factor, {}).get("rating", 0)
        total += rating * weight
    return round(total)


import re

# Ordering used to pick the single strongest program fit (prompt: "Ennoble Fitment").
_FIT_RANK = {"High Fit": 3, "Medium Fit": 2, "Low Fit": 1, "Not Evident": 0}


def parse_crore(value: str):
    """Best-effort convert a rupee amount string to a value in crore.
    Handles '₹5.2 Cr', 'Rs. 12 crore', '50 lakh', '1,20,00,000'. Returns None if unknown."""
    if not value or not isinstance(value, str):
        return None
    text = value.lower().replace(",", "").strip()
    if text in ("not found", "not publicly available", "none", "n/a", ""):
        return None
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    num = float(match.group(1))
    if "cr" in text:            # crore
        return num
    if "lakh" in text or "lac" in text:
        return num / 100.0
    # Bare number: assume rupees, convert to crore
    return num / 1e7


def best_program_fit(program_fit: dict) -> str:
    """Strongest fit label across the 6 Ennoble programs -> the 'Ennoble Fitment' value."""
    labels = [v for k, v in program_fit.items() if k != "Ennoble Fitment"]
    if not labels:
        return "Not Evident"
    return max(labels, key=lambda v: _FIT_RANK.get(v, 0))


def best_program_name(program_fit: dict):
    """Name of the program with the strongest fit, for human-readable reasoning."""
    labels = {k: v for k, v in program_fit.items() if k != "Ennoble Fitment"}
    if not labels:
        return None
    return max(labels, key=lambda k: _FIT_RANK.get(labels[k], 0))


# Tier A/B/C bands over the 0-100 weighted score (calculate_final_score).
# Contiguous, no gaps: A >= 65, B 30-64, C < 30.
TIER_A_MIN = 65
TIER_B_MIN = 30


def assign_category(final_score: int):
    """Tier A/B/C purely from the 0-100 weighted score.

    Bands (contiguous, gap-free):
        Tier A: score >= 65
        Tier B: 30 <= score <= 64
        Tier C: score < 30

    Returns (category, reasoning) so the score that drove the tier is shown."""
    score = final_score if final_score is not None else 0

    if score >= TIER_A_MIN:
        return "Tier A", f"Score {score}/100 is >= {TIER_A_MIN}, so it clears the Tier A bar."
    if score >= TIER_B_MIN:
        return "Tier B", f"Score {score}/100 is in the {TIER_B_MIN}-{TIER_A_MIN - 1} range, so it's Tier B."
    return "Tier C", f"Score {score}/100 is below {TIER_B_MIN}, so it's Tier C."


def score_company(company_id: str):
    company = get_company(company_id)
    research = company.get("research_json", {})
    # One unified LLM call returns both factor ratings and program fitment.
    # Reusing both results avoids sending the same research JSON through a
    # second, duplicate fitment call.
    ratings, program_fit, scoring_error = rate_scoring_and_fitment(research)

    # Hard financial prospect check (financial_extractor.check_prospect_criteria,
    # OR logic over turnover/net worth/net profit) overrides the LLM's qualitative
    # spend_capacity guess with full marks when it's met - real Screener numbers
    # beat a text-based guess. When it's not met, the LLM rating stands as-is.
    if company.get("is_prospect"):
        ratings = dict(ratings)
        ratings["spend_capacity"] = {
            "rating": 1.0,
            "reason": "Meets the hard financial prospect criteria (net worth >= Rs 500 Cr AND net profit >= "
                      "Rs 5 Cr, OR turnover >= Rs 1000 Cr) from Screener data - full marks awarded over the "
                      "qualitative signal.",
        }

    # # CSR capacity gate: comment out this block when this rule is not needed.
    # # Both the PBT-based and Net-Profit-based 2% CSR capacity calculations must
    # # be at least Rs 3 Cr for the company to receive spend-capacity points.
    # pbt_capacity = (company.get("csr_budget") or {}).get("csr_budget_2pct")
    # net_profit_capacity = (company.get("csr_budget_net_profit") or {}).get(
    #     "csr_budget_2pct_net_profit"
    # )
    # ratings = dict(ratings)
    # if (
    #     pbt_capacity is None
    #     or net_profit_capacity is None
    #     or pbt_capacity < 3
    #     or net_profit_capacity < 3
    # ):
    #     ratings["spend_capacity"] = {
    #         "rating": 0.0,
    #         "reason": "PBT-based and Net-Profit-based CSR capacity must both be at least Rs 3 Cr.",
    #     }
    # else:
    #     ratings["spend_capacity"] = {
    #         "rating": 1.0,
    #         "reason": "Both PBT-based and Net-Profit-based CSR capacity calculations are at least Rs 3 Cr.",
    #     }

    # Weighted 0-100 score; ye ab tier (A/B/C) bhi decide karta hai.
    # NOTE: agar ratings_error set hai (jaise LLM rate-limited), to ratings={} aur
    # final_score 0 aa sakta hai - jo ek REAL 0-score se distinguish nahi hota
    # sirf final_score dekh ke. Isliye ye error ko alag se save karte hain, taaki
    # "Tier C" dikhne wali company actually rate-limit ki wajah se unrated ho, is
    # baat ka pata frontend/reviewer ko chal sake.
    final_score = calculate_final_score(ratings)

    # Ennoble Fitment = strongest overall program fit (prompt + guidelines).
    program_fit["Ennoble Fitment"] = best_program_fit(program_fit)

    # A high general CSR score must not promote a company that has no usable
    # fit with any Ennoble programme. Keep the numeric score for diagnostics,
    # but remove it from Tier A/B/C and label it explicitly as not a fit.
    overall_fit = program_fit["Ennoble Fitment"]
    if overall_fit in ("High Fit", "Medium Fit"):
        category, tier_reasoning = assign_category(final_score)
    else:
        category = "Not a Fit"
        tier_reasoning = (
            f"General CSR score is {final_score}/100, but overall Ennoble fitment is "
            f"{overall_fit}; the company is excluded from Tier A/B/C."
        )

    # Guidelines fit-check: decide the record stage (Prospect / Nurture / Enriched Data / Disqualified).
    decision = decide_record_stage(research, program_fit)

    # Pehla error jo mila (factor rating ya program fitment) - scoring_error field
    # mein save karte hain taaki caller/frontend "yeh score API failure ki wajah
    # se incomplete hai" dikha sake, "genuinely low score" ki jagah.
    update_company(company_id, {
        "score": final_score,               # internal hint only
        "tier": category,                   # kept for backward compatibility with table/warm-connect
        "category": category,
        "tier_reasoning": tier_reasoning,
        "score_reasoning": ratings,
        "program_fitment": program_fit,
        "record_stage": decision.record_stage,
        "fitment_reasoning": decision.reasoning,
        "fitment_checklist": decision.checklist,
        "status": "scored",
        "scoring_error": scoring_error,
    })

    log_action(company_id, "scoring_completed", "ScoringAgent",
               details=f"stage={decision.record_stage}; category={category}; score={final_score}"
                       + (f"; WARNING: {scoring_error['message']}" if scoring_error else ""))

    return {
        "score": final_score,
        "tier": category,
        "category": category,
        "tier_reasoning": tier_reasoning,
        "record_stage": decision.record_stage,
        "reasoning": ratings,
        "program_fitment": program_fit,
        "scoring_error": scoring_error,
    }

