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
    "total csr expenditure",
    "total csr spend",
    "actual csr expenditure",
    "amount required to be spent",
    "amount actually spent",
    "surplus arising out of csr activities",
    "shortfall in csr expenditure",
    "ongoing project",
    "csr-1",
    "schedule vii",
    "csr annual action plan",
    "project-wise expenditure",
    "wise amount spent",
    "csr obligation for the financial year",
    "amount spent on csr projects",
    "spent a total of",
    "unspent csr",
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
    "total csr expenditure",
    "total csr spend",
    "actual csr expenditure",
    "amount required to be spent",
    "amount actually spent",
    "surplus arising out of csr activities",
    "shortfall in csr expenditure",
    "ongoing project",
    "csr-1",
    "schedule vii",
    "csr annual action plan",
    "project-wise expenditure",
    "wise amount spent",
    "csr obligation",
]


def _table_to_markdown(table: list) -> str:
    """Renders a pdfplumber extract_tables() row-list as a markdown table so
    row/column alignment (e.g. which Schedule VII sector a spend amount
    belongs to) survives being flattened into a single text blob for the LLM."""
    rows = [row for row in table if any((c or "").strip() for c in row)]
    if not rows:
        return ""
    cleaned = [[(c or "").strip().replace("\n", " ").replace("|", "/") for c in row] for row in rows]

    # pdfplumber's default line-based detector occasionally mistakes a running
    # page header/footer (e.g. "ANNUAL REPORT | 2025-2026") for a real table -
    # skip these tiny, content-free matches instead of polluting the LLM input
    # with noise ahead of the actual page text.
    total_chars = sum(len(c) for row in cleaned for c in row)
    if len(cleaned) <= 2 and total_chars < 40:
        return ""

    lines = ["| " + " | ".join(row) + " |" for row in cleaned]
    lines.insert(1, "| " + " | ".join(["---"] * len(cleaned[0])) + " |")
    return "\n".join(lines)


def _tables_markdown_for_pages(pdf_bytes: bytes, page_indices: set) -> dict:
    """Re-scans only the given 0-based page indices with pdfplumber's
    extract_tables() (pypdf can't do table structure). Cheap in practice since
    only a handful of CSR-keyword-matched pages are ever passed in, not the
    whole (often 100+ page) report."""
    tables_by_page = {}
    if not page_indices:
        return tables_by_page
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for idx in page_indices:
                if idx >= len(pdf.pages):
                    continue
                try:
                    tables = pdf.pages[idx].extract_tables()
                except Exception:
                    continue
                md_tables = [md for t in (tables or []) if (md := _table_to_markdown(t))]
                if md_tables:
                    tables_by_page[idx] = "\n\n".join(md_tables)
    except Exception as e:
        print(f"[PDF Warning] CSR table extraction pass failed: {e}")
    return tables_by_page


def extract_csr_section_text(pdf_url: str, max_chars: int = 50000) -> str:
    """
    Scans annual report PDF pages for CSR-related keywords and extracts text.
    Uses ultra-lightweight pypdf streaming to keep memory footprint under 5MB (prevents OOM on Railway).

    For pages matching STRONG_CSR_KEYWORDS (the Schedule VII / CSR Annexure
    compliance table, not narrative prose), also re-extracts the page as a
    markdown table via pdfplumber and prepends it ahead of the flattened text.
    extract_text() alone turns a "Sector | Amount Outlay | Amount Spent" table
    into a wall of numbers with no row/column alignment, which is why
    sector-specific figures (e.g. education spend) were unreliable even when
    the total CSR spend extracted fine - the LLM had no way to tell which
    number belonged to which sector.
    """
    cache_key = make_key("csr-pdf", pdf_url, max_chars)
    cached = get_json(cache_key)
    if isinstance(cached, dict) and isinstance(cached.get("text"), str):
        print(f"[PDF Cache] Hit: {pdf_url}")
        return cached["text"]

    try:
        # Stream response with a safety cap to prevent RAM exhaustion. Scanned/image-heavy
        # annual reports (e.g. BSE Ltd's FY25 report is ~33MB) routinely exceed 25MB, and a
        # truncated download corrupts the PDF trailer/xref table so it can't be parsed at
        # all (not even partially) - so the cap needs real headroom, not just a token buffer.
        response = requests.get(pdf_url, headers=_headers_for(pdf_url), timeout=25, stream=True)
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "")
        if "pdf" not in content_type.lower() and not pdf_url.lower().endswith(".pdf"):
            raise ValueError(f"Response Content-Type is not PDF: {content_type}")

        # Read at most 50MB of content
        max_bytes = 50 * 1024 * 1024
        content_chunks = []
        total_size = 0
        for chunk in response.iter_content(chunk_size=128 * 1024):
            content_chunks.append(chunk)
            total_size += len(chunk)
            if total_size >= max_bytes:
                break
        pdf_bytes = b"".join(content_chunks)

        strong_pages = []  # list of (page_index, text)
        weak_pages = []

        # 1. Try ultra-lightweight pypdf first
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
            for idx, page in enumerate(reader.pages[:150]):  # Limit to 150 pages max
                try:
                    page_text = page.extract_text() or ""
                    lowered = page_text.lower()
                    if any(keyword in lowered for keyword in STRONG_CSR_KEYWORDS):
                        strong_pages.append((idx, page_text))
                    elif any(keyword in lowered for keyword in CSR_KEYWORDS):
                        weak_pages.append((idx, page_text))
                except Exception:
                    continue
        except Exception:
            # 2. Fallback to pdfplumber if pypdf has issues on encrypted streams
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for idx, page in enumerate(pdf.pages[:60]):
                    page_text = page.extract_text() or ""
                    lowered = page_text.lower()
                    if any(keyword in lowered for keyword in STRONG_CSR_KEYWORDS):
                        strong_pages.append((idx, page_text))
                    elif any(keyword in lowered for keyword in CSR_KEYWORDS):
                        weak_pages.append((idx, page_text))

        # Re-extract STRONG (Schedule VII / Annexure) pages as markdown tables
        # so sector-wise amounts keep their row/column alignment.
        tables_by_page = _tables_markdown_for_pages(pdf_bytes, {idx for idx, _ in strong_pages})

        strong_texts = [
            (f"{tables_by_page[idx]}\n\n{text}" if idx in tables_by_page else text)
            for idx, text in strong_pages
        ]
        weak_texts = [text for _, text in weak_pages]

        combined = "\n".join(strong_texts + weak_texts)
        result_text = combined[:max_chars]
        set_json(cache_key, {"text": result_text})
        return result_text

    except Exception as e:
        print(f"[PDF Error] CSR section extraction failed: {e}")
        return ""

