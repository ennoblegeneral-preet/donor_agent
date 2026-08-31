def first_source_url(research: dict) -> str:
    """`source_url` may hold several URLs joined by ';'. Return only the first valid
    one so it can be safely used as a single link / Website fallback (never the whole
    concatenated string, which is not a valid URL)."""
    raw = (research or {}).get("source_url", "") or ""
    for part in str(raw).split(";"):
        part = part.strip()
        if part and part.lower() != "not found" and "http" in part.lower():
            return part
    return ""


def map_to_zoho_lead(company_data: dict) -> dict:
    """
    Maps a MongoDB company record to the Zoho CRM Leads module.
    Zoho Leads requires Last_Name, so the company name is used for it.
    Includes research data + CRM dashboard fields.
    """
    research = company_data.get("research_json") or {}
    crm = company_data.get("crm") or {}
    contact = research.get("contact") or {}
    financial_data = company_data.get("financial_data") or {}

    # Latest fiscal year's turnover -> Zoho's standard Annual_Revenue field.
    # Sent as-is in Crores (not converted to rupees) - matches how the rest
    # of this app displays financial figures, by team decision.
    # Rest of financial_data (net worth, PBT, net profit, CSR spend) stays
    # in Description until custom fields exist (blocked on Free Edition).
    fiscal_years = financial_data.get("fiscal_years") or []
    annual_revenue = (financial_data.get("turnover") or {}).get(fiscal_years[0]) if fiscal_years else None

    # Build CSR themes string
    thematic_focus = research.get("thematic_focus") or []
    csr_themes = ", ".join(thematic_focus) if thematic_focus else "Not Found"

    # Build implementation partners string
    partners = research.get("existing_implementation_partners") or []
    impl_partners = ", ".join(partners) if partners else "Not Found"

    # Build committee members string with LinkedIn URLs
    csr_data = company_data.get("csr_data") or {}
    committee_members = csr_data.get("committee_members") or company_data.get("committee_members") or []
    committee_linkedin = company_data.get("committee_members_linkedin") or {}
    if committee_members:
        comm_strs = []
        for m in committee_members:
            li_url = committee_linkedin.get(m)
            if li_url:
                comm_strs.append(f"{m} ({li_url})")
            else:
                comm_strs.append(m)
        comm_members_str = ", ".join(comm_strs)
    else:
        comm_members_str = "Not Found"

    # Build description with all key research info
    description_parts = [
        f"Industry: {research.get('industry', 'Not Found')}",
        f"CSR Themes: {csr_themes}",
        f"Geography: {research.get('geographical_priority', 'Not Found')}",
        f"CSR Focus: {research.get('company_csr_focus', 'Not Found')}",
        f"Past CSR Programs: {research.get('previous_education_projects', 'Not Found')}",
        f"Avg Ticket Size: {research.get('avg_ticket_size', 'Not Found')}",
        f"Program District/State: {research.get('program_district_state', 'Not Found')}",
        f"Education CSR Spend: {research.get('education_csr_spend', 'Not Found')}",
        f"CSR Spend (Prev FY): {research.get('csr_spend_previous_fy', 'Not Found')}",
        f"CSR Spend (3 FY): {research.get('csr_spend_previous_3fy', 'Not Found')}",
        f"Implementation Partners: {impl_partners}",
        f"CSR Committee Members: {comm_members_str}",
        f"Has Company Foundation: {research.get('has_company_foundation', 'Not Found')}",
        f"Source URL: {research.get('source_url', 'Not Found')}",
    ]

    # Add CRM dashboard fields if present
    if crm.get("lead_status"):
        description_parts.append(f"Lead Status: {crm['lead_status']}")
    if crm.get("next_followup_date") or crm.get("next_followups_date"):
        followup = crm.get("next_followup_date") or crm.get("next_followups_date")
        description_parts.append(f"Next Follow-up: {followup}")
    if crm.get("immediate_action"):
        description_parts.append(f"Immediate Action: {crm['immediate_action']}")
    if crm.get("description"):
        description_parts.append(f"Notes: {crm['description']}")
    if crm.get("decision_maker_name"):
        description_parts.append(f"Decision Maker: {crm['decision_maker_name']}")

    def clean(val):
        """Return empty string if value is None or the literal 'Not Found' / 'Not publicly available'."""
        if not val or str(val).strip().lower() in ("not found", "none", "n/a", "not publicly available", "-"):
            return ""
        return str(val).strip()

    # Determine Lead / Decision Maker Name
    dm_name = clean(crm.get("decision_maker_name"))
    if dm_name:
        parts = dm_name.split(" ", 1)
        first_name = parts[0] if len(parts) > 1 else ""
        last_name = parts[1] if len(parts) > 1 else parts[0]
    else:
        first_name = clean(contact.get("first_name"))
        last_name = clean(contact.get("last_name")) or company_data.get("company_name") or "Unknown Company"

    # Determine Email and Phone
    email = clean(crm.get("decision_maker_email")) or clean(contact.get("email"))
    phone = clean(crm.get("decision_maker_phone")) or clean(contact.get("phone")) or clean(contact.get("mobile"))

    payload = {
        "Last_Name": last_name[:80],
        "Company": (company_data.get("company_name") or "Unknown Company")[:120],
        "Website": clean(company_data.get("website") or first_source_url(research))[:255],
        "Industry": clean(research.get("industry"))[:120],
        "City": clean(research.get("city"))[:100],
        "State": clean(research.get("state") or research.get("geographical_priority"))[:100],
        "Country": "India",
        "Description": "\n".join(description_parts),
        "Lead_Source": crm.get("lead_source") or "AI Research Agent",
        "Lead_Status": crm.get("lead_status") or "Open - Not Contacted",
    }

    if first_name:
        payload["First_Name"] = first_name[:40]
    if email:
        payload["Email"] = email[:100]
    if phone:
        payload["Phone"] = phone[:50]
        payload["Mobile"] = phone[:50]

    if annual_revenue is not None:
        payload["Annual_Revenue"] = annual_revenue

    return payload


