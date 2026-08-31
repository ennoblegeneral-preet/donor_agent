from models import MeetingBrief
from datetime import datetime

def generate_meeting_brief(company:dict) -> MeetingBrief:
    research = company.get("research_json", {}) or {}
    fitment= company.get("program_fitment",{}) or {}

    high_fits = [k for k, v in fitment.items() if v == "High Fit"]
    talking_points = [
        f"Company CSR spend priority: { research.get('csr_spend_priority', 'not available')}",
        f"Existing implementation partners: {', '.join(research.get('existing_implementation_partners', []))or 'None found'}",
        f"category: {company.get('category', 'Not scored')}",

    ]

    return MeetingBrief(
        company_snapshot=f"{company.get('company_name')} — {research.get('industry', 'Not publicly available')}, "
                          f"HQ: {research.get('city', '-')}, {research.get('state', '-')}",
        csr_priorities=research.get('company_csr_focus', 'Not publicly available'),
        ennoble_pitch_angle=f"Strongest program fit: {', '.join(high_fits) if high_fits else 'To be assessed in discovery call'}",
        key_talking_points=talking_points,
        generated_at=datetime.utcnow().isoformat()
    )