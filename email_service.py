# """
# email service fro seending research reports
# """
# import smtplib
# from email.mime.base import MIMEMultipart
# from email.mime.text import MIMEBase
# from email import encoders
# from io import BytesIO
# import os
# from dotenv import load_dotenv

# load_dotenv()

# class EmailService:
#     def __init__(self):
#         self.smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
#         self.smtp_port = int(os.getenv("SMTP_PORT", 587))
#         self.sender_email = os.getenv("SENDER_EMAIL", "[EMAIL_ADDRESS]")
#         self.sender_password = os.getenv("SENDER_PASSWORD", "12345")
#         self.sender_name = os.getenv("SENDER_NAME", "Research Pipeline")

#     def send_research_report(
#         self,
#         recipient_email: str,
#         company_name: str,
#         pdf_buffer: BytesIO,
#         filename: str,
#         recipient_name: str = None
#     ) -> dict:
#      """
#         Send research report PDF via email
        
#         Args:
#             recipient_email: Email address to send to
#             company_name: Name of the company
#             pdf_buffer: BytesIO buffer containing PDF
#             filename: Filename for the attachment
#             recipient_name: Optional name of recipient
            
#         Returns:
#             dict with 'success' and 'message' keys
#         """

#     try: 
#         if not self.sender_email or not self.sender_password:
#             return {
#                 "success": False,
#                 "message": "Email not configured. Set SENDER_EMAIL and SENDER_PASSWORD in .env"
#             }

#             message = MIMEMultipart()
#             message["From"] = fromataddr((self.sender_name, self.sender_email))
#             message["To"] = recipient_email
#             message["Subject"] = f"Research Report: {company_name}"

# body = f"""
# Dear {recipient_name or 'Team'},

# Please find attached the research report for {company_name}.

# This report contains detailed CSR research data including:
# - Company information and location
# - CSR focus and themes
# - CSR spending information
# - Implementation partners
# - Geographic priorities

# Best regards,
# {self.sender_name}

# ---
# This is an automated email from the Donor Partner Research Pipeline System.
# """

# message.attach(MIMEText(body, "plain"))

# pdf_buffer.seek(0)
# part = MIMEBase("application", "octet-stream")
# part.set_payload(pdf_buffer.read())
# encoders.encode_base64(part)
# part.add_header(
#     "Content-Disposition"
#     f"attachment; filename={filename}",

# )
# meassage.attach(part)

# with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
#     server.starttls()
#     server.login(self.sender_email, self.smtp_port) as server:
#     server.starttls()
#     server.login(self.sender_email, self.sender_password)
#     server.send_message(message)
#     return{
#         "success": True,
#         "message" :f"Research report sent successfully to {recipient_email}" 
#     }

"""
Email service for sending research reports
"""
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email.utils import formataddr
from email import encoders
from io import BytesIO
import os
from dotenv import load_dotenv

load_dotenv()

