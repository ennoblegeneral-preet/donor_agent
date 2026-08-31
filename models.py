from importlib.metadata import distribution
from pydantic import BaseModel, field_validator
from typing import Optional, List


class ContactInfo(BaseModel):
    first_name: Optional[str] = "Not publicly available"
    last_name: Optional[str] = "Not publicly available"
    designation: Optional[str] = "Not publicly available"
    email: Optional[str] = "Not publicly available"
    mobile: Optional[str] = "Not publicly available"
    phone: Optional[str] = "Not publicly available"
    linkedin: Optional[str] = "Not publicly available"
    source: Optional[str] = "Not publicly available"

    class Config:
        extra = 'allow'

    @field_validator(
        "first_name", "last_name", "designation",
        "email", "mobile", "phone", "linkedin", "source",
        mode="before"
    )
    @classmethod
    def fix_list_to_string(cls, v):
        if isinstance(v, list):
            cleaned = [str(i).strip() for i in v if i]
            return cleaned[0] if cleaned else "Not publicly available"
        return v


class CompanyResearch(BaseModel):
    contact: ContactInfo = ContactInfo()
    company_name: Optional[str] = "Not Found"
    website: Optional[str] = "Not Found"
    industry: Optional[str] = "Not Found"
    city: Optional[str] = "Not Found"
    state: Optional[str] = "Not Found"
    company_csr_focus: Optional[str] = "Not Found"
    thematic_focus: List[str] = []
    geographical_priority: Optional[str] = "Not Found"
    program_district_state: Optional[str] = "Not Found"
    csr_spend_previous_fy: Optional[str] = "Not Found"
    csr_spend_previous_3fy: Optional[str] = "Not Found"
    education_csr_spend: Optional[str] = "Not Found"
    unspent_csr_amount: Optional[str] = "Not Found"
    has_company_foundation: Optional[str] = "Not Found"
    existing_implementation_partners: List[str] = []
    avg_ticket_size: Optional[str] = "Not Found"
    previous_education_projects: Optional[str] = "Not Found"
    csr_school_infra_transformation: Optional[str] = "Not Found"
    csr_holistic_transformation: Optional[str] = "Not Found"
    csr_anganwadi_transformation: Optional[str] = "Not Found"
    csr_quality_education: Optional[str] = "Not Found"
    csr_stem_education: Optional[str] = "Not Found"             
    csr_model_school_transformation: Optional[str] = "Not Found" 
    duration_past_projects: Optional[str] = "Not Found"
    csr_spend_priority: Optional[str] = "Low"
    geographical_priority: Optional[str] = "Low"
    num_implementation_partners: Optional[str] = None
    source_url: Optional[str] = "Not Found"
    confidence: Optional[str] = "unverified"

    class Config:
        extra = 'allow'

    # List-type fields: agar model ne string bhej di, list bana do
    @field_validator("thematic_focus", "existing_implementation_partners", mode="before")
    @classmethod
    def fix_list_field(cls, v):
        if isinstance(v, str):
            return [] if v.strip().lower() in ["not found", "none", "na", "n/a"] else [v]
        return v

    # String-type fields: agar model ne list bhej di, string bana do (yahi missing tha)
    @field_validator(
        "company_csr_focus", "geographical_priority", "program_district_state", "industry", "city",
        "state", "csr_spend_previous_fy", "csr_spend_previous_3fy", "education_csr_spend",
        "unspent_csr_amount", "has_company_foundation", "avg_ticket_size", "previous_education_projects",
        "duration_past_projects", "csr_school_infra_transformation", "csr_holistic_transformation",
        "csr_anganwadi_transformation", "csr_quality_education",
        "csr_stem_education", "csr_model_school_transformation", 
        mode="before"
    )
    @classmethod
    def fix_string_field(cls, v):
        if isinstance(v, list):
            cleaned = [str(item).strip() for item in v if item]
            return ", ".join(cleaned) if cleaned else "Not Found"
        return v

    @field_validator("num_implementation_partners", mode="before")
    @classmethod
    def fix_partner_count(cls, v):
        if v is None:
            return None
        if isinstance(v, list):
            v = v[0] if v else None
            if v is None:
                return None
        if isinstance(v, str):
            cleaned = v.strip()
            return None if cleaned.lower() in ["", "not found", "none", "na", "n/a"] else cleaned
        if isinstance(v, (int, float)):
            return str(int(v)) if float(v).is_integer() else str(v)
        return str(v)

    @field_validator("csr_spend_priority", "geographical_priority", mode="before")
    @classmethod
    def fix_priority_field(cls, v):
        if isinstance(v, list):
            v = v[0] if v else "Low"
        if isinstance(v, str) and v.strip().title() in ["High", "Medium", "Low"]:
            return v.strip().title()
        return "Low"

class FitmentDecision (BaseModel):
    record_stage: str
    reasoning: str
    checklist: dict

class ScoringResults(BaseModel):
    ennoble_fitment: int
    category: str
    program_fit_scores: dict
    reasoning: dict


class CRMRecord(BaseModel):
    record_stage: str = "NEW"
    lead_owner: Optional[str] = None
    lead_source: str = "AI Research Agent"
    lead_status: str = "Open - Not Contacted"
    next_followup_date: Optional[str] = None
    immediate_action: Optional[str] = None
    description: Optional[str] = None
    decision_maker_name: Optional[str] = None
    decision_maker_phone: Optional[str] = None
    decision_maker_email: Optional[str] = None


class WarmConnectSusggestion(BaseModel):
    has_warm_connect: bool = False
    suggested_connector_name: Optional[str] = None
    connector_relationship: Optional[str] = None
    confidence: str = "Low"

class OutreachChannelRecommendation(BaseModel):
    recommended_channel: str
    reasoning: str

class DraftedMessage(BaseModel):
    channel: str
    subject: Optional[str] = None
    body: str
    generated_at: Optional[str] = None
    status: str = "draft"

class MeetingBrief(BaseModel):
    company_snapshot: str
    csr_priorities: str
    ennoble_pitch_angle: str
    key_talking_points: List[str] = []
    generated_at: Optional[str] = None

    