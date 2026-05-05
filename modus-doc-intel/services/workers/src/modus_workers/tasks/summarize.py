"""
Hierarchical compression tree: L1 (per-section) → L2 (cluster) → L3 (global).

Numbers always pass through verbatim — never paraphrased.
L1 runs with up to 4 concurrent calls (Semaphore(4)) — no throttling needed with Cerebras.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import TYPE_CHECKING

from modus_schemas import (
    ClusterDigest,
    ExtractedClaim,
    GlobalDigest,
    PageOCR,
    SectionBoundary,
    SectionSummary,
)
from modus_prompts import PromptRegistry

if TYPE_CHECKING:
    from modus_workers.groq_client import GroqClient

from modus_workers.groq_client import FAST_MODEL, PRIMARY_MODEL

logger = logging.getLogger(__name__)

# Max characters to send per section chunk (~8K chars ≈ ~2K tokens)
MAX_SECTION_CHARS = 8_000

# Overlap between consecutive chunks so context isn't lost at boundaries
CHUNK_OVERLAP = 500

# Merge sections with fewer than this many pages before L1 to reduce API calls
MIN_MERGE_PAGES = 4

# Cerebras free tier: 30 req/min for llama3.1-8b = 1 req per 2s
# 3.0s interval → ≤ 20 req/min; leaves headroom for L2/L3 calls using the same model.
_semaphore = asyncio.Semaphore(1)
_REQUEST_INTERVAL = 6.0  # minimum seconds between requests
_TPM_LIMIT = 55_000       # conservative budget under Cerebras free tier 60k TPM


def _pages_for_section(
    pages: list[PageOCR], section: SectionBoundary
) -> str:
    """Concatenate page texts for a section.

    Note: raw_text already contains table markdown when tables are present
    (set by extract_page as '[TABLES]...\\n\\n[TEXT]...'), so no separate
    table_markdown prepend is needed here.
    """
    parts = []
    for page in pages:
        if section.start_page <= page.page_number <= section.end_page:
            parts.append(f"[Page {page.page_number + 1}]\n{page.raw_text}")
    return "\n\n".join(parts)


def _split_into_chunks(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Split text into overlapping chunks of at most chunk_size characters."""
    if len(text) <= chunk_size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = end - overlap
    return chunks


async def _generate_l1_for_chunk(
    section: SectionBoundary,
    chunk_text: str,
    groq_client: "GroqClient",
) -> tuple[dict, int]:
    """Run one LLM call for a single chunk of section text. Returns (parsed_data, tokens)."""
    messages = PromptRegistry.render_messages(
        "section_summary", {"section_text": chunk_text}
    )
    async with _semaphore:
        try:
            raw, tokens_used = await groq_client.complete_with_usage(
                messages,
                model=FAST_MODEL,
                response_format={"type": "json_object"},
            )
        except Exception as e:
            logger.error(f"L1 chunk generation failed for section {section.section_id}: {e}")
            await asyncio.sleep(_REQUEST_INTERVAL)
            return {}, 0

        # Token-aware sleep: ensure we don't exceed _TPM_LIMIT sustained throughput.
        # Each call sleeps proportionally to tokens consumed; minimum _REQUEST_INTERVAL.
        token_sleep = (tokens_used / _TPM_LIMIT) * 60
        await asyncio.sleep(max(_REQUEST_INTERVAL, token_sleep))

    # Strip markdown fences — llama3.1-8b wraps JSON in ```json ... ``` despite instructions
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.lstrip("`").lstrip("json").strip()
        cleaned = cleaned.rsplit("```", 1)[0].strip()

    try:
        return json.loads(cleaned), tokens_used
    except json.JSONDecodeError:
        # Fallback: find the first {...} block in the response
        brace_match = re.search(r"\{[\s\S]+\}", cleaned)
        if brace_match:
            try:
                return json.loads(brace_match.group(0)), tokens_used
            except json.JSONDecodeError:
                pass
        logger.warning(
            f"L1 chunk JSON parse failed for section {section.section_id} — "
            f"raw_preview={raw[:300]!r}"
        )
        return {"summary_text": cleaned[:2000]}, tokens_used