class EmailService:
    def __init__(self):
        self.smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.sender_email = os.getenv("SENDER_EMAIL")
        self.sender_password = os.getenv("SENDER_PASSWORD")
        self.sender_name = os.getenv("SENDER_NAME", "Research Pipeline")
        
    def send_research_report(
        self, 
        recipient_email: str,
        company_name: str,
        pdf_buffer: BytesIO,
        filename: str,
        recipient_name: str = None
    ) -> dict:
        """
        Send research report PDF via email
        
        Args:
            recipient_email: Email address to send to
            company_name: Name of the company
            pdf_buffer: BytesIO buffer containing PDF
            filename: Filename for the attachment
            recipient_name: Optional name of recipient
            
        Returns:
            dict with 'success' and 'message' keys
        """
        try:
            # Validate email configuration
            if not self.sender_email or not self.sender_password:
                return {
                    "success": False,
                    "message": "Email not configured. Set SENDER_EMAIL and SENDER_PASSWORD in .env"
                }
            
            # Create message
            message = MIMEMultipart()
            message["From"] = formataddr((self.sender_name, self.sender_email))
            message["To"] = recipient_email
            message["Subject"] = f"Research Report: {company_name}"
            
            # Email body
            body = f"""
Dear {recipient_name or 'Team'},

Please find attached the research report for {company_name}.

This report contains detailed CSR research data including:
- Company information and location
- CSR focus and themes
- CSR spending information
- Implementation partners
- Geographic priorities

Best regards,
{self.sender_name}

---
This is an automated email from the Donor Partner Research Pipeline System.
"""
            
            message.attach(MIMEText(body, "plain"))
            
            # Attach PDF
            pdf_buffer.seek(0)
                        # Line 181: Set Content-Type for XLSX file
            part = MIMEBase("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")

            # part = MIMEBase("application", "octet-stream")
            part.set_payload(pdf_buffer.read())
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                f"attachment; filename= {filename}",
            )
            message.attach(part)
            
            # Send email
            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                server.starttls()
                server.login(self.sender_email, self.sender_password)
                server.send_message(message)
            
            return {
                "success": True,
                "message": f"Research report sent successfully to {recipient_email}"
            }
            
        except smtplib.SMTPAuthenticationError:
            return {
                "success": False,
                "message": "Email authentication failed. Check SENDER_EMAIL and SENDER_PASSWORD."
            }
        except smtplib.SMTPException as e:
            return {
                "success": False,
                "message": f"SMTP error: {str(e)}"
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"Error sending email: {str(e)}"
            }


# def send_research_pdf(company_id: str, recipient_email: str, recipient_name: str = None) -> dict:
#     """
#     Helper function to fetch company data and send research PDF
#     """
#     from db import get_company
#     from pdf_service import generate_research_pdf, generate_research_filename
    
#     # Get company data
#     company = get_company(company_id)
#     if not company:
#         return {
#             "success": False,
#             "message": "Company not found"
#         }
    
#     # Generate PDF
#     pdf_buffer = generate_research_pdf(company)
#     filename = generate_research_filename(company["company_name"])
    
#     # Send email
#     email_service = EmailService()
#     return email_service.send_research_report(
#         recipient_email=recipient_email,
#         company_name=company["company_name"],
#         pdf_buffer=pdf_buffer,
#         filename=filename,
#         recipient_name=recipient_name
#     )

# Line 218-245
def send_research_excel(company_id: str, recipient_email: str, recipient_name: str = None) -> dict:
    from db import get_company
    from excel_service import generate_research_excel, generate_research_excel_filename
    
    # Get company data
    company = get_company(company_id)
    if not company:
        return {
            "success": False,
            "message": "Company not found"
        }
    
    # Generate Excel sheet
    excel_buffer = generate_research_excel(company)
    filename = generate_research_excel_filename(company["company_name"])
    
    # Send email with Excel attachment
    email_service = EmailService()
    return email_service.send_research_report(
        recipient_email=recipient_email,
        company_name=company["company_name"],
        pdf_buffer=excel_buffer,  # Pass the Excel buffer
        filename=filename,
        recipient_name=recipient_name
    )


def send_combined_research_excel(company_ids: list, recipient_email: str, recipient_name: str = None) -> dict:
    from db import get_company
    from excel_service import generate_combined_research_excel, generate_combined_research_excel_filename

    companies = [get_company(company_id) for company_id in company_ids]
    companies = [company for company in companies if company]
    if not companies:
        return {"success": False, "message": "No selected companies were found."}

    excel_buffer = generate_combined_research_excel(companies)
    email_service = EmailService()
    return email_service.send_research_report(
        recipient_email=recipient_email,
        company_name=f"{len(companies)} selected companies",
        pdf_buffer=excel_buffer,
        filename=generate_combined_research_excel_filename(),
        recipient_name=recipient_name,
    )


