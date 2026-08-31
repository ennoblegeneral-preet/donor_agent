import requests
import json
import os
from dotenv import load_dotenv
from models import CompanyResearch
from error_utils import classify_error
from llm_service import call_llm_safe

load_dotenv()

def _normalize_optional_text(value):
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned.lower() in {"", "not found", "none", "na", "n/a"}:
            return None
        return cleaned
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else str(value)
    return str(value)

RELEVANT_KEYWORDS = {
    # CSR & Education Programs
    "csr", "education", "schools", "school", "foundation", "social responsibility", "community",
    "anganwadi", "stem", "steam", "maker stem", "infrastructure", "transformation",
    "water", "sanitation", "hygiene", "wash", "drinking water",
    "digital literacy", "computer education", "education technology", "e-learning",
    "corporate citizenship", "community development", "social impact", "sustainable development",
    "csr report", "annual report", "sustainability report", "brsr", "esg",
    # Financials & Spend
    "crore", "lakh", "turnover", "revenue", "profit", "budget", "spend", "spent",
    "unspent", "financial year", "fy", "ticket size", "total csr expenditure",
    # Contact & Leadership
    "contact", "email", "phone", "mobile", "director", "head", "officer", "manager", "trustee", "linkedin"
}

def filter_relevant_text(raw_text: str, max_chars: int = 3500) -> str:
    """Extracts only high-signal sentences/paragraphs related to CSR, financials, and contacts,
    discarding website boilerplate, navbars, and legal disclaimers."""
    if not raw_text or len(raw_text) <= 500:
        return raw_text or ""

    paragraphs = [p.strip() for p in raw_text.split("\n") if len(p.strip()) > 25]
    selected_paragraphs = []
    seen = set()
    total_len = 0

    for p in paragraphs:
        p_lower = p.lower()
        if any(keyword in p_lower for keyword in RELEVANT_KEYWORDS):
            snippet_hash = p_lower[:60]
            if snippet_hash in seen:
                continue
            seen.add(snippet_hash)

            selected_paragraphs.append(p)
            total_len += len(p)
            if total_len >= max_chars:
                break

    if not selected_paragraphs:
        return raw_text[:1500]

    return "\n\n".join(selected_paragraphs)