def map_to_zoho_contact(contact_data: dict, account_name: str, account_id: str = None) -> dict:
    """
    Maps MongoDB contact details to Zoho CRM Contact format
    """
    name_parts = contact_data.get("name", "Unknown Contact").strip().split(" ", 1)
    first_name = name_parts[0] if len(name_parts) > 1 else ""
    last_name = name_parts[1] if len(name_parts) > 1 else name_parts[0]

    contact = {
        "First_Name": first_name,
        "Last_Name": last_name,
        "Title": contact_data.get("designation", "CSR Contact"),
        "Email": contact_data.get("email") if contact_data.get("email") and contact_data.get("email") != "Not Found" else None,
        "Description": f"Source: {contact_data.get('source', '')}"
    }
    if account_id:
        contact["Account_Name"] = {"id": account_id}
    else:
        contact["Account_Name"] = {"Account_Name": account_name}
    return contact


def map_to_zoho_format(company: dict) -> dict:
    """
    Maps a MongoDB company record to Zoho CRM format,
    including contact details extracted from research_json.contact.
    """
    research = company.get("research_json", {}) or {}
    contact = research.get("contact", {}) or {}

    # Build CSR themes string
    thematic_focus = research.get("thematic_focus") or []
    csr_themes = ", ".join(thematic_focus) if thematic_focus else "Not Found"

    # Build implementation partners string
    partners = research.get("existing_implementation_partners") or []
    impl_partners = ", ".join(partners) if partners else "Not Found"

    # Build description with all key research info
    description_parts = [
        f"Industry: {research.get('industry', 'Not Found')}",
        f"CSR Themes: {csr_themes}",
        f"Geography: {research.get('geographical_priority', 'Not Found')}",
        f"CSR Focus: {research.get('company_csr_focus', 'Not Found')}",
        f"Past CSR Programs: {research.get('previous_education_projects', 'Not Found')}",
        f"Avg Ticket Size: {research.get('avg_ticket_size', 'Not Found')}",
        f"Education CSR Spend: {research.get('education_csr_spend', 'Not Found')}",
        f"CSR Spend (Prev FY): {research.get('csr_spend_previous_fy', 'Not Found')}",
        f"Implementation Partners: {impl_partners}",
        f"Source URL: {research.get('source_url', 'Not Found')}",
    ]

    crm = company.get("crm", {}) or {}

    dm_name = (crm.get("decision_maker_name") or "").strip()
    if dm_name:
        parts = dm_name.split(" ", 1)
        first_name = parts[0] if len(parts) > 1 else ""
        last_name = parts[1] if len(parts) > 1 else parts[0]
    else:
        first_name = contact.get("first_name", "Not publicly available")
        last_name = contact.get("last_name", "Not publicly available")

    email = crm.get("decision_maker_email") or contact.get("email", "Not publicly available")
    phone = crm.get("decision_maker_phone") or contact.get("phone", "Not publicly available")
    mobile = crm.get("decision_maker_phone") or contact.get("mobile", "Not publicly available")

    return {
        # Company fields
        "Company": company.get("company_name") or "Unknown Company",
        "Website": company.get("website") or first_source_url(research),
        "Industry": research.get("industry", "Not publicly available"),
        "Description": "\n".join(description_parts),
        # Contact fields from CRM / Decision Maker or research_json.contact
        "First_Name": first_name,
        "Last_Name": last_name,
        "Email": email,
        "Mobile": mobile,
        "Phone": phone,
        "Designation": contact.get("designation", "Not publicly available"),
        "Record_Stage": company.get("record_stage", "Enriched Data"),
    }