def _merge_chunk_data(
    chunk_data_list: list[dict], section: SectionBoundary
) -> SectionSummary:
    """Merge L1 outputs from multiple chunks into a single SectionSummary.

    - summary_text: concatenated with separator
    - key_metrics: union dict, later chunks override earlier on the same key
    - key_entities: deduplicated union
    - key_risks: deduplicated union
    - claims: concatenated, deduplicated by claim_text
    """
    summary_parts = [
        d.get("summary_text") or ""
        for d in chunk_data_list
        if d.get("summary_text")
    ]
    summary_text = " [...] ".join(summary_parts) if summary_parts else ""

    # key_metrics: union, later chunks override on the same key
    all_metrics: dict[str, str] = {}
    for d in chunk_data_list:
        raw = d.get("key_metrics") or {}
        if isinstance(raw, dict):
            all_metrics.update({k: str(v) for k, v in raw.items() if v is not None})

    # key_entities: deduplicated union preserving order
    seen_entities: set[str] = set()
    key_entities: list[str] = []
    for d in chunk_data_list:
        for item in (d.get("key_entities") or []):
            name = item["name"] if isinstance(item, dict) else str(item)
            if name not in seen_entities:
                seen_entities.add(name)
                key_entities.append(name)

    # key_risks: deduplicated union preserving order
    seen_risks: set[str] = set()
    key_risks: list[str] = []
    for d in chunk_data_list:
        for item in (d.get("key_risks") or []):
            desc = item["description"] if isinstance(item, dict) else str(item)
            if desc not in seen_risks:
                seen_risks.add(desc)
                key_risks.append(desc)

    # claims: concatenated, deduplicated by claim_text
    all_claims: list[ExtractedClaim] = []
    seen_claim_texts: set[str] = set()
    for d in chunk_data_list:
        for c in (d.get("claims") or []):
            if not isinstance(c, dict):
                continue
            text = str(c.get("claim_text") or "")
            if text in seen_claim_texts:
                continue
            seen_claim_texts.add(text)
            all_claims.append(ExtractedClaim(
                claim_id=str(uuid.uuid4()),
                doc_id=section.doc_id,
                section_id=section.section_id,
                page_number=section.start_page,
                claim_text=text,
                claim_type=c.get("claim_type"),
                subject=str(c.get("subject") or ""),
                value=str(v) if (v := c.get("value")) is not None else None,
                confidence=float(c.get("confidence") or 1.0),
            ))

    return SectionSummary(
        section_id=section.section_id,
        doc_id=section.doc_id,
        summary_text=summary_text,
        key_metrics=all_metrics,
        key_entities=key_entities,
        key_risks=key_risks,
        claims=all_claims,
    )


async def generate_l1(
    section: SectionBoundary,
    pages: list[PageOCR],
    groq_client: "GroqClient",
) -> tuple[SectionSummary, int]:
    """Generate L1 section summary via Llama-8B. Returns (summary, tokens_used).

    P2-3: For sections exceeding MAX_SECTION_CHARS, splits into overlapping chunks,
    generates one L1 per chunk, then merges key_metrics, key_risks, and claims
    across all chunks so no content is silently truncated.
    """
    section_text = _pages_for_section(pages, section)
    chunks = _split_into_chunks(section_text, MAX_SECTION_CHARS, CHUNK_OVERLAP)

    if len(chunks) > 1:
        logger.info(
            f"L1: section {section.section_id!r} split into {len(chunks)} chunks "
            f"({len(section_text)} chars)"
        )

    chunk_data_list: list[dict] = []
    total_tokens = 0
    for chunk_text in chunks:
        data, tokens = await _generate_l1_for_chunk(section, chunk_text, groq_client)
        chunk_data_list.append(data)
        total_tokens += tokens

    if not chunk_data_list or all(not d for d in chunk_data_list):
        return SectionSummary(
            section_id=section.section_id,
            doc_id=section.doc_id,
            summary_text="[Summary unavailable]",
            key_metrics={},
            key_entities=[],
            key_risks=[],
            claims=[],
        ), total_tokens

    return _merge_chunk_data(chunk_data_list, section), total_tokens


def merge_small_sections(
    sections: list[SectionBoundary], min_pages: int = MIN_MERGE_PAGES
) -> list[SectionBoundary]:
    """
    Merge sections below min_pages into their next neighbor (or previous if last).
    Reduces total L1 API calls without changing the architecture.
    """
    if not sections:
        return sections

    merged: list[SectionBoundary] = []
    i = 0
    while i < len(sections):
        current = sections[i]
        if current.page_count < min_pages and i + 1 < len(sections):
            nxt = sections[i + 1]
            merged.append(SectionBoundary(
                section_id=current.section_id,
                doc_id=current.doc_id,
                title=f"{current.title} / {nxt.title}",
                kind=current.kind,
                start_page=current.start_page,
                end_page=nxt.end_page,
            ))
            i += 2
        else:
            merged.append(current)
            i += 1

    # If the last section is still small, absorb it into the previous
    if len(merged) >= 2 and merged[-1].page_count < min_pages:
        prev, last = merged[-2], merged[-1]
        merged[-2] = SectionBoundary(
            section_id=prev.section_id,
            doc_id=prev.doc_id,
            title=f"{prev.title} / {last.title}",
            kind=prev.kind,
            start_page=prev.start_page,
            end_page=last.end_page,
        )
        merged.pop()

    return merged