def extract_research_with_contact(company_name: str, sources: list):
    """Returns (CompanyResearch, error_or_None). error is set only when the LLM API
    call itself failed (network/auth/rate-limit/etc) - not when the model simply
    returned sparse "Not Found" fields, so callers can tell "the extraction service
    broke" apart from "genuinely nothing to extract"."""
    openai_key = os.getenv("OPENAI_API_KEY")
    source_text_char_limit = 100000 if openai_key else 38000

    combined_text = ""
    sorted_sources = sorted(sources, key=lambda s: s.get("priority", 99))[:10]
    for s in sorted_sources:
        raw_source_text = s.get("text", "")
        clean_text = filter_relevant_text(raw_source_text, max_chars=8000)
        if clean_text:
            combined_text += f"\n\n--- SOURCE ({s.get('source_type', 'Web')}, Priority {s.get('priority', 1)}): {s.get('url', '')} ---\n{clean_text}"
            if len(combined_text) >= 38000:
                combined_text = combined_text[:38000]
                break

    prompt = f"""You are Ennoble CSR Partner Research GPT. Research "{company_name}" using ONLY
the text excerpts provided below (from multiple sources, priority-ordered).

STRICT RULES:
- Use only information present in the excerpts below. Do not fabricate data.
- If a field is unavailable, write "Not publicly available".
- If a value is estimated rather than directly stated, write "Estimated from available disclosures".
- Never guess mobile numbers, phone numbers, or email IDs — only use what's explicitly written in the text.

RESEARCH CHECKLIST (per the Ennoble Lead Fitment Guidelines - check each explicitly, do not skip):
- Company CSR Focus: Is CSR work visible? Is education/community development included?
- Thematic Focus: Does the theme match education, school, child, infrastructure, Anganwadi, or quality education?
- CSR Spent of Previous Financial Year: Is there visible CSR funding potential?
- CSR Spent of Previous 3 Financial Years: Is there consistent CSR spending over time (look for a 3-year trend, not just the latest year)?
- Education CSR Spend: Has the company spent specifically on education?
- Unspent CSR Amount: Is there a possible unused/carried-forward CSR budget?
- Previous Projects on Education: Has the company supported education-related projects earlier, and for how long?
- Company Foundation: Is there a foundation route for partnership?
- Existing Implementation Partners: Does the company already work with NGOs/implementation partners? Name them and count them if disclosed.
- Geography: Where does the company's CSR work actually happen? Look for city/state HQ AND the
  district(s)/state(s) where CSR programs run or beneficiaries are located (these can differ from
  the HQ). If multiple states/districts are named for CSR program activity, that is a WIDER
  geographic footprint - note this explicitly, since it directly sets "geographical_priority" below.

GEOGRAPHY FIELD RULES:
- "city"/"state": company HQ city/state, OR the CSR-relevant operating city/state if that's what the text discusses.
- "program_district_state": the specific district(s)/state(s) named for CSR program activity/beneficiaries
  (e.g. "Pune, Maharashtra; Bengaluru, Karnataka"). List all named locations, semicolon-separated.
- "geographical_priority": based purely on breadth of CSR presence found in the text (NOT compared to
  any specific target region):
    "High"   = CSR programs are named across 3+ distinct states/districts, OR described as pan-India/nationwide.
    "Medium" = CSR programs are named in 2 states/districts.
    "Low"    = only 1 location is named, or no program-location information is found at all.

CONTACT PERSON RULES:
- Identify and extract the best available key decision maker for the company.
- Priority Order:
  1. CSR Lead / Head of CSR / CSR Manager / Foundation Head / CSR Officer
  2. Sustainability Lead / Chief Sustainability Officer (CSO) / ESG Head
  3. CSR Committee Member / Board Member / Trustee
  4. CEO / Managing Director (MD) / Founder / President / Executive Director
  5. Chief Human Resources Officer (CHRO) / HR Head / VP HR / Director HR
  6. Any senior Director / Key Management Personnel listed in public disclosures
- If no dedicated CSR head is mentioned, ALWAYS extract the CEO, Managing Director, Founder, or HR Head.
- Priority order for contact sources: Company Website/Leadership page > Annual Report/CSR Report/BRSR > LinkedIn > Registry (Zaubacorp/Tofler) > Media.
- LinkedIn may be used for: name, current designation, company association, and linkedin_url.
- NEVER fabricate email or phone numbers. If not explicitly stated, set to "Not publicly available".
- Always note in "source" which type of source confirmed the contact (e.g. "LinkedIn", "Annual Report", "Company Website").

Return ONLY valid JSON in this exact structure, nothing else:
{{
  "company_name": "{company_name}",
  "industry": "...",
  "city": "...",
  "state": "...",
  "company_csr_focus": "...",
  "thematic_focus": [],
  "geographical_priority": "...",
  "program_district_state": "...",
  "csr_spend_previous_fy": "...",
  "csr_spend_previous_3fy": "...",
  "education_csr_spend": "...",
  "has_company_foundation": "...",
  "existing_implementation_partners": ["Extract all named NGO partners, implementation agencies, non-profit foundations, or execution partners mentioned for the company's CSR projects"],
  "num_implementation_partners": null,
  "avg_ticket_size": "...",
  "previous_education_projects": "...",
  "duration_past_projects": "...",
  "csr_school_infra_transformation": "Yes/No/Not Found - Does company support School Infrastructure (classrooms, sanitation, drinking water, building repair)?",
  "csr_holistic_transformation": "Yes/No/Not Found - Does company support Holistic/Whole School Transformation or comprehensive school development?",
  "csr_anganwadi_transformation": "Yes/No/Not Found - Does company support Anganwadi, early childhood care, pre-schools, or maternal/child nutrition?",
  "csr_quality_education": "Yes/No/Not Found - Does company support Quality Education (general education, teacher training, scholarships, literacy, learning outcomes)?",
  "csr_stem_education": "Yes/No/Not Found - Does company support STEM, science labs, computer labs, robotics, digital learning, or coding?",
  "csr_model_school_transformation": "Yes/No/Not Found - Does company support Model Schools, government school upgrades, or district-level education?",
  "contact": {{
    "first_name": "...",
    "last_name": "...",
    "designation": "...",
    "email": "...",
    "mobile": "...",
    "phone": "...",
    "linkedin_url": "...",
    "source": "..."
  }},
  "confidence": "High/Medium/Low"
}}

TEXT EXCERPTS:
{combined_text[:source_text_char_limit]}
"""

    data, llm_error = call_llm_safe(prompt, json_mode=True, timeout=120)
    if not isinstance(data, dict):
        data = {}

    data["source_url"] = "; ".join([s["url"] for s in sources[:5] if s.get("url")]) or "Not Found"

    if "confidence" not in data or not data["confidence"]:
        data["confidence"] = "Medium"

    defaults = {
        "company_name": company_name,
        "website": "Not Found",
        "industry": "Not Found",
        "city": "Not Found",
        "state": "Not Found",
        "company_csr_focus": "Not Found",
        "thematic_focus": [],
        "geographical_priority": "Not Found",
        "program_district_state": "Not Found",
        "csr_spend_previous_fy": "Not Found",
        "csr_spend_previous_3fy": "Not Found",
        "education_csr_spend": "Not Found",
        "unspent_csr_amount": "Not Found",
        "has_company_foundation": "Not Found",
        "existing_implementation_partners": [],
        "num_implementation_partners": None,
        "avg_ticket_size": "Not Found",
        "previous_education_projects": "Not Found",
        "duration_past_projects": "Not Found",
        "csr_school_infra_transformation": "Not Found",
        "csr_holistic_transformation": "Not Found",
        "csr_anganwadi_transformation": "Not Found",
        "csr_quality_education": "Not Found",
        "csr_stem_education": "Not Found",
        "csr_model_school_transformation": "Not Found"
    }

    for key, default_value in defaults.items():
        if key not in data or data[key] is None:
            data[key] = default_value

    data["num_implementation_partners"] = _normalize_optional_text(
        data.get("num_implementation_partners")
    )

    return CompanyResearch(**data), llm_error


