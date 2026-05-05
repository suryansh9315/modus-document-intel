"""
Main ingestion flow: PDF → OCR → Segment → L1 → L2 → L3 → MongoDB + DuckDB.

Runs as plain async functions (no Prefect server required).
L1 runs up to 4 sections concurrently via Cerebras API (no TPM throttle).
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime

from modus_schemas import (
    DocumentRecord,
    DocumentStatus,
    PageOCR,
    SectionBoundary,
)
from modus_workers.tasks import ocr as ocr_tasks
from modus_workers.tasks import segment as segment_tasks
from modus_workers.tasks import summarize as summarize_tasks
from modus_workers.tasks import duckdb_write
from modus_workers.groq_client import GroqClient

logger = logging.getLogger(__name__)


async def _get_mongo():
    """Get async MongoDB client."""
    import motor.motor_asyncio
    mongo_uri = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
    mongo_db_name = os.environ.get("MONGO_DB_NAME", "modus_db")
    client = motor.motor_asyncio.AsyncIOMotorClient(mongo_uri)
    return client[mongo_db_name]


async def _update_doc_status(db, doc_id: str, status: DocumentStatus, **kwargs):
    update = {
        "status": status.value,
        "updated_at": datetime.utcnow().isoformat(),
        **kwargs,
    }
    await db.documents.update_one(
        {"_id": doc_id},
        {"$set": update},
    )
    logger.info(f"Doc {doc_id}: status → {status.value}")


def run_ocr(pdf_path: str) -> list[dict]:
    """Extract text from all pages of PDF. Uses JSON cache to skip re-OCR."""
    import json
    from pathlib import Path

    pdf_path_obj = Path(pdf_path)
    cache_path = pdf_path_obj.parent / (pdf_path_obj.stem + "_ocr.json")

    if cache_path.exists():
        logger.info(f"OCR cache hit: loading pages from {cache_path}")
        with cache_path.open("r") as f:
            return json.load(f)

    logger.info(f"Starting OCR on {pdf_path}")
    pages = ocr_tasks.extract_all_pages(pdf_path)
    page_dicts = [p.model_dump() for p in pages]

    try:
        with cache_path.open("w") as f:
            json.dump(page_dicts, f)
        logger.info(f"OCR cache written: {cache_path}")
    except Exception as e:
        logger.warning(f"Failed to write OCR cache (non-fatal): {e}")

    logger.info(f"OCR complete: {len(page_dicts)} pages")
    return page_dicts


def run_segmentation(page_dicts: list[dict], doc_id: str) -> list[dict]:
    """Detect section boundaries from OCR'd pages."""
    pages = [PageOCR(**d) for d in page_dicts]
    boundaries = segment_tasks.detect_sections(pages, doc_id)
    logger.info(f"Segmentation: {len(boundaries)} sections detected")
    return [b.model_dump() for b in boundaries]


async def run_l1_summaries(
    page_dicts: list[dict],
    boundary_dicts: list[dict],
    doc_id: str,
) -> tuple[list[dict], list[dict]]:
    """Generate L1 section summaries. Returns (summary_dicts, merged_boundary_dicts)."""
    pages = [PageOCR(**d) for d in page_dicts]
    boundaries = [SectionBoundary(**d) for d in boundary_dicts]

    # Merge small sections first so we can capture the merged list for MongoDB
    merged = summarize_tasks.merge_small_sections(boundaries)

    async with GroqClient() as client:
        summaries = await summarize_tasks.generate_l1_batch(
            merged, pages, client
        )

    logger.info(f"L1 summaries: {len(summaries)} generated from {len(merged)} merged sections")
    return [s.model_dump() for s in summaries], [b.model_dump() for b in merged]


async def run_l2_digests(summary_dicts: list[dict], doc_id: str) -> list[dict]:
    """Generate L2 cluster digests."""
    from modus_schemas import SectionSummary

    summaries = [SectionSummary(**d) for d in summary_dicts]
    clusters = summarize_tasks.cluster_summaries(summaries)

    async with GroqClient() as client:
        digests = await asyncio.gather(
            *[
                summarize_tasks.generate_l2(cluster, doc_id, i, client)
                for i, cluster in enumerate(clusters)
            ]
        )

    logger.info(f"L2 digests: {len(digests)} generated")
    return [d.model_dump() for d in digests]


