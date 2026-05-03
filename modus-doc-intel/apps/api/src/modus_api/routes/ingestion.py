"""
Ingestion routes.

POST /ingestion/upload        — upload PDF, create DocumentRecord, trigger Prefect flow
GET  /ingestion/{job_id}      — poll ingestion status
"""
from __future__ import annotations

import logging
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from modus_api.config import settings
from modus_schemas import DocumentRecord, DocumentStatus, IngestionJob

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/upload")
async def upload_document(request: Request, file: UploadFile = File(...)):
    """
    Upload a PDF and trigger background ingestion.

    1. Save PDF to upload_dir.
    2. Create DocumentRecord in MongoDB (status=PENDING).
    3. Trigger Prefect flow in background (non-blocking).
    4. Return doc_id and job_id for status polling.
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    db = request.app.state.db
    doc_id = str(uuid.uuid4())

    # Save uploaded file
    upload_path = Path(settings.upload_dir) / f"{doc_id}.pdf"
    with open(upload_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    logger.info(f"Saved PDF: {upload_path} ({upload_path.stat().st_size} bytes)")

    # Create DocumentRecord in MongoDB
    now = datetime.utcnow().isoformat()
    doc_record = {
        "_id": doc_id,
        "doc_id": doc_id,
        "filename": file.filename,
        "total_pages": 0,
        "status": DocumentStatus.PENDING.value,
        "section_boundaries": [],
        "section_summaries": [],
        "cluster_digests": [],
        "global_digest": None,
        "created_at": now,
        "updated_at": now,
    }
    await db.documents.insert_one(doc_record)

    # Trigger Prefect ingestion flow (background — non-blocking)
    try:
        import asyncio
        asyncio.create_task(_run_ingestion_background(str(upload_path), doc_id))
        logger.info(f"Ingestion started for doc {doc_id}")
    except Exception as e:
        logger.error(f"Failed to start ingestion: {e}")
        await db.documents.update_one(
            {"_id": doc_id},
            {"$set": {"status": DocumentStatus.ERROR.value, "error_message": str(e)}},
        )

    return {
        "doc_id": doc_id,
        "filename": file.filename,
        "status": DocumentStatus.PENDING.value,
        "message": "Ingestion started. Poll /ingestion/{doc_id} for status.",
    }


async def _run_ingestion_background(pdf_path: str, doc_id: str):
    """Run the Prefect ingestion flow in background."""
    try:
        from modus_workers.flows.ingest_document import ingest_document_flow
        await ingest_document_flow(pdf_path=pdf_path, doc_id=doc_id)
    except Exception as e:
        logger.error(f"Background ingestion failed for {doc_id}: {e}", exc_info=True)


@router.get("/{doc_id}")
async def get_ingestion_status(doc_id: str, request: Request):
    """Poll ingestion status for a document."""
    db = request.app.state.db
    doc = await db.documents.find_one(
        {"_id": doc_id},
        projection={
            "_id": 1,
            "filename": 1,
            "status": 1,
            "total_pages": 1,
            "error_message": 1,
            "updated_at": 1,
        },
    )
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")

    # Compute rough progress percentage based on status
    status_progress = {
        DocumentStatus.PENDING.value: 0,
        DocumentStatus.INGESTING.value: 15,
        DocumentStatus.SEGMENTING.value: 30,
        DocumentStatus.ANALYZING.value: 60,
        DocumentStatus.AGGREGATING.value: 85,
        DocumentStatus.READY.value: 100,
        DocumentStatus.ERROR.value: 0,
    }

    status = doc.get("status", DocumentStatus.PENDING.value)
    return IngestionJob(
        job_id=doc_id,
        doc_id=doc_id,
        status=DocumentStatus(status),
        progress_pct=float(status_progress.get(status, 0)),
        message=f"Status: {status}",
        error=doc.get("error_message"),
    )
