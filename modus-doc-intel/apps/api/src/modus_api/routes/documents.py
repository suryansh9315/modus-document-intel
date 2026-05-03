"""
Document management routes.

GET /documents         — list all documents
GET /documents/{id}    — get document record with all summaries
DELETE /documents/{id} — remove document and all associated data
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from modus_schemas import DocumentRecord

logger = logging.getLogger(__name__)
router = APIRouter()


def _doc_from_mongo(doc: dict) -> dict:
    """Convert MongoDB document to API-friendly format."""
    if "_id" in doc:
        doc["doc_id"] = str(doc.pop("_id"))
    return doc


@router.get("/", response_model=list[dict[str, Any]])
async def list_documents(request: Request):
    """List all ingested documents (id, filename, status, page count)."""
    db = request.app.state.db
    cursor = db.documents.find(
        {},
        projection={
            "_id": 1,
            "filename": 1,
            "status": 1,
            "total_pages": 1,
            "created_at": 1,
            "updated_at": 1,
            "error_message": 1,
        },
    )
    docs = []
    async for doc in cursor:
        doc["doc_id"] = str(doc.pop("_id"))
        docs.append(doc)
    return docs


@router.get("/{doc_id}")
async def get_document(doc_id: str, request: Request):
    """Get full document record including summaries."""
    db = request.app.state.db
    doc = await db.documents.find_one({"_id": doc_id})
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")
    return _doc_from_mongo(doc)


@router.get("/{doc_id}/sections")
async def get_document_sections(doc_id: str, request: Request):
    """Get section boundaries for a document (lightweight)."""
    db = request.app.state.db
    doc = await db.documents.find_one(
        {"_id": doc_id},
        projection={"_id": 1, "section_boundaries": 1, "status": 1},
    )
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")
    return {
        "doc_id": doc_id,
        "status": doc.get("status"),
        "sections": doc.get("section_boundaries", []),
    }


@router.delete("/{doc_id}")
async def delete_document(doc_id: str, request: Request):
    """Delete a document and all associated data."""
    db = request.app.state.db
    result = await db.documents.delete_one({"_id": doc_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")
    return {"message": f"Document {doc_id} deleted successfully"}
