"""
Ingestion routes.

POST /ingestion/upload        — upload PDF, create DocumentRecord, trigger ingestion pipeline
GET  /ingestion/{job_id}      — poll ingestion status
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Request, UploadFile

from modus_api.config import settings
from modus_schemas import DocumentRecord, DocumentStatus, IngestionJob

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/upload")
async def upload_document(request: Request, background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    """
    Upload a PDF and trigger background ingestion.

    1. Save PDF to upload_dir.
    2. Create DocumentRecord in MongoDB (status=PENDING).
    3. Trigger ingestion pipeline in background (non-blocking).
    4. Return doc_id and job_id for status polling.
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    db = request.app.state.db
    doc_id = str(uuid.uuid4())

    # Save uploaded file (async read + threaded write to avoid blocking the event loop)
    upload_path = Path(settings.upload_dir) / f"{doc_id}.pdf"
    contents = await file.read()
    await asyncio.to_thread(upload_path.write_bytes, contents)

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

    # Trigger ingestion pipeline (background — runs after response is sent)
    background_tasks.add_task(_run_ingestion_background, str(upload_path), doc_id)
    logger.info(f"Ingestion queued for doc {doc_id}")

    return {
        "doc_id": doc_id,
        "filename": file.filename,
        "status": DocumentStatus.PENDING.value,
        "message": "Ingestion started. Poll /ingestion/{doc_id} for status.",
    }


async def _run_ingestion_background(pdf_path: str, doc_id: str):
    """Run the ingestion pipeline in background."""
    try:
        from modus_workers.flows.ingest_document import ingest_document_flow
        await ingest_document_flow(pdf_path=pdf_path, doc_id=doc_id)
    except Exception as e:
        logger.error(f"Background ingestion failed for {doc_id}: {e}", exc_info=True)
        # ingest_document_flow updates status to ERROR internally; this is a safety net
        # for import errors or other failures before the flow starts.
        try:
            import motor.motor_asyncio, os
            from modus_schemas import DocumentStatus as DS
            client = motor.motor_asyncio.AsyncIOMotorClient(
                os.environ.get("MONGO_URI", "mongodb://localhost:27017")
            )
            db = client[os.environ.get("MONGO_DB_NAME", "modus_db")]
            await db.documents.update_one(
                {"_id": doc_id},
                {"$set": {"status": DS.ERROR.value, "error_message": str(e)}},
            )
            client.close()
        except Exception:
            pass


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
