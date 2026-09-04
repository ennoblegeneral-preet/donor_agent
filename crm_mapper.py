from scoring_agent import parse_crore


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
    Maps a MongoDB company record to the Zoho CRM Leads module using the
    CONFIRMED API names from "DonorIQ x Zoho Fields.xlsx" (Sheet2, "Field api
    name" column) - a direct 1:1 mapping, not the earlier guessed-alias
    approach. Whatever real data we have goes through as-is (or parsed to the
    right type for Currency/Number fields); anything we don't have - missing,
    "Not Found", "Not publicly available", etc, all normalized to "" by
    clean_crm_val()/parse_crore() below - is left OUT of the payload entirely
    rather than sent as literal placeholder text, so Zoho shows those fields
    genuinely blank instead of full of "Not Found" strings.
    """
    research = company_data.get("research_json") or {}
    crm = company_data.get("crm") or {}
    contact = research.get("contact") or {}
    financial_data = company_data.get("financial_data") or {}
    program_fitment = company_data.get("program_fitment") or {}

    company_name = clean_crm_val(company_data.get("company_name")) or "Unknown Company"
    website = clean_crm_val(company_data.get("website") or first_source_url(research))
    lead_owner = clean_crm_val(crm.get("lead_owner") or company_data.get("created_by"))
    lead_status = clean_crm_val(crm.get("lead_status")) or "Open - Not Contacted"
    designation = clean_crm_val(crm.get("decision_maker_designation") or contact.get("designation") or contact.get("title"))
    industry = clean_crm_val(research.get("industry"))
    csr_focus = clean_crm_val(research.get("company_csr_focus"))
    prev_projects_edu = clean_crm_val(research.get("previous_education_projects"))
    program_dist_state = clean_crm_val(research.get("program_district_state"))
    social_lead_id = clean_crm_val(company_data.get("id_str") or str(company_data.get("_id", "")))
    lead_tier = clean_crm_val(company_data.get("tier"))
    score_fitment = clean_crm_val(company_data.get("score"))
    # "Ennoble Fitment" is the strongest of the 6 program fits (High/Medium/Low
    # Fit/Not Evident), computed in scoring_agent.py and stored under
    # program_fitment - NOT the 0-100 numeric score (that has no confirmed
    # Zoho field yet, so it only appears in the Description text below).
    ennoble_fitment_label = clean_crm_val(program_fitment.get("Ennoble Fitment"))
    lead_source = clean_crm_val(crm.get("lead_source")) or "AI Research Agent"
    foundation = clean_crm_val(research.get("has_company_foundation"))
    duration_past = clean_crm_val(research.get("duration_past_projects"))
    immediate_action = clean_crm_val(crm.get("immediate_action"))
    agency = "Ennoble Social Innovation"
    sources_links = clean_crm_val(research.get("source_url"))

    # Location
    city = clean_crm_val(research.get("city"))
    state = clean_crm_val(research.get("state") or research.get("geographical_priority"))
    street = clean_crm_val(research.get("street") or research.get("address"))
    zip_code = clean_crm_val(research.get("pincode") or research.get("zip_code"))
    country = "India"

    # Priority determination based on Tier
    priority = "High" if lead_tier == "Tier A" else ("Medium" if lead_tier == "Tier B" else "Low")

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
    sec_email = clean_crm_val(contact.get("secondary_email"))
    phone = clean_crm_val(crm.get("decision_maker_phone") or contact.get("phone"))
    mobile = clean_crm_val(contact.get("mobile") or phone)
    landline = clean_crm_val(contact.get("landline") or phone)

    # Implementation partners breakdown (Count + Names)
    raw_partners = research.get("existing_implementation_partners") or []
    partners_list = []
    if isinstance(raw_partners, list):
        partners_list = [str(x).strip() for x in raw_partners if x and str(x).strip().lower() not in ("not found", "none", "n/a", "not publicly available", "-")]
    elif isinstance(raw_partners, str) and raw_partners.strip():
        partners_list = [x.strip() for x in raw_partners.split(",") if x.strip() and x.strip().lower() not in ("not found", "none", "n/a", "not publicly available", "-")]
    partners_count = len(partners_list) if partners_list else None
    partners_names_str = ", ".join(partners_list) if partners_list else ""

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

    # Thematic Focus - Zoho's Thematic_Focus field takes a list (jsonarray)
    thematic_focus_list = []
    raw_thematic = research.get("thematic_focus")
    if isinstance(raw_thematic, list):
        thematic_focus_list = [str(x).strip() for x in raw_thematic if x and str(x).strip().lower() not in ("not found", "none", "n/a", "not publicly available", "-", "null")]
    elif isinstance(raw_thematic, str) and raw_thematic.strip():
        thematic_focus_list = [x.strip() for x in raw_thematic.split(",") if x.strip() and x.strip().lower() not in ("not found", "none", "n/a", "not publicly available", "-", "null")]
    thematic_focus_display = ", ".join(thematic_focus_list)

    # Currency/Number fields - Zoho expects real numbers here, not free text
    # like "Rs 19.2 Cr (FY24)" (confirmed live: sending unparseable text into a
    # Number/Currency field raises an INVALID_DATA error). parse_crore()
    # extracts the number in crore, or returns None if it can't - in which case
    # the field is simply left out of the payload rather than guessed at. The
    # original free text is kept separately for the Description block, where a
    # human-readable range like "Rs 50L-2Cr depending on scope" is more useful
    # than a single lossily-parsed number.
    csr_spend_prev_txt = clean_crm_val(research.get("csr_spend_previous_fy"))
    csr_spend_3fy_txt = clean_crm_val(research.get("csr_spend_previous_3fy"))
    edu_csr_spend_txt = clean_crm_val(research.get("education_csr_spend"))
    unspent_amt_txt = clean_crm_val(research.get("unspent_csr_amount"))
    avg_ticket_txt = clean_crm_val(research.get("avg_ticket_size"))

    csr_spend_prev_num = parse_crore(research.get("csr_spend_previous_fy"))
    csr_spend_3fy_num = parse_crore(research.get("csr_spend_previous_3fy"))
    edu_csr_spend_num = parse_crore(research.get("education_csr_spend"))
    unspent_amt_num = parse_crore(research.get("unspent_csr_amount"))
    avg_ticket_num = parse_crore(research.get("avg_ticket_size"))

    # Financial Data 4-Year Series & CSR Min / Max calculations
    fiscal_years = financial_data.get("fiscal_years") or []
    turnover_dict = financial_data.get("turnover") or {}
    pbt_dict = financial_data.get("pbt") or {}
    net_profit_dict = financial_data.get("net_profit") or {}
    net_worth_dict = financial_data.get("net_worth") or {}
    prescribed_csr = financial_data.get("prescribed_csr_cr") or financial_data.get("prescribed_csr")

    def _format_4yr_series(metric_dict):
        parts = []
        for fy in fiscal_years[:4]:
            val = metric_dict.get(fy)
            if val is not None:
                parts.append(f"{fy}: ₹{val:,.2f} Cr" if isinstance(val, (int, float)) else f"{fy}: ₹{val} Cr")
        return " | ".join(parts)

    turnover_4yr_str = _format_4yr_series(turnover_dict)
    pbt_4yr_str = _format_4yr_series(pbt_dict)
    net_profit_4yr_str = _format_4yr_series(net_profit_dict)
    net_worth_4yr_str = _format_4yr_series(net_worth_dict)

    # CSR Maximum (2% of 3-year avg PBT)
    pbt_vals = [pbt_dict[fy] for fy in fiscal_years[:3] if isinstance(pbt_dict.get(fy), (int, float))]
    csr_max_str = ""
    if pbt_vals:
        avg_pbt = sum(pbt_vals) / len(pbt_vals)
        csr_max_val = avg_pbt * 0.02
        csr_max_str = f"₹{csr_max_val:,.2f} Cr" if csr_max_val > 0 else ""

    # CSR Minimum (2% of 3-year avg Net Profit / Prescribed CSR)
    np_vals = [net_profit_dict[fy] for fy in fiscal_years[:3] if isinstance(net_profit_dict.get(fy), (int, float))]
    csr_min_str = ""
    if np_vals:
        avg_np = sum(np_vals) / len(np_vals)
        csr_min_val = avg_np * 0.02
        csr_min_str = f"₹{csr_min_val:,.2f} Cr" if csr_min_val > 0 else ""
    elif prescribed_csr:
        csr_min_str = f"₹{prescribed_csr} Cr" if isinstance(prescribed_csr, (int, float)) else str(prescribed_csr)

    # Build comprehensive description
    desc_sections = []
    sec_lead = ["=== LEAD & FITMENT OVERVIEW ==="]
    if lead_owner: sec_lead.append(f"Lead Found By: {lead_owner}")
    if score_fitment: sec_lead.append(f"Fitment Score: {score_fitment}/100 ({lead_tier or 'Unranked'})")
    if designation: sec_lead.append(f"Decision Maker Designation: {designation}")
    if industry: sec_lead.append(f"Industry: {industry}")
    if city or state: sec_lead.append(f"Location: {', '.join(filter(None, [city, state]))}")
    desc_sections.append("\n".join(sec_lead))

    sec_csr = ["=== CSR & EDUCATION INTELLIGENCE ==="]
    if csr_focus: sec_csr.append(f"CSR Philosophy / Focus: {csr_focus}")
    if thematic_focus_display: sec_csr.append(f"Thematic Focus: {thematic_focus_display}")
    if program_dist_state: sec_csr.append(f"Program Districts & States: {program_dist_state}")
    if edu_csr_spend_txt: sec_csr.append(f"Education CSR Spend: {edu_csr_spend_txt}")
    if csr_spend_prev_txt: sec_csr.append(f"Previous Year Total CSR: {csr_spend_prev_txt}")
    if csr_spend_3fy_txt: sec_csr.append(f"Past 3 Years CSR Spend: {csr_spend_3fy_txt}")
    if unspent_amt_txt: sec_csr.append(f"Unspent CSR Amount: {unspent_amt_txt}")
    if avg_ticket_txt: sec_csr.append(f"Avg Grant / Ticket Size: {avg_ticket_txt}")
    if partners_names_str: sec_csr.append(f"NGO Partners: {partners_names_str}")
    if foundation: sec_csr.append(f"Company Foundation: {foundation}")
    if comm_members_str: sec_csr.append(f"CSR Committee Members: {comm_members_str}")
    desc_sections.append("\n".join(sec_csr))

    fin_lines = []
    if turnover_4yr_str: fin_lines.append(f"  • Annual Turnover: {turnover_4yr_str}")
    if pbt_4yr_str: fin_lines.append(f"  • PBT: {pbt_4yr_str}")
    if net_profit_4yr_str: fin_lines.append(f"  • Net Profit: {net_profit_4yr_str}")
    if net_worth_4yr_str: fin_lines.append(f"  • Net Worth: {net_worth_4yr_str}")
    if csr_max_str: fin_lines.append(f"  • CSR Maximum (2% of PBT): {csr_max_str}")
    if csr_min_str: fin_lines.append(f"  • CSR Minimum (2% of Net Profit): {csr_min_str}")
    if fin_lines:
        desc_sections.append("=== FINANCIAL METRICS (SCREENER.IN) ===\n" + "\n".join(fin_lines))

    if sources_links:
        desc_sections.append(f"=== CITATIONS & SOURCES ===\n{sources_links}")

    full_description = "\n\n".join(desc_sections)

    # BUILD THE ZOHO LEADS PAYLOAD - one key per field, using ONLY the API
    # names confirmed in DonorIQ x Zoho Fields.xlsx (Sheet2, "Field api name").
    # No more guessed aliases: every key below is either a confirmed API name,
    # or (for the still-unconfirmed "ADD-ON ASKS" rows 40-46: annual turnover,
    # PBT, net profit, net worth, CSR max/min, geography fit check) omitted
    # from the payload entirely and folded into the Description text instead,
    # so nothing sends to a field name that might not exist or might be the
    # wrong data type.
    payload = {
        # 1. Lead Owner (Owner) - deliberately NOT sent. Confirmed live: Zoho's
        # Owner field expects a numeric Zoho user id (bigint), not a plain
        # username/email string, and rejects the WHOLE Lead if it gets one.
        # We don't yet have a mapping from app usernames to real Zoho user
        # ids, so omitting it leaves the lead unassigned (or defaults to the
        # uploading API token's user) rather than blocking every upload.
        # "Lead Found By" is still visible in the Description text below.
        "Company": company_name[:120],                     # 2. Company
        "Website": website[:255],                          # 3. Website
        "Lead_Status": lead_status,                         # 4. Lead Status
        "Designation_3": designation[:100],                 # 5. Designation.
        "Industry": industry[:120],                         # 10. Industry
        "Program_State": program_dist_state,                # 14. Program District & State
        "leadchain0__Social_Lead_ID": social_lead_id,       # 15. Social Lead ID
        "First_Name": first_name[:40],                      # 16. First Name
        "Last_Name": last_name[:80],                        # 17. Last Name
        "Lead_Source": lead_source,                         # 19. Lead Source
        "Phone": phone[:50],                                # 20. Phone
        "Mobile": mobile[:50],                              # 21. Mobile
        "Email": email[:100],                               # 22. Email
        "Secondary_Email": sec_email[:100],                 # 23. Secondary Email
        "Office_Landline": landline[:50],                   # 24. Office Landline
        "Priority": priority,                               # 25. Priority
        # 26. Enoble Fitment - confirmed multi-select picklist in Zoho (same
        # jsonarray requirement as Lead_SourceCategory), set separately below.
        "Duration_of_Past_Projects": duration_past,          # 27. Duration of Past Projects
        "Immediate_Action": immediate_action,                # 28. Immediate Action
        "Agency": agency,                                   # 29. Agency
        "Do_they_Have_Company_Foundation": foundation,       # 33. Do they Have Company Foundation
        "Street": street[:255],                             # 34. Street
        "State": state[:100],                               # 35. State
        "City": city[:100],                                 # 36. City
        "Country": country,                                 # 37. Country
        "Zip_Code": zip_code[:20],                          # 38. Zip Code
        "Description": full_description,                    # 39. Description
    }

    # 18. Lead SourceCategory - confirmed multi-select picklist in Zoho (a live
    # upload error showed it expects a JSON array, not a plain string).
    if lead_tier:
        payload["Lead_SourceCategory"] = [lead_tier]

    # 26. Enoble Fitment - also confirmed multi-select picklist (same
    # jsonarray requirement, confirmed by a live INVALID_DATA error).
    if ennoble_fitment_label:
        payload["Enoble_Fitment"] = [ennoble_fitment_label]

    # 11. Company CSR Focus - capped at 120 chars in Zoho (confirmed live -
    # exceeding it rejects the WHOLE Lead record, not just this field).
    if csr_focus:
        payload["Company_CSR_Focus"] = csr_focus[:120]

    # 12. Thematic Focus - Zoho field takes a list (jsonarray)
    if thematic_focus_list:
        payload["Thematic_Focus"] = thematic_focus_list

    # 13. Previous Projects on Education
    if prev_projects_edu:
        payload["Previous_Projects_on_Education"] = prev_projects_edu

    # 31. No. of Existing Implementation Partners (Number field - already an int)
    if partners_count is not None:
        payload["No_of_Existing_Implementation_Partners1"] = partners_count

    # 32. Names of Existing Implementation Partners
    if partners_names_str:
        payload["Names_of_Existing_Implementation_Partners"] = partners_names_str

    # 6-9, 30. Currency/Number fields - only sent when parse_crore() could
    # extract a real number; unparseable text (a range, a vague description)
    # stays out of these fields but is still visible in the Description above.
    if csr_spend_prev_num is not None:
        payload["CSR_Spent_of_Previous_Financial_Year1"] = csr_spend_prev_num
    if csr_spend_3fy_num is not None:
        payload["CSR_Spent_of_Previous_3_Financial_Year1"] = csr_spend_3fy_num
    if edu_csr_spend_num is not None:
        payload["Education_CSR_Spend1"] = edu_csr_spend_num
    if unspent_amt_num is not None:
        payload["Unspent_CSR_Amount"] = unspent_amt_num
    if avg_ticket_num is not None:
        payload["Avg_Ticket_Size_for_Project_Approved1"] = avg_ticket_num

    # Rows 40-46 (annual turnover, pbt, net profit, net worth, CSR max/min,
    # geography fit check) intentionally NOT mapped yet - waiting on the real
    # confirmed API names before wiring them in permanently. That data still
    # shows up in the Description text block above in the meantime.

    # Description is Long Text in Zoho (much higher limit) - never truncate it.
    _UNCAPPED_FIELDS = {"Description"}
    # Zoho rejects the ENTIRE Lead record if even one single-line/picklist field
    # exceeds its configured max length (confirmed live: Company_CSR_Focus at
    # 120 chars). Every field above with a known limit is already explicitly
    # sized; this is a safety net for the rest.
    _SAFE_DEFAULT_MAX_LEN = 120

    # Clean payload: whatever we have goes through as-is; whatever we don't
    # have (None, "", or anything clean_crm_val()/parse_crore() already
    # normalized away - "Not Found", "Not publicly available", etc.) is left
    # out of the payload entirely, so Zoho shows those fields genuinely blank
    # instead of full of placeholder text.
    cleaned_payload = {}
    for k, v in payload.items():
        if v is None or v == "":
            continue
        if isinstance(v, str) and k not in _UNCAPPED_FIELDS and len(v) > _SAFE_DEFAULT_MAX_LEN:
            v = v[:_SAFE_DEFAULT_MAX_LEN]
        cleaned_payload[k] = v

    return cleaned_payload


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
