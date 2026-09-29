"""
Debug tool: shows exactly how an annual report PDF gets parsed for CSR data,
so you can visually verify the pipeline is finding your keywords instead of
just trusting the final JSON output.

Produces an HTML report showing:
  1. Which PDF pages matched your CSR keywords (STRONG vs weak), with the
     matched keywords highlighted in the raw page text.
  2. The text after stage 1 (pdf_utils.extract_csr_section_text - page
     selection + 20,000 char cap).
  3. The text after stage 2 (financial_extractor.filter_csr_annexure_text -
     line-level keyword filter + 4,500 char cap) - each line is marked
     included/dropped with a reason, so you can see exactly what the LLM
     never got to read.
  4. The final structured JSON the LLM extracted from that stage-2 text.

Usage:
    python debug_pdf_parse.py "Company Name"
    python debug_pdf_parse.py "Company Name" --skip-llm
"""
import sys
import io
import os
import re
import json
import html
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

import requests
from pdf_utils import _headers_for, STRONG_CSR_KEYWORDS, CSR_KEYWORDS, _tables_markdown_for_pages
from financial_extractor import CSR_FINANCIAL_KEYWORDS, extract_csr_data, calculate_education_spend_percentage
from search_tool import search_annual_report_pdf_via_screener, search_annual_report_pdf

STAGE1_MAX_CHARS = 50000  # must match pdf_utils.extract_csr_section_text's default
STAGE2_MAX_CHARS = 15000  # must match financial_extractor.filter_csr_annexure_text's default
MAX_PAGES_PYPDF = 150
MAX_PAGES_PDFPLUMBER = 60
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024  # keep in sync with pdf_utils.extract_csr_section_text's cap


def pick_candidates(company_name: str, website: str = None):
    screener_result = search_annual_report_pdf_via_screener(company_name)
    screener_url = screener_result.get("screener_url") if screener_result else None
    report_url = screener_result.get("pdf_url") if screener_result else None
    previous_year = screener_result.get("previous_year") if screener_result else None
    previous_year_candidates = (screener_result.get("previous_year_pdf_url_candidates") if screener_result else None) or []
    latest_year_candidates = (screener_result.get("pdf_url_candidates") if screener_result else None) or []

    latest_year = screener_result.get("latest_year") if screener_result else None
    if latest_year_candidates:
        candidates, report_year = latest_year_candidates, latest_year
    elif previous_year_candidates:
        candidates, report_year = previous_year_candidates, previous_year
    else:
        candidates, report_year = ([report_url] if report_url else []), None

    if not candidates:
        fallback = search_annual_report_pdf(company_name, website)
        candidates = [fallback] if fallback else []
        report_year = None

    return screener_url, candidates, report_year


def fetch_pdf_bytes(pdf_url: str) -> bytes:
    response = requests.get(pdf_url, headers=_headers_for(pdf_url), timeout=30, stream=True)
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "")
    if "pdf" not in content_type.lower() and not pdf_url.lower().endswith(".pdf"):
        raise ValueError(f"Response Content-Type is not PDF: {content_type}")
    chunks, total = [], 0
    for chunk in response.iter_content(chunk_size=128 * 1024):
        chunks.append(chunk)
        total += len(chunk)
        if total >= MAX_DOWNLOAD_BYTES:
            break
    return b"".join(chunks)


def parse_pages(pdf_bytes: bytes):
    """Returns (pages_info, total_pages, engine_used). Mirrors pdf_utils'
    pypdf-first / pdfplumber-fallback logic but keeps per-page detail."""
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        total_pages = len(reader.pages)
        pages_info = []
        for i, page in enumerate(reader.pages[:MAX_PAGES_PYPDF]):
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            pages_info.append(_tag_page(i + 1, text))
        engine = "pypdf"
    except Exception:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            total_pages = len(pdf.pages)
            pages_info = []
            for i, page in enumerate(pdf.pages[:MAX_PAGES_PDFPLUMBER]):
                text = page.extract_text() or ""
                pages_info.append(_tag_page(i + 1, text))
        engine = "pdfplumber (fallback)"

    # Mirrors pdf_utils.extract_csr_section_text's table re-extraction pass:
    # STRONG (Schedule VII / Annexure) pages get re-scanned for tables so the
    # debug view shows exactly what the LLM actually receives now.
    strong_indices = {p["page_num"] - 1 for p in pages_info if p["strong_hits"]}
    tables_by_page = _tables_markdown_for_pages(pdf_bytes, strong_indices)
    for p in pages_info:
        p["table_md"] = tables_by_page.get(p["page_num"] - 1)

    return pages_info, total_pages, engine