EDUCATION_FIELDS = [
    "csr_stem_education",
    "csr_school_infra_transformation",
    "csr_holistic_transformation",
    "csr_anganwadi_transformation",
    "csr_quality_education",
    "csr_model_school_transformation",
]


def extract_education_fields(company_name: str, search_data: dict):
    """Extract the six education fields from dedicated, labelled evidence."""
    field_meta = {}
    sections = []
    for field in EDUCATION_FIELDS:
        details = search_data.get(field, {})
        sources = details.get("sources", [])
        field_meta[field] = {
            "attempts": details.get("attempts", 0),
            "sources_checked": details.get("sources_checked", 0),
            "search_errors": details.get("errors", []),
        }
        source_lines = []
        for index, source in enumerate(sources[:5], start=1):
            source_lines.append(
                f"[{index}] URL: {source.get('url', '')}\n"
                f"{filter_relevant_text(source.get('text', ''), max_chars=3500)}"
            )
        sections.append(
            f"\n### FIELD: {field}\n"
            f"SEARCH METADATA: {json.dumps(field_meta[field], ensure_ascii=False)}\n"
            + ("\n".join(source_lines) if source_lines else "NO SOURCES")
        )

    prompt = f"""You are an evidence verifier for {company_name}'s CSR education programs.
Use ONLY the labelled source excerpts below. Do not use general knowledge.

For each field return:
- value: Yes only when the source explicitly supports that exact activity;
  No only when a reliable source explicitly says it is absent; otherwise Not Found.
- evidence: a short factual quote/paraphrase grounded in the source, or empty string.
- source_indexes: indexes of the supporting sources, such as [1, 2].

Do not treat a generic statement that the company supports education as proof of
STEM, school infrastructure, holistic transformation, Anganwadi, quality
education, or model-school transformation. Return Not Found when the complete
field search metadata shows sources were checked but no specific evidence exists.

Return ONLY valid JSON:
{{
  "csr_stem_education": {{"value": "Yes/No/Not Found", "evidence": "", "source_indexes": []}},
  "csr_school_infra_transformation": {{"value": "Yes/No/Not Found", "evidence": "", "source_indexes": []}},
  "csr_holistic_transformation": {{"value": "Yes/No/Not Found", "evidence": "", "source_indexes": []}},
  "csr_anganwadi_transformation": {{"value": "Yes/No/Not Found", "evidence": "", "source_indexes": []}},
  "csr_quality_education": {{"value": "Yes/No/Not Found", "evidence": "", "source_indexes": []}},
  "csr_model_school_transformation": {{"value": "Yes/No/Not Found", "evidence": "", "source_indexes": []}}
}}

{''.join(sections)}
"""

    data, error = call_llm_safe(prompt, json_mode=True, timeout=120)
    if error:
        return {}, error

    result = {}
    for field in EDUCATION_FIELDS:
        details = search_data.get(field, {})
        raw = data.get(field) if isinstance(data.get(field), dict) else {}
        value = str(raw.get("value") or "Not Found").strip().lower()
        if value not in {"yes", "no", "not found"}:
            value = "not found"
        indexes = raw.get("source_indexes") if isinstance(raw.get("source_indexes"), list) else []
        sources = details.get("sources", [])
        valid_indexes = [i for i in indexes if isinstance(i, int) and 1 <= i <= min(5, len(sources))]
        search_errors = details.get("errors", [])
        status = "search_failed" if search_errors and not sources else (
            "found" if value in {"yes", "no"} else "exhausted"
        )
        result[field] = {
            "value": value.title() if value != "not found" else "Not Found",
            "evidence": str(raw.get("evidence") or "").strip(),
            "sources": [
                {
                    "url": sources[i - 1].get("url"),
                    "title": sources[i - 1].get("title") or "",
                }
                for i in valid_indexes
            ],
            "checked_sources": [
                {
                    "url": source.get("url"),
                    "title": source.get("title") or "",
                }
                for source in sources[:5]
                if source.get("url")
            ],
            "status": status,
            "attempts": details.get("attempts", 0),
            "sources_checked": details.get("sources_checked", 0),
            "search_errors": search_errors,
        }
    return result, None