async def generate_l1_batch(
    sections: list[SectionBoundary],
    pages: list[PageOCR],
    groq_client: "GroqClient",
) -> list[SectionSummary]:
    """
    Generate L1 summaries concurrently (up to 4 in-flight via _semaphore).

    Caller is expected to pass pre-merged sections (e.g. from merge_small_sections).
    """
    total = len(sections)
    logger.info(f"L1: {total} sections after merging small ones")

    async def _run(i: int, section: SectionBoundary) -> SectionSummary:
        logger.info(f"L1 {i + 1}/{total}: {section.title!r} (pages {section.start_page}–{section.end_page})")
        try:
            summary, _ = await generate_l1(section, pages, groq_client)
            return summary
        except Exception as e:
            logger.error(f"L1 {i + 1}/{total}: section {section.section_id!r} failed: {e}")
            from modus_schemas import SectionSummary as _SS
            return _SS(
                section_id=section.section_id,
                doc_id=section.doc_id,
                summary_text="[Summary unavailable]",
                key_metrics={},
                key_entities=[],
                key_risks=[],
                claims=[],
            )

    results = await asyncio.gather(*[_run(i, s) for i, s in enumerate(sections)])
    return list(results)


def cluster_summaries(
    summaries: list[SectionSummary], target_size: int = 6
) -> list[list[SectionSummary]]:
    """Cluster summaries by page proximity into groups of ~target_size."""
    return [
        summaries[i : i + target_size]
        for i in range(0, len(summaries), target_size)
    ]


async def generate_l2(
    cluster: list[SectionSummary],
    doc_id: str,
    cluster_index: int,
    groq_client: "GroqClient",
) -> ClusterDigest:
    """Generate L2 cluster digest from a group of L1 summaries."""
    summaries_text = "\n\n---\n\n".join(
        f"SECTION: {s.section_id}\n{s.summary_text}\nKEY METRICS: {s.key_metrics}"
        for s in cluster
    )

    messages = PromptRegistry.render_messages(
        "cluster_digest",
        {
            "summaries_text": summaries_text,
            "section_count": len(cluster),
        },
    )

    async with _semaphore:
        try:
            raw, tokens_used = await groq_client.complete_with_usage(
                messages,
                model=PRIMARY_MODEL,
                response_format={"type": "json_object"},
            )
        except Exception as e:
            logger.error(f"L2 generation failed for cluster {cluster_index}: {e}")
            await asyncio.sleep(_REQUEST_INTERVAL)
            return ClusterDigest(
                cluster_id=str(uuid.uuid4()),
                doc_id=doc_id,
                digest_text=f"[Cluster digest unavailable: {e}]",
                section_ids=[s.section_id for s in cluster],
                cluster_index=cluster_index,
            )
        token_sleep = (tokens_used / _TPM_LIMIT) * 60
        await asyncio.sleep(max(_REQUEST_INTERVAL, token_sleep))

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {"digest_text": raw[:3000]}

    # P2-2: capture consolidated_metrics from the LLM's JSON output
    raw_metrics = data.get("consolidated_metrics") or {}
    consolidated_metrics = {k: str(v) for k, v in raw_metrics.items() if v is not None} if isinstance(raw_metrics, dict) else {}

    return ClusterDigest(
        cluster_id=str(uuid.uuid4()),
        doc_id=doc_id,
        digest_text=data.get("digest_text") or "",
        section_ids=[s.section_id for s in cluster],
        cluster_index=cluster_index,
        consolidated_metrics=consolidated_metrics,
    )


async def generate_l3(
    clusters: list[ClusterDigest],
    doc_id: str,
    total_pages: int,
    groq_client: "GroqClient",
) -> GlobalDigest:
    """Generate L3 global digest from all cluster digests."""
    cluster_text = "\n\n---\n\n".join(
        f"CLUSTER {c.cluster_index + 1} (Sections: {', '.join(c.section_ids[:3])}...):\n{c.digest_text}"
        for c in clusters
    )

    messages = PromptRegistry.render_messages(
        "global_digest",
        {
            "cluster_text": cluster_text,
            "cluster_count": len(clusters),
            "total_pages": total_pages,
        },
    )

    async with _semaphore:
        try:
            raw, tokens_used = await groq_client.complete_with_usage(
                messages,
                model=PRIMARY_MODEL,
                response_format={"type": "json_object"},
            )
        except Exception as e:
            logger.error(f"L3 generation failed: {e}")
            await asyncio.sleep(_REQUEST_INTERVAL)
            return GlobalDigest(
                doc_id=doc_id,
                digest_text=f"[Global digest unavailable: {e}]",
                executive_summary="",
            )
        token_sleep = (tokens_used / _TPM_LIMIT) * 60
        await asyncio.sleep(max(_REQUEST_INTERVAL, token_sleep))

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {"digest_text": raw[:3000], "executive_summary": ""}

    # P2-1: capture top_metrics and top_risks from the LLM's JSON output
    raw_top_metrics = data.get("top_metrics") or {}
    top_metrics = {k: str(v) for k, v in raw_top_metrics.items() if v is not None} if isinstance(raw_top_metrics, dict) else {}
    top_risks = [str(r) for r in (data.get("top_risks") or []) if r]

    return GlobalDigest(
        doc_id=doc_id,
        digest_text=data.get("digest_text") or "",
        executive_summary=data.get("executive_summary") or "",
        top_metrics=top_metrics,
        top_risks=top_risks,
    )