def _tag_page(page_num: int, text: str) -> dict:
    lowered = text.lower()
    strong_hits = [k for k in STRONG_CSR_KEYWORDS if k in lowered]
    weak_hits = [k for k in CSR_KEYWORDS if k in lowered and k not in strong_hits]
    return {"page_num": page_num, "text": text, "strong_hits": strong_hits, "weak_hits": weak_hits}


def build_stage1_text(pages_info: list) -> str:
    strong_text = "\n".join(
        (f"{p['table_md']}\n\n{p['text']}" if p.get("table_md") else p["text"])
        for p in pages_info if p["strong_hits"]
    )
    weak_text = "\n".join(p["text"] for p in pages_info if p["weak_hits"] and not p["strong_hits"])
    return (strong_text + "\n" + weak_text)[:STAGE1_MAX_CHARS]


def analyze_stage2_filter(stage1_text: str, max_chars: int = STAGE2_MAX_CHARS):
    """Re-implements financial_extractor.filter_csr_annexure_text but tags
    every line as included/dropped + why, instead of silently discarding."""
    if not stage1_text or len(stage1_text) <= 1500:
        return [], stage1_text or ""

    paragraphs = [p.strip() for p in stage1_text.split("\n") if len(p.strip()) > 15]
    seen = set()
    total_len = 0
    budget_exceeded = False
    rows = []
    for p in paragraphs:
        p_lower = p.lower()
        has_kw = any(k in p_lower for k in CSR_FINANCIAL_KEYWORDS)
        snip = p_lower[:50]
        included, reason = False, ""
        if not has_kw:
            reason = "no CSR/financial keyword on this line"
        elif snip in seen:
            reason = "duplicate line (already included earlier)"
        elif budget_exceeded:
            reason = f"dropped - {max_chars:,} char budget already filled"
        else:
            included = True
            seen.add(snip)
            total_len += len(p)
            if total_len >= max_chars:
                budget_exceeded = True
        rows.append({"text": p, "included": included, "reason": reason})

    final_text = "\n\n".join(r["text"] for r in rows if r["included"]) or stage1_text[:2500]
    return rows, final_text


def highlight(text: str, keywords: list) -> str:
    escaped = html.escape(text)
    if not keywords:
        return escaped
    pattern = "|".join(re.escape(k) for k in sorted(set(keywords), key=len, reverse=True))
    return re.sub(f"({pattern})", r"<mark>\1</mark>", escaped, flags=re.IGNORECASE)


