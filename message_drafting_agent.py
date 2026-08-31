import json
import requests
import os
from datetime import datetime
from dotenv import load_dotenv
from models import DraftedMessage

load_dotenv()


from llm_service import call_llm


def _call_groq(prompt: str) -> dict:
    return call_llm(prompt, json_mode=True, temperature=0.3)


def draft_outreach_message(company: dict, channel: str = "email") -> DraftedMessage:
    research = company.get("research_json", {}) or {}
    contact = research.get("contact", {}) or {}
    fitment = company.get("program_fitment", {}) or {}

    best_fit_program = max(fitment.items(), key=lambda x: x[1] == "High Fit", default=(None, None))[0]

    prompt = f"""You are drafting a SHORT, professional first-touch outreach {channel} on behalf of 
Ennoble Social Innovation Foundation (an education-focused CSR implementation partner) to a potential 
corporate CSR partner.

STRICT RULES:
- This is a DRAFT ONLY. Do not include any sending instructions, tracking pixels, or automation triggers.
- Keep it under 150 words, professional, Indian English, no hype or exaggerated claims.
- Reference the company's actual CSR focus area from the data below — do not invent facts.
- If the contact's name is "Not publicly available", address it generically ("Dear CSR Team").
- End with a soft call-to-action (e.g. "Would you be open to a short call?"), not a hard sell.

Company: {company.get('company_name')}
CSR Focus: {research.get('company_csr_focus', 'Not publicly available')}
Best-fit Ennoble Program: {best_fit_program or 'School Transformation'}
Contact Name: {contact.get('first_name', 'Not publicly available')} {contact.get('last_name', '')}
Contact Designation: {contact.get('designation', 'Not publicly available')}

Return ONLY JSON: {{"subject": "...", "body": "..."}}
"""

    result = _call_groq(prompt)

    return DraftedMessage(
        channel=channel,
        subject=result.get("subject"),
        body=result.get("body", ""),
        generated_at=datetime.utcnow().isoformat(),
        status="draft"   # HARD-CODED — this agent can never mark a message as "sent"
    )