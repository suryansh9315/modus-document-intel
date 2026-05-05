"""
Hybrid OCR: pdfplumber (fast, text-accurate) with docTR fallback for scanned pages.

Critical: financial tables are serialized as markdown to prevent number hallucination.
"""
from __future__ import annotations

import concurrent.futures
import logging
import threading
from pathlib import Path

import pdfplumber

from modus_schemas import PageOCR

logger = logging.getLogger(__name__)

# Lazy-loaded docTR singleton (avoid import cost when not needed)
_doctr_model = None
_doctr_lock = threading.Lock()


def _get_doctr_model():
    global _doctr_model
    if _doctr_model is None:
        with _doctr_lock:
            if _doctr_model is None:
                from doctr.models import ocr_predictor
                logger.info("Loading docTR model (first use)...")
                _doctr_model = ocr_predictor(pretrained=True)
                logger.info("docTR model loaded.")
    return _doctr_model


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
    Extract text from a single PDF page.

    Strategy:
    1. Try pdfplumber for text-extractable pages.
    2. For pages with tables, serialize tables as markdown and prepend.
    3. Fall back to docTR if text content is insufficient.
    """
    pdf_path = str(pdf_path)
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_no]
        text = page.extract_text() or ""

        # Extract tables if present (prevents number hallucination)
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

        # Use pdfplumber if we got enough text
        if text and len(text.strip()) > 100:
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

    # Fallback: docTR OCR
    return _run_doctr(pdf_path, page_no)


def _run_doctr(pdf_path: str, page_no: int) -> PageOCR:
    """Run docTR OCR on a single page (scanned image)."""
    from doctr.io import DocumentFile

    model = _get_doctr_model()
    # docTR DocumentFile.from_pdf uses 1-based page indexing
    doc = DocumentFile.from_pdf(pdf_path, page_indices=[page_no])
    result = model(doc)

    words = [
        word
        for block in result.pages[0].blocks
        for line in block.lines
        for word in line.words
    ]

    text = " ".join(w.value for w in words)
    conf = (
        sum(w.confidence for w in words) / max(1, len(words))
        if words
        else 0.0
    )

    return PageOCR(
        page_number=page_no,
        raw_text=text,
        confidence=conf,
        ocr_engine="doctr",
        has_tables=False,
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
            logger.error(f"Failed to OCR page {i}: {e}")
            return PageOCR(
                page_number=i,
                raw_text="[OCR ERROR]",
                confidence=0.0,
                ocr_engine="pdfplumber",
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(_safe_extract, range(total_pages)))

    logger.info(f"OCR complete: {total_pages} pages processed in parallel")
    return results