def render_html(ctx: dict) -> str:
    def esc(s):
        return html.escape(str(s)) if s is not None else ""

    matched_pages = [p for p in ctx["pages_info"] if p["strong_hits"] or p["weak_hits"]]

    page_blocks = []
    for p in matched_pages:
        kw_list = p["strong_hits"] + p["weak_hits"]
        badge = '<span class="badge strong">STRONG</span>' if p["strong_hits"] else '<span class="badge weak">weak</span>'
        table_block = f'<p><b>Extracted table (pdfplumber):</b></p><pre>{esc(p["table_md"])}</pre>' if p.get("table_md") else ""
        page_blocks.append(f"""
        <details>
          <summary>{badge} Page {p['page_num']} &mdash; matched: {esc(', '.join(kw_list))}{' + table detected' if p.get('table_md') else ''}</summary>
          {table_block}
          <pre>{highlight(p['text'], kw_list)}</pre>
        </details>""")

    stage2_rows = ctx["stage2_rows"]
    stage2_blocks = []
    for r in stage2_rows:
        cls = "kept" if r["included"] else "dropped"
        note = f'<span class="reason">{esc(r["reason"])}</span>' if r["reason"] else ""
        stage2_blocks.append(f'<div class="line {cls}"><pre>{esc(r["text"])}</pre>{note}</div>')

    dropped_count = sum(1 for r in stage2_rows if not r["included"])
    kept_count = len(stage2_rows) - dropped_count

    csr_data = ctx["csr_data"] or {}
    edu_pct = ctx["edu_pct"] or {}

    return f"""<title>CSR PDF Parse Debug - {esc(ctx['company_name'])}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, Segoe UI, Arial, sans-serif; max-width: 1000px; margin: 0 auto; padding: 24px; line-height: 1.5; }}
  h1, h2 {{ border-bottom: 2px solid #8884; padding-bottom: 6px; }}
  table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
  td, th {{ border: 1px solid #8886; padding: 6px 10px; text-align: left; font-size: 14px; }}
  pre {{ white-space: pre-wrap; word-break: break-word; background: #80808014; padding: 10px; border-radius: 6px; font-size: 13px; max-height: 500px; overflow-y: auto; }}
  mark {{ background: #ffe066; color: #000; padding: 0 2px; border-radius: 2px; }}
  details {{ margin: 8px 0; border: 1px solid #8886; border-radius: 6px; padding: 8px 12px; }}
  summary {{ cursor: pointer; font-weight: 600; }}
  .badge {{ font-size: 11px; padding: 2px 6px; border-radius: 4px; margin-right: 8px; }}
  .badge.strong {{ background: #d9480f; color: #fff; }}
  .badge.weak {{ background: #868e96; color: #fff; }}
  .line {{ border-left: 4px solid #ccc; margin: 4px 0; padding-left: 10px; }}
  .line.kept {{ border-left-color: #2f9e44; }}
  .line.dropped {{ border-left-color: #e03131; opacity: 0.55; }}
  .line pre {{ margin: 2px 0; max-height: none; }}
  .reason {{ font-size: 12px; color: #e03131; }}
  .stats {{ display: flex; gap: 24px; flex-wrap: wrap; margin: 12px 0; }}
  .stat {{ background: #80808014; border-radius: 8px; padding: 10px 16px; }}
  .stat b {{ display: block; font-size: 20px; }}
</style>

<h1>CSR PDF Parse Debug &mdash; {esc(ctx['company_name'])}</h1>
<p>Generated {esc(ctx['timestamp'])}</p>

<h2>1. Source</h2>
<table>
  <tr><th>Screener profile</th><td>{esc(ctx['screener_url']) or 'not found (unlisted)'}</td></tr>
  <tr><th>PDF used</th><td><a href="{esc(ctx['pdf_url'])}">{esc(ctx['pdf_url'])}</a></td></tr>
  <tr><th>Report year</th><td>{esc(ctx['report_year']) or 'latest available'}</td></tr>
  <tr><th>Parser engine</th><td>{esc(ctx['engine'])}</td></tr>
  <tr><th>Candidates tried</th><td>{esc(', '.join(ctx['attempts']))}</td></tr>
</table>

<h2>2. Page scan</h2>
<div class="stats">
  <div class="stat"><b>{ctx['total_pages']}</b>total pages in PDF</div>
  <div class="stat"><b>{ctx['pages_scanned']}</b>pages scanned</div>
  <div class="stat"><b>{sum(1 for p in ctx['pages_info'] if p['strong_hits'])}</b>pages w/ STRONG match</div>
  <div class="stat"><b>{sum(1 for p in ctx['pages_info'] if p['weak_hits'] and not p['strong_hits'])}</b>pages w/ weak-only match</div>
</div>
<p>Only pages that matched a keyword are shown below (matched terms highlighted). STRONG keywords = Schedule VII / CSR Annexure compliance table language; weak = general CSR narrative mentions.</p>
{''.join(page_blocks) if page_blocks else '<p><b>No pages matched any CSR keyword.</b> Either this report genuinely has no CSR section, or it is a scanned/image-only PDF with no extractable text layer (pypdf/pdfplumber return blank text for scans - would need OCR).</p>'}

<h2>3. Stage 1 &rarr; Stage 2 filtering (what actually reaches the LLM)</h2>
<div class="stats">
  <div class="stat"><b>{len(ctx['stage1_text']):,}</b>chars after page selection (stage 1 cap {STAGE1_MAX_CHARS:,})</div>
  <div class="stat"><b>{kept_count}</b>lines kept</div>
  <div class="stat"><b>{dropped_count}</b>lines dropped</div>
  <div class="stat"><b>{len(ctx['stage2_text']):,}</b>chars actually sent to the LLM (cap {STAGE2_MAX_CHARS:,})</div>
</div>
<p>Green border = line kept and sent to the LLM. Red border = line dropped by the stage-2 keyword filter, with the reason. If a data row (e.g. a bare number like a spend trend row) got dropped here, that's why the model reports it as "not found" even though the PDF has it.</p>
<div>{''.join(stage2_blocks) if stage2_blocks else '<p>No stage-1 text to filter.</p>'}</div>

<h2>4. Final extracted JSON</h2>
{'<p><i>Skipped (--skip-llm passed)</i></p>' if ctx['skip_llm'] else f'<pre>{esc(json.dumps(csr_data, indent=2))}</pre>'}

<h2>5. Education spend summary</h2>
{'<p><i>Skipped</i></p>' if ctx['skip_llm'] else f'<pre>{esc(json.dumps(edu_pct, indent=2))}</pre>'}
"""


