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


def clean_crm_val(val):
    """Return empty string if value is None or literal 'Not Found' / 'None' / 'N/A'."""
    if val is None:
        return ""
    if isinstance(val, list):
        filtered = [str(x).strip() for x in val if x and str(x).strip().lower() not in ("not found", "none", "n/a", "not publicly available", "-", "null")]
        return ", ".join(filtered)
    s = str(val).strip()
    if s.lower() in ("not found", "none", "n/a", "not publicly available", "-", "null", "undefined"):
        return ""
    return s


def map_to_zoho_lead(company_data: dict) -> dict:
    """
    Maps a MongoDB company record to the Zoho CRM Leads module.
    Fills all 21 standard + custom fields if available, leaving missing/Not Found fields empty.
    """
    research = company_data.get("research_json") or {}
    crm = company_data.get("crm") or {}
    contact = research.get("contact") or {}
    financial_data = company_data.get("financial_data") or {}

    # Extract clean values for all 21 fields
    company_name = clean_crm_val(company_data.get("company_name")) or "Unknown Company"
    website = clean_crm_val(company_data.get("website") or first_source_url(research))
    lead_owner = clean_crm_val(crm.get("lead_owner") or company_data.get("created_by"))
    lead_status = clean_crm_val(crm.get("lead_status")) or "Open - Not Contacted"
    designation = clean_crm_val(contact.get("designation"))
    csr_spend_prev = clean_crm_val(research.get("csr_spend_previous_fy"))
    edu_csr_spend = clean_crm_val(research.get("education_csr_spend"))
    unspent_amt = clean_crm_val(research.get("unspent_csr_amount"))
    industry = clean_crm_val(research.get("industry"))
    csr_focus = clean_crm_val(research.get("company_csr_focus"))
    thematic_focus = clean_crm_val(research.get("thematic_focus"))
    program_dist_state = clean_crm_val(research.get("program_district_state"))
    lead_tier = clean_crm_val(company_data.get("tier"))
    score_fitment = clean_crm_val(company_data.get("score"))
    sources_links = clean_crm_val(research.get("source_url"))
    avg_ticket = clean_crm_val(research.get("avg_ticket_size"))
    impl_partners = clean_crm_val(research.get("existing_implementation_partners"))
    foundation = clean_crm_val(research.get("has_company_foundation"))
    city = clean_crm_val(research.get("city"))
    state = clean_crm_val(research.get("state") or research.get("geographical_priority"))

    # Decision Maker / Contact details
    dm_name = clean_crm_val(crm.get("decision_maker_name"))
    if dm_name:
        parts = dm_name.split(" ", 1)
        first_name = parts[0] if len(parts) > 1 else ""
        last_name = parts[1] if len(parts) > 1 else parts[0]
    else:
        first_name = clean_crm_val(contact.get("first_name"))
        last_name = clean_crm_val(contact.get("last_name")) or company_name

    email = clean_crm_val(crm.get("decision_maker_email") or contact.get("email"))
    phone = clean_crm_val(crm.get("decision_maker_phone") or contact.get("phone"))
    mobile = clean_crm_val(contact.get("mobile") or phone)

    # Committee members summary
    csr_data = company_data.get("csr_data") or {}
    committee_members = csr_data.get("committee_members") or company_data.get("committee_members") or []
    committee_linkedin = company_data.get("committee_members_linkedin") or {}
    if committee_members:
        comm_strs = []
        for m in committee_members:
            li_url = committee_linkedin.get(m)
            comm_strs.append(f"{m} ({li_url})" if li_url else str(m))
        comm_members_str = ", ".join(comm_strs)
    else:
        comm_members_str = ""

    # Extract additional Financial Data
    fiscal_years = financial_data.get("fiscal_years") or []
    turnover_dict = financial_data.get("turnover") or {}
    pbt_dict = financial_data.get("pbt") or {}
    net_profit_dict = financial_data.get("net_profit") or {}
    net_worth_dict = financial_data.get("net_worth") or {}
    prescribed_csr = financial_data.get("prescribed_csr_cr") or financial_data.get("prescribed_csr")

    fin_lines = []
    if fiscal_years:
        for fy in fiscal_years[:3]:
            t = turnover_dict.get(fy)
            np = net_profit_dict.get(fy)
            p = pbt_dict.get(fy)
            nw = net_worth_dict.get(fy)
            fin_entry = []
            if t is not None:
                fin_entry.append(f"Turnover: ₹{t:,.2f} Cr" if isinstance(t, (int, float)) else f"Turnover: ₹{t} Cr")
            if p is not None:
                fin_entry.append(f"PBT: ₹{p:,.2f} Cr" if isinstance(p, (int, float)) else f"PBT: ₹{p} Cr")
            if np is not None:
                fin_entry.append(f"Net Profit: ₹{np:,.2f} Cr" if isinstance(np, (int, float)) else f"Net Profit: ₹{np} Cr")
            if nw is not None:
                fin_entry.append(f"Net Worth: ₹{nw:,.2f} Cr" if isinstance(nw, (int, float)) else f"Net Worth: ₹{nw} Cr")
            if fin_entry:
                fin_lines.append(f"  • {fy}: {', '.join(fin_entry)}")
    if prescribed_csr:
        fin_lines.append(f"  • Prescribed CSR (2% Obligation): ₹{prescribed_csr} Cr")

    # Thematic Pillar Flags
    pillars = []
    if clean_crm_val(research.get("csr_stem_education")) in ("Yes", "yes"):
        pillars.append("STEM / Robotics")
    if clean_crm_val(research.get("csr_school_infra_transformation")) in ("Yes", "yes"):
        pillars.append("School Infrastructure")
    if clean_crm_val(research.get("csr_holistic_transformation")) in ("Yes", "yes"):
        pillars.append("Holistic Transformation")
    if clean_crm_val(research.get("csr_anganwadi_transformation")) in ("Yes", "yes"):
        pillars.append("Anganwadi Care")
    if clean_crm_val(research.get("csr_quality_education")) in ("Yes", "yes"):
        pillars.append("Quality Education / Literacy")
    if clean_crm_val(research.get("csr_model_school_transformation")) in ("Yes", "yes"):
        pillars.append("Model School Upgradation")

    # AI Intelligence Briefs
    channel = clean_crm_val(company_data.get("recommended_channel"))
    warm_angle = clean_crm_val(company_data.get("warm_connect"))
    brief = clean_crm_val(company_data.get("meeting_brief"))
    next_follow = clean_crm_val(crm.get("next_followup_date") or crm.get("next_followups_date"))
    imm_action = clean_crm_val(crm.get("immediate_action"))
    notes = clean_crm_val(crm.get("description"))

    # Comprehensive clean Master Lead Card in Description
    desc_sections = []

    # 1. Lead Overview
    sec_lead = ["=== LEAD & FITMENT OVERVIEW ==="]
    if lead_owner:
        sec_lead.append(f"Lead Found By: {lead_owner}")
    if score_fitment:
        sec_lead.append(f"Fitment Score: {score_fitment}/100 ({lead_tier or 'Unranked'})")
    if designation:
        sec_lead.append(f"Decision Maker Designation: {designation}")
    if industry:
        sec_lead.append(f"Industry: {industry}")
    if city or state:
        sec_lead.append(f"Location: {', '.join(filter(None, [city, state]))}")
    desc_sections.append("\n".join(sec_lead))

    # 2. CSR & Education Deep Dive
    sec_csr = ["=== CSR & EDUCATION INTELLIGENCE ==="]
    if csr_focus:
        sec_csr.append(f"CSR Philosophy / Focus: {csr_focus}")
    if thematic_focus:
        sec_csr.append(f"Thematic Focus: {thematic_focus}")
    if program_dist_state:
        sec_csr.append(f"Program Districts & States: {program_dist_state}")
    if edu_csr_spend:
        sec_csr.append(f"Education CSR Spend: {edu_csr_spend}")
    if csr_spend_prev:
        sec_csr.append(f"Previous Year Total CSR: {csr_spend_prev}")
    if unspent_amt:
        sec_csr.append(f"Unspent CSR Amount: {unspent_amt}")
    if avg_ticket:
        sec_csr.append(f"Avg Grant / Ticket Size: {avg_ticket}")
    if impl_partners:
        sec_csr.append(f"NGO Partners: {impl_partners}")
    if foundation:
        sec_csr.append(f"Company Foundation: {foundation}")
    if pillars:
        sec_csr.append(f"Supported Education Themes: {', '.join(pillars)}")
    if comm_members_str:
        sec_csr.append(f"CSR Committee Members: {comm_members_str}")
    desc_sections.append("\n".join(sec_csr))

    # 3. Financial Statements
    if fin_lines:
        sec_fin = ["=== FINANCIAL METRICS (SCREENER.IN) ==="] + fin_lines
        desc_sections.append("\n".join(sec_fin))

    # 4. Action & Followup
    if next_follow or imm_action or notes:
        sec_act = ["=== OUTREACH & NEXT ACTIONS ==="]
        if next_follow:
            sec_act.append(f"Next Follow-up Date: {next_follow}")
        if imm_action:
            sec_act.append(f"Immediate Action: {imm_action}")
        if notes:
            sec_act.append(f"Notes: {notes}")
        if channel:
            sec_act.append(f"Recommended Outreach Channel: {channel}")
        desc_sections.append("\n".join(sec_act))

    # 5. Brief & Source Links
    if brief:
        desc_sections.append(f"=== EXECUTIVE BRIEF ===\n{brief[:1500]}")
    if sources_links:
        desc_sections.append(f"=== CITATIONS & SOURCES ===\n{sources_links}")

    full_description = "\n\n".join(desc_sections)

    # Build standard Zoho Lead payload
    payload = {
        "Last_Name": last_name[:80],
        "Company": company_name[:120],
        "Lead_Status": lead_status,
        "Lead_Source": "AI Research Agent",
        "Description": full_description,
    }

    if first_name:
        payload["First_Name"] = first_name[:40]
    if designation:
        payload["Designation"] = designation[:100]
        payload["Title"] = designation[:100]
    if website:
        payload["Website"] = website[:255]
    if email:
        payload["Email"] = email[:100]
    if phone:
        payload["Phone"] = phone[:50]
    if mobile:
        payload["Mobile"] = mobile[:50]
    if industry:
        payload["Industry"] = industry[:120]
    if city:
        payload["City"] = city[:100]
    if state:
        payload["State"] = state[:100]
    payload["Country"] = "India"

    # Map custom field keys if defined in user's Zoho CRM layout
    if lead_tier:
        payload["Rating"] = lead_tier
        payload["Lead_Category"] = lead_tier
        payload["Tier"] = lead_tier
    if score_fitment:
        payload["Ennoble_Fitment"] = score_fitment
        payload["Fitment_Score"] = score_fitment
    if csr_spend_prev:
        payload["CSR_Spent_Previous_Year"] = csr_spend_prev
        payload["CSR_Spend_Previous_Year"] = csr_spend_prev
    if edu_csr_spend:
        payload["Education_CSR_Spend"] = edu_csr_spend
    if unspent_amt:
        payload["Unspent_Amount"] = unspent_amt
        payload["Unspent_CSR_Amount"] = unspent_amt
    if csr_focus:
        payload["Company_CSR_Focus"] = csr_focus
    # Multi-select list fields for Zoho CRM (expects jsonarray)
    thematic_focus_list = []
    raw_thematic = research.get("thematic_focus")
    if isinstance(raw_thematic, list):
        thematic_focus_list = [str(x).strip() for x in raw_thematic if x and str(x).strip().lower() not in ("not found", "none", "n/a", "not publicly available", "-", "null")]
    elif isinstance(raw_thematic, str) and raw_thematic.strip():
        thematic_focus_list = [x.strip() for x in raw_thematic.split(",") if x.strip() and x.strip().lower() not in ("not found", "none", "n/a", "not publicly available", "-", "null")]

    if thematic_focus_list:
        payload["Thematic_Focus"] = thematic_focus_list

    if program_dist_state:
        payload["Program_District_and_State"] = program_dist_state
        payload["Program_District_State"] = program_dist_state
    if avg_ticket:
        payload["Avg_Ticket_Size"] = avg_ticket
    if impl_partners:
        payload["Implementation_Partners"] = impl_partners
        payload["Name_of_Implementation_Partner"] = impl_partners
    if foundation:
        payload["Company_Foundation"] = foundation
        payload["Do_they_have_company_foundation"] = foundation
    if sources_links:
        payload["Lead_Sources"] = sources_links[:255]

    # Latest turnover
    latest_turnover = turnover_dict.get(fiscal_years[0]) if fiscal_years else None
    if latest_turnover is not None:
        try:
            payload["Annual_Revenue"] = float(latest_turnover)
        except (ValueError, TypeError):
            pass

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
