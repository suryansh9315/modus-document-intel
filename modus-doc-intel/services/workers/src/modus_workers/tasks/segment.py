"""
Section boundary detection via heading heuristics.

Uses font-size proxies from pdfplumber char data + regex for numbered headings.
Falls back to equal 30-page chunks if fewer than 5 sections detected.
"""
from __future__ import annotations

import re
import uuid
import logging

from modus_schemas import PageOCR, SectionBoundary, SectionKind

logger = logging.getLogger(__name__)

HEADING_PATTERNS = [
    # "CHAPTER 1", "SECTION 2", "PART III"
    (r"^(?:CHAPTER|SECTION|PART)\s+[\dIVXLCM]+", SectionKind.CHAPTER),
    # "1. FINANCIAL HIGHLIGHTS" or "10. RISK MANAGEMENT"
    (r"^\d+\.\s+[A-Z][A-Z\s]{5,}$", SectionKind.SECTION),
    # All-caps lines > 10 chars (likely headings)
    (r"^[A-Z][A-Z\s&,\-:]{10,}$", SectionKind.SECTION),
    # Numbered with dot: "2.1 Capital Adequacy"
    (r"^\d+\.\d+\s+[A-Z]", SectionKind.SUBSECTION),
    # Appendix patterns
    (r"^ANNEX(?:URE)?\s+[A-Z\d]", SectionKind.APPENDIX),
]

# Lines to exclude — common false positives
EXCLUDE_PATTERNS = [
    r"^\d+$",                    # page numbers
    r"^[A-Z]{1,3}$",             # abbreviations
    r"^\d{1,2}/\d{1,2}/\d{4}",  # dates
]

FALLBACK_CHUNK_SIZE = 30  # pages per chunk if detection fails
MIN_SECTIONS = 5


def _is_heading(line: str) -> tuple[bool, SectionKind]:
    """Check if a line matches heading patterns."""
    line = line.strip()
    if not line or len(line) < 5:
        return False, SectionKind.UNKNOWN

    # Check excludes first
    for exc in EXCLUDE_PATTERNS:
        if re.match(exc, line):
            return False, SectionKind.UNKNOWN

    for pattern, kind in HEADING_PATTERNS:
        if re.match(pattern, line):
            return True, kind

    return False, SectionKind.UNKNOWN


def detect_sections(
    pages: list[PageOCR], doc_id: str
) -> list[SectionBoundary]:
    """
    Detect section boundaries from OCR'd pages.

    Scans first 5 lines of each page for heading patterns.
    Returns list of SectionBoundary objects with page ranges.
    """
    boundaries: list[SectionBoundary] = []
    current_start = 0
    current_title = "Introduction"
    current_kind = SectionKind.UNKNOWN

    for i, page in enumerate(pages):
        if not page.raw_text:
            continue

        lines = page.raw_text.split("\n")
        # Check first 5 non-empty lines per page
        checked = 0
        for line in lines:
            if not line.strip():
                continue
            is_heading, kind = _is_heading(line)
            if is_heading:
                # Close previous section
                if i > current_start:
                    boundaries.append(
                        SectionBoundary(
                            section_id=str(uuid.uuid4()),
                            doc_id=doc_id,
                            title=current_title[:120],
                            kind=current_kind,
                            start_page=current_start,
                            end_page=i - 1,
                        )
                    )
                current_title = line.strip()[:120]
                current_kind = kind
                current_start = i
                break
            checked += 1
            if checked >= 5:
                break

    # Finalize last section
    if pages:
        boundaries.append(
            SectionBoundary(
                section_id=str(uuid.uuid4()),
                doc_id=doc_id,
                title=current_title[:120],
                kind=current_kind,
                start_page=current_start,
                end_page=len(pages) - 1,
            )
        )

    # Fallback: too few sections detected
    if len(boundaries) < MIN_SECTIONS:
        logger.warning(
            f"Only {len(boundaries)} sections detected — falling back to "
            f"{FALLBACK_CHUNK_SIZE}-page chunks."
        )
        return _fallback_chunks(pages, doc_id)

    logger.info(f"Detected {len(boundaries)} sections.")
    return boundaries


def _fallback_chunks(
    pages: list[PageOCR], doc_id: str
) -> list[SectionBoundary]:
    """Split document into equal-size chunks when heading detection fails."""
    boundaries = []
    total = len(pages)
    for i, start in enumerate(range(0, total, FALLBACK_CHUNK_SIZE)):
        end = min(start + FALLBACK_CHUNK_SIZE - 1, total - 1)
        boundaries.append(
            SectionBoundary(
                section_id=str(uuid.uuid4()),
                doc_id=doc_id,
                title=f"Part {i + 1} (Pages {start + 1}–{end + 1})",
                kind=SectionKind.SECTION,
                start_page=start,
                end_page=end,
            )
        )
    return boundaries
