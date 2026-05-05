"""
PDF text extraction via pdfplumber.

Financial tables are serialized as markdown to prevent number hallucination.
"""
from __future__ import annotations

import concurrent.futures
import logging
from pathlib import Path

import pdfplumber

from modus_schemas import PageOCR

logger = logging.getLogger(__name__)


def _table_to_markdown(table: list[list[str | None]]) -> str:
    """Convert pdfplumber table to markdown table string."""
    if not table or not table[0]:
        return ""
    rows = []
    header = table[0]
    rows.append("| " + " | ".join(str(c or "") for c in header) + " |")
    rows.append("| " + " | ".join("---" for _ in header) + " |")
    for row in table[1:]:
        rows.append("| " + " | ".join(str(c or "") for c in row) + " |")
    return "\n".join(rows)


def extract_page(pdf_path: str, page_no: int) -> PageOCR:
    """
    Extract text from a single PDF page using pdfplumber.

    Tables are serialized as markdown and prepended to the text to prevent
    number hallucination by the LLM.
    """
    pdf_path = str(pdf_path)
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_no]
        text = page.extract_text() or ""

        tables = page.extract_tables()
        table_md = ""
        has_tables = bool(tables)
        if tables:
            table_parts = []
            for tbl in tables:
                md = _table_to_markdown(tbl)
                if md:
                    table_parts.append(md)
            table_md = "\n\n".join(table_parts)

        full_text = text
        if table_md:
            full_text = f"[TABLES]\n{table_md}\n\n[TEXT]\n{text}"

        return PageOCR(
            page_number=page_no,
            raw_text=full_text,
            confidence=1.0,
            ocr_engine="pdfplumber",
            has_tables=has_tables,
            table_markdown=table_md if has_tables else None,
        )


def extract_all_pages(pdf_path: str) -> list[PageOCR]:
    """Extract text from all pages of a PDF in parallel."""
    pdf_path = str(pdf_path)
    with pdfplumber.open(pdf_path) as pdf:
        total_pages = len(pdf.pages)

    def _safe_extract(i: int) -> PageOCR:
        try:
            return extract_page(pdf_path, i)
        except Exception as e:
            logger.error(f"Failed to extract page {i}: {e}")
            return PageOCR(
                page_number=i,
                raw_text="[EXTRACTION ERROR]",
                confidence=0.0,
                ocr_engine="pdfplumber",
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(_safe_extract, range(total_pages)))

    logger.info(f"Extraction complete: {total_pages} pages processed in parallel")
    return results
