import os
import requests
from dotenv import load_dotenv

load_dotenv()

# ─── Apollo integration is currently DISABLED ────────────────────────────────
# To re-enable: uncomment the function body below and
# un-comment the import + call in app.py.
# ─────────────────────────────────────────────────────────────────────────────

def find_decision_makers_apollo(company_name: str, domain: str = None) -> dict:
    """Apollo.io contact lookup — currently disabled. Returns empty result instantly."""
    return {"contacts": [], "confidence": "Disabled"}

# def find_decision_makers_apollo(company_name: str, domain: str = None) -> dict:
#     api_key = os.getenv("APOLLO_API_KEY")
#     if not api_key:
#         print("[Apollo Warning] APOLLO_API_KEY not found in .env")
#         return {"contacts": []}
#
#     url = "https://api.apollo.io/v1/mixed_people/search"
#
#     payload = {
#         "api_key": api_key,
#         "q_organization_names": [company_name],
#         "person_titles": [
#             # CSR Person
#             "Head of CSR", "CSR Head", "CSR Manager", "CSR Lead", "CSR Director", "Corporate Social Responsibility", "Head CSR", "CSR Officer",
#             # HR Head
#             "Head of HR", "HR Head", "Chief Human Resources Officer", "CHRO", "Head - Human Resources", "VP HR", "Director HR", "Head of Human Resources",
#             # Committee Members
#             "CSR Committee Member", "CSR Committee", "Committee Member", "Board Committee Member", "Member - CSR Committee",
#             # CEO / Managing Director
#             "CEO", "Chief Executive Officer", "Managing Director", "MD", "President",
#             # Sustainability
#             "Head of Sustainability", "Sustainability Head", "Chief Sustainability Officer", "CSO", "Sustainability Lead", "Sustainability Manager", "ESG Head", "VP Sustainability"
#         ],
#         "page": 1,
#         "per_page": 10
#     }
#
#     try:
#         response = requests.post(url, json=payload, timeout=15)
#         response.raise_for_status()
#         data = response.json()
#
#         contacts = []
#         for person in data.get("people", []):
#             contacts.append({
#                 "first_name": person.get("first_name", "Not publicly available"),
#                 "last_name": person.get("last_name", ""),
#                 "designation": person.get("title", "CSR Decision Maker"),
#                 "email": person.get("email") or "Not publicly available",
#                 "linkedin": person.get("linkedin_url", ""),
#                 "phone": person.get("sanitized_phone") or "Not publicly available",
#                 "source": "Apollo.io"
#             })
#
#         return {"contacts": contacts, "confidence": "High" if contacts else "Low"}
#     except Exception as e:
#         print(f"[Apollo Error] {e}")
#         return {"contacts": [], "confidence": "Error"}