def main():
    if len(sys.argv) < 2:
        print("Usage: python debug_pdf_parse.py \"Company Name\" [--skip-llm]")
        sys.exit(1)

    company_name = sys.argv[1]
    skip_llm = "--skip-llm" in sys.argv[2:]

    print(f"[1/4] Finding annual report PDF for {company_name}...")
    screener_url, candidates, report_year = pick_candidates(company_name)
    if not candidates:
        print("No candidate PDF found via Screener or general web search. Aborting.")
        sys.exit(1)

    pages_info, total_pages, engine, pdf_url, attempts = [], 0, "", None, []
    for candidate_url in candidates:
        attempts.append(candidate_url)
        print(f"[2/4] Downloading + parsing: {candidate_url}")
        try:
            pdf_bytes = fetch_pdf_bytes(candidate_url)
            pages_info, total_pages, engine = parse_pages(pdf_bytes)
            pdf_url = candidate_url
            break
        except Exception as e:
            print(f"  failed ({e}), trying next candidate if any...")

    if not pdf_url:
        print("All candidate PDFs failed to download/parse. Aborting.")
        sys.exit(1)

    print(f"[3/4] Building stage 1 / stage 2 filtered text...")
    stage1_text = build_stage1_text(pages_info)
    stage2_rows, stage2_text = analyze_stage2_filter(stage1_text)

    csr_data, edu_pct = None, None
    if not skip_llm and stage2_text:
        print(f"[4/4] Calling LLM to extract structured CSR data...")
        csr_data, err = extract_csr_data(stage2_text, company_name)
        if err:
            print(f"  LLM extraction failed: {err.get('message')}")
        if csr_data:
            edu_pct = calculate_education_spend_percentage(csr_data)
    else:
        print("[4/4] Skipping LLM call.")

    html_out = render_html({
        "company_name": company_name,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "screener_url": screener_url,
        "pdf_url": pdf_url,
        "report_year": report_year,
        "engine": engine,
        "attempts": attempts,
        "total_pages": total_pages,
        "pages_scanned": len(pages_info),
        "pages_info": pages_info,
        "stage1_text": stage1_text,
        "stage2_rows": stage2_rows,
        "stage2_text": stage2_text,
        "csr_data": csr_data,
        "edu_pct": edu_pct,
        "skip_llm": skip_llm,
    })

    os.makedirs("debug_reports", exist_ok=True)
    safe_name = "".join(c for c in company_name if c.isalnum() or c in (" ", "-", "_")).strip().replace(" ", "_")
    out_path = os.path.join("debug_reports", f"{safe_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_out)

    print(f"\nDone. Open this file in your browser:\n  {os.path.abspath(out_path)}")


if __name__ == "__main__":
    main()