def format_gpt_horizontal_table(company: dict) -> str:
    """
    Formats a company record into an exact horizontal markdown table row
    ready to copy-paste into ChatGPT or Excel.
    """
    research = company.get("research_json", {}) or {}
    contact = research.get("contact", {}) or {}
    crm = company.get("crm", {}) or {}
    fitment = company.get("program_fitment", {}) or {}

    headers = [
        "Record Stage", "Lead Owner", "Company", "First Name", "Last Name", "Email", "Mobile", "Phone",
        "Website", "Industry", "City", "State", "Lead Source", "Lead Status", "Designation",
        "Category", "Company CSR Focus", "Thematic Focus", "Ennoble Fitment",
        "STEM Education", "School Infrastructure Transformation", "Holistic School Transformation",
        "Anganwadi Transformation", "Quality Education", "Model School Transformation",
        "Geographical Priority", "Program District & State", "CSR Spent of Previous Financial Year",
        "CSR Spent of Previous 3 Financial Year", "Education - CSR Spend", "Unspent CSR Amount",
        "Do they Have Company Foundation", "Names of Existing Implementation Partners",
        "No. of Existing Implementation Partners", "Avg. Ticket Size for Project Approved",
        "Approved Previous Projects on Education", "Duration of Past Projects",
        "Next follow up date", "Immediate Action", "Description"
    ]

    thematic = ", ".join(research.get("thematic_focus", [])) if research.get("thematic_focus") else "Not publicly available"
    partners = ", ".join(research.get("existing_implementation_partners", [])) if research.get("existing_implementation_partners") else "Not publicly available"
    partner_count = str(len(research.get("existing_implementation_partners", []))) if research.get("existing_implementation_partners") else "0"

    desc = f"Source Type: CSR Report; Annual Report; Company Website; LinkedIn. Confidence Level: {research.get('confidence', 'High')}. Source Notes: CSR spend and contact details verified. Source URLs: {research.get('source_url', '-')}"

    # Decision Maker details override contact details if provided
    dm_name = (crm.get("decision_maker_name") or "").strip()
    if dm_name:
        parts = dm_name.split(" ", 1)
        first_name = parts[0] if len(parts) > 1 else ""
        last_name = parts[1] if len(parts) > 1 else parts[0]
    else:
        first_name = contact.get("first_name", "Not publicly available")
        last_name = contact.get("last_name", "Not publicly available")

    email = crm.get("decision_maker_email") or contact.get("email", "Not publicly available")
    phone = crm.get("decision_maker_phone") or contact.get("phone", "Not publicly available")
    mobile = crm.get("decision_maker_phone") or contact.get("mobile", "Not publicly available")

    row = [
        "Enriched",
        crm.get("lead_owner") or "Shadab",
        company.get("company_name", "-"),
        first_name,
        last_name,
        email,
        mobile,
        phone,
        company.get("website") or research.get("source_url", "-"),
        research.get("industry", "Not publicly available"),
        research.get("city", "Not publicly available"),
        research.get("state", "Not publicly available"),
        contact.get("source") or "CSR Report; Annual Report; Company Website; LinkedIn",
        "Fresh Lead",
        contact.get("designation", "Not publicly available"),
        company.get("tier") or company.get("category") or "Tier B",
        research.get("company_csr_focus", "Not publicly available"),
        thematic,
        fitment.get("Ennoble Fitment", "Not Evident"),
        fitment.get("STEM Education", "Not Evident"),
        fitment.get("School Infrastructure Transformation", "Not Evident"),
        fitment.get("Holistic School Transformation", "Not Evident"),
        fitment.get("Anganwadi Transformation", "Not Evident"),
        fitment.get("Quality Education", "Not Evident"),
        fitment.get("Model School Transformation", "Not Evident"),
        research.get("geographical_priority", "Not publicly available"),
        research.get("program_district_state", "Not publicly available"),
        research.get("csr_spend_previous_fy", "Not publicly available"),
        research.get("csr_spend_previous_3fy", "Not publicly available"),
        research.get("education_csr_spend", "Not publicly available"),
        research.get("unspent_csr_amount", "Not publicly available"),
        research.get("has_company_foundation", "Not publicly available"),
        partners,
        partner_count,
        research.get("avg_ticket_size", "Not publicly available"),
        research.get("previous_education_projects", "Not publicly available"),
        research.get("duration_past_projects", "Not publicly available"),
        crm.get("next_followup_date", ""),
        crm.get("immediate_action", ""),
        desc
    ]

    header_line = "| " + " | ".join(headers) + " |"
    separator_line = "| " + " | ".join(["---"] * len(headers)) + " |"
    data_line = "| " + " | ".join([str(val).replace("|", "\\|").replace("\n", " ") for val in row]) + " |"

    return f"{header_line}\n{separator_line}\n{data_line}"
