"""
PDF generation service for research data export
"""
from reportlab.lib.pagesizes import letter, A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
from io import BytesIO
from datetime import datetime

def generate_research_pdf(company_data: dict) -> BytesIO:
    """
    Generate a PDF document containing company research data
    Returns BytesIO object with PDF content
    """
    # Create PDF buffer
    buffer = BytesIO()
    
    # Create document
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=0.75*inch,
        leftMargin=0.75*inch,
        topMargin=0.75*inch,
        bottomMargin=0.75*inch,
    )
    
    # Container for PDF elements
    elements = []
    
    # Define styles
    styles = getSampleStyleSheet()
    
    # Title style
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=20,
        textColor=colors.HexColor('#1a1a1a'),
        spaceAfter=12,
        alignment=TA_CENTER,
        fontName='Helvetica-Bold'
    )
    
    # Company name style
    company_style = ParagraphStyle(
        'CompanyName',
        parent=styles['Heading2'],
        fontSize=16,
        textColor=colors.HexColor('#2c3e50'),
        spaceAfter=6,
        fontName='Helvetica-Bold'
    )
    
    # Section header style
    section_style = ParagraphStyle(
        'SectionHeader',
        parent=styles['Heading3'],
        fontSize=12,
        textColor=colors.HexColor('#34495e'),
        spaceAfter=8,
        spaceBefore=8,
        fontName='Helvetica-Bold'
    )
    
    # Data item style
    data_style = ParagraphStyle(
        'DataItem',
        parent=styles['Normal'],
        fontSize=10,
        textColor=colors.HexColor('#2c3e50'),
        spaceAfter=6,
        leading=14
    )
    
    # Add title
    elements.append(Paragraph("Company Research Report", title_style))
    elements.append(Spacer(1, 0.2*inch))
    
    # Add metadata
    metadata_text = f"Generated on {datetime.now().strftime('%B %d, %Y at %I:%M %p')}"
    metadata_style = ParagraphStyle(
        'Metadata',
        parent=styles['Normal'],
        fontSize=9,
        textColor=colors.grey,
        alignment=TA_CENTER
    )
    elements.append(Paragraph(metadata_text, metadata_style))
    elements.append(Spacer(1, 0.3*inch))
    
    # Extract research data
    research = company_data.get('research_json', {})
    company_name = company_data.get('company_name', 'Unknown')
    
    # Add company name
    elements.append(Paragraph(f"Company: {company_name}", company_style))
    elements.append(Spacer(1, 0.15*inch))
    
    # Add research section header
    elements.append(Paragraph("Company Research", section_style))
    
    # Research data fields
    research_fields = [
        ('Industry', research.get('industry', 'Not Found')),
        ('CSR Themes', ', '.join(research.get('thematic_focus', [])) if research.get('thematic_focus') else 'Not Found'),
        ('Geography', research.get('geographical_priority', 'Not Found')),
        ('CSR Focus', research.get('company_csr_focus', 'Not Found')),
        ('Past CSR Programs', research.get('previous_education_projects', 'Not Found')),
        ('Avg Ticket Size', research.get('avg_ticket_size', 'Not Found')),
        ('Program District/State', research.get('program_district_state', 'Not Found')),
        ('Education CSR Spend', research.get('education_csr_spend', 'Not Found')),
        ('CSR Spend (Previous FY)', research.get('csr_spend_previous_fy', 'Not Found')),
        ('CSR Spend (3 FY)', research.get('csr_spend_previous_3fy', 'Not Found')),
        ('Implementation Partners', ', '.join(research.get('existing_implementation_partners', [])) if research.get('existing_implementation_partners') else 'Not Found'),
        ('Website', research.get('website', 'Not Found')),
        ('City', research.get('city', 'Not Found')),
        ('State', research.get('state', 'Not Found')),
        ('Has Company Foundation', research.get('has_company_foundation', 'Not Found')),
        ('Data Confidence', research.get('confidence', 'unverified')),
    ]
    
    # Create table for research data
    table_data = [['<b>Field</b>', '<b>Value</b>']]
    
    for field_name, field_value in research_fields:
        # Skip empty values
        if field_value and field_value != 'Not Found':
            table_data.append([
                Paragraph(f"<b>{field_name}:</b>", data_style),
                Paragraph(str(field_value), data_style)
            ])
        elif field_value == 'Not Found':
            table_data.append([
                Paragraph(f"<b>{field_name}:</b>", data_style),
                Paragraph("<i>Not Found</i>", data_style)
            ])
    
    # Create table with styling
    table = Table(table_data, colWidths=[2.5*inch, 3.5*inch])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#34495e')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 11),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
        ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#bdc3c7')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8f9fa')]),
        ('LEFTPADDING', (0, 0), (-1, -1), 10),
        ('RIGHTPADDING', (0, 0), (-1, -1), 10),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    
    elements.append(table)
    elements.append(Spacer(1, 0.3*inch))
    
    # Add source URL if available
    source_url = research.get('source_url', '')
    if source_url and source_url != 'Not Found':
        elements.append(Paragraph("Source Information", section_style))
        source_text = f"<b>Primary Source:</b> {source_url}"
        elements.append(Paragraph(source_text, data_style))
        elements.append(Spacer(1, 0.2*inch))
    
    # Add footer
    footer_style = ParagraphStyle(
        'Footer',
        parent=styles['Normal'],
        fontSize=8,
        textColor=colors.grey,
        alignment=TA_CENTER
    )
    elements.append(Spacer(1, 0.2*inch))
    elements.append(Paragraph(
        "This report was automatically generated by the Donor Partner Research Pipeline System",
        footer_style
    ))
    
    # Build PDF
    doc.build(elements)
    
    # Move to beginning of buffer
    buffer.seek(0)
    return buffer


def generate_research_filename(company_name: str) -> str:
    """Generate a filename for the research PDF"""
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    safe_name = "".join(c for c in company_name if c.isalnum() or c in (' ', '-', '_')).rstrip()
    return f"Research_{safe_name}_{timestamp}.pdf"
