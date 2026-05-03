"""
Query routes — SSE streaming endpoint.

POST /queries/stream — stream query answer via Vercel AI SDK data-stream protocol.
POST /queries        — non-streaming query (for testing).
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from modus_schemas import DocumentRecord, QueryRequest, QueryResponse, AgentState

logger = logging.getLogger(__name__)
router = APIRouter()


def _doc_from_mongo(doc: dict) -> DocumentRecord:
    """Convert MongoDB dict to DocumentRecord."""
    if "_id" in doc:
        doc["doc_id"] = str(doc.pop("_id"))
    return DocumentRecord(**doc)


@router.post("/stream")
async def stream_query(request: Request, body: QueryRequest):
    """
    Stream a query answer using Vercel AI SDK data-stream protocol.

    Emits:
    - `0:"token"\\n` for each text token
    - `d:{}\\n` for stream done signal
    """
    db = request.app.state.db
    doc_raw = await db.documents.find_one({"_id": body.doc_id})
    if not doc_raw:
        raise HTTPException(status_code=404, detail=f"Document {body.doc_id} not found")

    doc = _doc_from_mongo(doc_raw)

    if doc.status.value not in ("READY", "AGGREGATING"):
        raise HTTPException(
            status_code=422,
            detail=f"Document not ready for queries (status: {doc.status.value})",
        )

    from modus_agents.graph import query_graph

    initial_state: AgentState = {
        "query": body,
        "doc": doc,
        "context_used": [],
        "token_budget_used": 0,
        "token_budget_limit": 120_000,
        "answer": "",
        "sources": [],
        "contradictions": [],
        "route": "",
    }

    async def event_generator():
        try:
            # Run graph — collect intermediate states
            answer_streamed = False
            async for event in query_graph.astream(initial_state, stream_mode="values"):
                answer = event.get("answer", "")
                if answer and not answer_streamed:
                    # Stream the answer token by token (simulate streaming)
                    # In production, modify query_node to yield tokens
                    for i in range(0, len(answer), 20):
                        chunk = answer[i : i + 20]
                        yield f'0:{json.dumps(chunk)}\n'
                    answer_streamed = True

                    # Emit sources
                    sources = event.get("sources", [])
                    if sources:
                        yield f'8:{json.dumps(sources)}\n'

                    # Emit contradictions if any
                    contradictions = event.get("contradictions", [])
                    if contradictions:
                        contra_dicts = [
                            c.model_dump() if hasattr(c, "model_dump") else c
                            for c in contradictions
                        ]
                        yield f'8:{json.dumps({"contradictions": contra_dicts})}\n'

            # Done signal
            yield 'd:{}\n'

        except Exception as e:
            logger.error(f"Stream query failed: {e}", exc_info=True)
            yield f'0:{json.dumps(f"Error: {str(e)}")}\n'
            yield 'd:{}\n'

    return StreamingResponse(
        event_generator(),
        media_type="text/plain",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/", response_model=QueryResponse)
async def run_query(request: Request, body: QueryRequest):
    """
    Non-streaming query endpoint (for testing / non-browser clients).
    """
    db = request.app.state.db
    doc_raw = await db.documents.find_one({"_id": body.doc_id})
    if not doc_raw:
        raise HTTPException(status_code=404, detail=f"Document {body.doc_id} not found")

    doc = _doc_from_mongo(doc_raw)

    from modus_agents.graph import query_graph

    initial_state: AgentState = {
        "query": body,
        "doc": doc,
        "context_used": [],
        "token_budget_used": 0,
        "token_budget_limit": 120_000,
        "answer": "",
        "sources": [],
        "contradictions": [],
        "route": "",
    }

    final_state = await query_graph.ainvoke(initial_state)

    return QueryResponse(
        answer=final_state["answer"],
        sources=final_state["sources"],
        contradictions=final_state["contradictions"],
        context_used=final_state["context_used"],
        token_budget_used=final_state["token_budget_used"],
    )
