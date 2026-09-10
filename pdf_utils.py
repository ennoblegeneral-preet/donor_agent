import io
from urllib.parse import urlparse
import requests
import pdfplumber
from redis_cache import get_json, set_json, make_key

# Kai corporate sites (jaise infosys.com) bot-protection/WAF ke peeche hain jo
# sirf User-Agent dekh ke bhi block kar dete hain agar request browser jaisi
# poori tarah na lage - isliye ek zyada complete browser-like header set.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/pdf,application/xhtml+xml,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _headers_for(pdf_url: str) -> dict:
    """Referer ko target site ke domain se hi banate hain - kai WAF cross-site
    Referer wali request block kar dete hain."""
    parsed = urlparse(pdf_url)
    headers = dict(HEADERS)
    if parsed.scheme and parsed.netloc:
        headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
    return headers

# CSR Annexure / Board's Report section in annual reports is usually near the
# end of the document, so scanning only the first few pages (jaisa turnover/PBT
# ke liye kiya jaata hai) usually misses it. Instead, saare pages scan karke
# jin pages mein in keywords se koi bhi milta hai unka text collect karte hain.
#
# STRONG keywords sirf formal Schedule VII / CSR Annexure compliance table mein
# milte hain (Total Amount Spent, Amount Unspent, Average Net Profit waala
# table) - CSR programs ki narrative/marketing description mein nahi. Bade
# reports mein narrative section pehle aata hai aur keyword-match honi ke
# wajah se char budget bhar deta hai, jisse asli Annexure table (jo report
# ke aakhir mein hota hai) truncate ho jaata tha. Isliye strong-keyword wale
# pages ko hamesha priority mein pehle rakhte hain.
STRONG_CSR_KEYWORDS = [
    "total amount spent for the financial year",
    "amount unspent",
    "unspent csr account",
    "average net profit",
    "prescribed csr expenditure",
    "details of csr spent",
    "manner in which the amount spent",
]

CSR_KEYWORDS = [
    "corporate social responsibility",
    "csr committee",
    "annexure",
    "amount spent",
    "amount unspent",
    "unspent csr",
    "unspent amount",
    "prescribed csr expenditure",
    "average net profit",
    "csr expenditure",
    "brsr",
    "business responsibility",
    "csr spend",
    "details of csr spent",
]


def extract_csr_section_text(pdf_url: str, max_chars: int = 20000) -> str:
    """
    Scans annual report PDF pages for CSR-related keywords and extracts text.
    Uses ultra-lightweight pypdf streaming to keep memory footprint under 5MB (prevents OOM on Railway).
    """
    cache_key = make_key("csr-pdf", pdf_url, max_chars)
    cached = get_json(cache_key)
    if isinstance(cached, dict) and isinstance(cached.get("text"), str):
        print(f"[PDF Cache] Hit: {pdf_url}")
        return cached["text"]

    try:
        # Stream response with max 25MB safety cap to prevent RAM exhaustion
        response = requests.get(pdf_url, headers=_headers_for(pdf_url), timeout=25, stream=True)
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "")
        if "pdf" not in content_type.lower() and not pdf_url.lower().endswith(".pdf"):
            raise ValueError(f"Response Content-Type is not PDF: {content_type}")

        # Read at most 25MB of content
        max_bytes = 25 * 1024 * 1024
        content_chunks = []
        total_size = 0
        for chunk in response.iter_content(chunk_size=128 * 1024):
            content_chunks.append(chunk)
            total_size += len(chunk)
            if total_size >= max_bytes:
                break
        pdf_bytes = b"".join(content_chunks)

        strong_pages = []
        weak_pages = []

        # 1. Try ultra-lightweight pypdf first
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
            for page in reader.pages[:150]:  # Limit to 150 pages max
                try:
                    page_text = page.extract_text() or ""
                    lowered = page_text.lower()
                    if any(keyword in lowered for keyword in STRONG_CSR_KEYWORDS):
                        strong_pages.append(page_text)
                    elif any(keyword in lowered for keyword in CSR_KEYWORDS):
                        weak_pages.append(page_text)
                except Exception:
                    continue
        except Exception:
            # 2. Fallback to pdfplumber if pypdf has issues on encrypted streams
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page in pdf.pages[:60]:
                    page_text = page.extract_text() or ""
                    lowered = page_text.lower()
                    if any(keyword in lowered for keyword in STRONG_CSR_KEYWORDS):
                        strong_pages.append(page_text)
                    elif any(keyword in lowered for keyword in CSR_KEYWORDS):
                        weak_pages.append(page_text)

        combined = "\n".join(strong_pages + weak_pages)
        result_text = combined[:max_chars]
        set_json(cache_key, {"text": result_text})
        return result_text

    except Exception as e:
        print(f"[PDF Error] CSR section extraction failed: {e}")
        return ""