async def run_l3_global(
    digest_dicts: list[dict], doc_id: str, total_pages: int
) -> dict:
    """Generate L3 global digest."""
    from modus_schemas import ClusterDigest

    digests = [ClusterDigest(**d) for d in digest_dicts]

    async with GroqClient() as client:
        global_digest = await summarize_tasks.generate_l3(
            digests, doc_id, total_pages, client
        )

    return global_digest.model_dump()


def run_duckdb_write(summary_dicts: list[dict], db_path: str | None = None) -> int:
    """Write claims and entities to DuckDB."""
    from modus_schemas import SectionSummary

    db_path = db_path or os.environ.get("DUCKDB_PATH", "/data/modus.duckdb")
    duckdb_write.init_schema(db_path)

    summaries = [SectionSummary(**d) for d in summary_dicts]
    count = duckdb_write.write_section_claims(summaries, db_path)
    entity_count = duckdb_write.write_section_entities(summaries, db_path)
    logger.info(f"DuckDB: {entity_count} entities written")
    return count


async def ingest_document_flow(pdf_path: str, doc_id: str) -> DocumentRecord:
    """
    Main ingestion flow. Orchestrates OCR → Segment → L1 → L2 → L3.

    Args:
        pdf_path: Absolute path to the PDF file.
        doc_id: Pre-assigned document ID (created when upload received).

    Returns:
        Final DocumentRecord with all summaries populated.
    """
    db = await _get_mongo()
    duckdb_path = os.environ.get("DUCKDB_PATH", "/data/modus.duckdb")

    try:
        # --- Phase 1: OCR ---
        await _update_doc_status(db, doc_id, DocumentStatus.INGESTING)
        # Run in thread pool to avoid blocking the FastAPI event loop
        page_dicts = await asyncio.to_thread(run_ocr, pdf_path)
        total_pages = len(page_dicts)

        # Update page count
        await db.documents.update_one(
            {"_id": doc_id}, {"$set": {"total_pages": total_pages}}
        )

        # --- Phase 2: Segmentation ---
        await _update_doc_status(db, doc_id, DocumentStatus.SEGMENTING)
        boundary_dicts = await asyncio.to_thread(run_segmentation, page_dicts, doc_id)

        # Persist section boundaries
        await db.documents.update_one(
            {"_id": doc_id},
            {"$set": {"section_boundaries": boundary_dicts}},
        )

        # --- Phase 3: L1 Summaries ---
        await _update_doc_status(db, doc_id, DocumentStatus.ANALYZING)
        summary_dicts, merged_boundary_dicts = await run_l1_summaries(page_dicts, boundary_dicts, doc_id)

        await db.documents.update_one(
            {"_id": doc_id},
            {
                "$set": {
                    "section_summaries": summary_dicts,
                    "section_boundaries": merged_boundary_dicts,
                }
            },
        )

        # --- Phase 4: Write DuckDB ---
        claims_count = await asyncio.to_thread(run_duckdb_write, summary_dicts, duckdb_path)
        logger.info(f"DuckDB: {claims_count} claims written")

        # --- Phase 5: L2 Cluster Digests ---
        await _update_doc_status(db, doc_id, DocumentStatus.AGGREGATING)
        digest_dicts = await run_l2_digests(summary_dicts, doc_id)

        await db.documents.update_one(
            {"_id": doc_id},
            {"$set": {"cluster_digests": digest_dicts}},
        )

        # --- Phase 6: L3 Global Digest ---
        global_dict = await run_l3_global(digest_dicts, doc_id, total_pages)

        # --- Final: Mark READY ---
        await db.documents.update_one(
            {"_id": doc_id},
            {
                "$set": {
                    "global_digest": global_dict,
                    "status": DocumentStatus.READY.value,
                    "updated_at": datetime.utcnow().isoformat(),
                }
            },
        )

        # Fetch final record
        doc_data = await db.documents.find_one({"_id": doc_id})
        doc_data["doc_id"] = doc_data.pop("_id")
        return DocumentRecord(**doc_data)

    except Exception as e:
        logger.error(f"Ingestion failed for doc {doc_id}: {e}", exc_info=True)
        await _update_doc_status(
            db, doc_id, DocumentStatus.ERROR, error_message=str(e)
        )
        raise
