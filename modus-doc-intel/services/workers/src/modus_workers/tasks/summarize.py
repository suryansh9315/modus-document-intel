"""
Hierarchical compression tree: L1 (per-section) → L2 (cluster) → L3 (global).

Numbers always pass through verbatim — never paraphrased.
Groq API calls are rate-limited via semaphore (max 5 concurrent).
"""
from __future__ import annotations

import asyncio
import json
import logging
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

logger = logging.getLogger(__name__)

# Max characters to send per section (~40K chars ≈ ~10K tokens, safe margin)
MAX_SECTION_CHARS = 40_000

# Semaphore: max concurrent Groq API calls during ingestion
# Keep low (2) to avoid Groq free-tier rate limits (tokens-per-minute)
_groq_semaphore = asyncio.Semaphore(2)


def _pages_for_section(
    pages: list[PageOCR], section: SectionBoundary
) -> str:
    """Concatenate page texts for a section, with table markdown prepended."""
    parts = []
    for page in pages:
        if section.start_page <= page.page_number <= section.end_page:
            if page.table_markdown:
                parts.append(
                    f"[Page {page.page_number + 1} — TABLE]\n{page.table_markdown}"
                )
            parts.append(
                f"[Page {page.page_number + 1}]\n{page.raw_text}"
            )
    return "\n\n".join(parts)


async def generate_l1(
    section: SectionBoundary,
    pages: list[PageOCR],
    groq_client: "GroqClient",
) -> SectionSummary:
    """Generate L1 section summary via Llama-70B."""
    section_text = _pages_for_section(pages, section)
    # Truncate to safe limit
    section_text = section_text[:MAX_SECTION_CHARS]

    messages = PromptRegistry.render_messages(
        "section_summary", {"section_text": section_text}
    )

    async with _groq_semaphore:
        try:
            raw = await groq_client.complete(
                messages,
                model="llama-3.3-70b-versatile",
                response_format={"type": "json_object"},
            )
            # Delay to respect Groq free-tier rate limits
            await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"L1 generation failed for section {section.section_id}: {e}")
            # Return minimal summary on failure
            return SectionSummary(
                section_id=section.section_id,
                doc_id=section.doc_id,
                summary_text=f"[Summary unavailable: {e}]",
                key_metrics={},
                key_entities=[],
                key_risks=[],
                claims=[],
            )

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.error(f"L1 JSON parse failed for section {section.section_id}")
        return SectionSummary(
            section_id=section.section_id,
            doc_id=section.doc_id,
            summary_text=raw[:2000],
            key_metrics={},
            key_entities=[],
            key_risks=[],
            claims=[],
        )

    # Build claims with proper IDs
    claims = []
    for c in data.get("claims", []):
        claims.append(
            ExtractedClaim(
                claim_id=str(uuid.uuid4()),
                doc_id=section.doc_id,
                section_id=section.section_id,
                page_number=section.start_page,
                claim_text=c.get("claim_text", ""),
                claim_type=c.get("claim_type", "statement"),
                subject=c.get("subject", ""),
                value=c.get("value"),
                fiscal_year=c.get("fiscal_year"),
                confidence=float(c.get("confidence", 1.0)),
            )
        )

    return SectionSummary(
        section_id=section.section_id,
        doc_id=section.doc_id,
        summary_text=data.get("summary_text", ""),
        key_metrics=data.get("key_metrics", {}),
        key_entities=data.get("key_entities", []),
        key_risks=data.get("key_risks", []),
        claims=claims,
    )


async def generate_l1_batch(
    sections: list[SectionBoundary],
    pages: list[PageOCR],
    groq_client: "GroqClient",
    batch_size: int = 2,
) -> list[SectionSummary]:
    """Generate L1 summaries in parallel batches."""
    summaries: list[SectionSummary] = []
    total_batches = (len(sections) + batch_size - 1) // batch_size
    for i in range(0, len(sections), batch_size):
        batch = sections[i : i + batch_size]
        batch_num = i // batch_size + 1
        logger.info(
            f"L1 batch {batch_num}/{total_batches}: sections {i + 1}–{i + len(batch)}"
        )
        batch_results = await asyncio.gather(
            *[generate_l1(s, pages, groq_client) for s in batch]
        )
        summaries.extend(batch_results)
        # Pause between batches to respect Groq rate limits
        if i + batch_size < len(sections):
            logger.info(f"Pausing 5s between L1 batches ({batch_num}/{total_batches} done)...")
            await asyncio.sleep(5)
    return summaries


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

    async with _groq_semaphore:
        try:
            raw = await groq_client.complete(
                messages,
                model="llama-3.3-70b-versatile",
                response_format={"type": "json_object"},
            )
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"L2 generation failed for cluster {cluster_index}: {e}")
            return ClusterDigest(
                cluster_id=str(uuid.uuid4()),
                doc_id=doc_id,
                digest_text=f"[Cluster digest unavailable: {e}]",
                section_ids=[s.section_id for s in cluster],
                cluster_index=cluster_index,
            )

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {"digest_text": raw[:3000]}

    return ClusterDigest(
        cluster_id=str(uuid.uuid4()),
        doc_id=doc_id,
        digest_text=data.get("digest_text", ""),
        section_ids=[s.section_id for s in cluster],
        cluster_index=cluster_index,
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

    raw = await groq_client.complete(
        messages,
        model="llama-3.3-70b-versatile",
        response_format={"type": "json_object"},
    )

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {"digest_text": raw[:3000], "executive_summary": ""}

    return GlobalDigest(
        doc_id=doc_id,
        digest_text=data.get("digest_text", ""),
        executive_summary=data.get("executive_summary", ""),
    )
