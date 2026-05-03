"""
Contradiction detection node.

Queries DuckDB for same-subject, different-value claims.
Uses Llama-70B to determine genuine vs. explainable contradictions.
"""
from __future__ import annotations

import json
import logging
import os
import uuid

from modus_prompts import PromptRegistry
from modus_schemas import AgentState, ContradictionReport
from modus_agents.llm import get_groq_client, PRIMARY_MODEL

logger = logging.getLogger(__name__)


def _get_contradiction_candidates(doc_id: str) -> list[dict]:
    """Query DuckDB for potential contradictions (thread-safe read-only)."""
    try:
        import duckdb
        db_path = os.environ.get("DUCKDB_PATH", "/data/modus.duckdb")
        con = duckdb.connect(db_path, read_only=True)
        try:
            results = con.execute(
                """
                SELECT
                    a.claim_text AS claim_a_text,
                    b.claim_text AS claim_b_text,
                    a.section_id AS section_a_id,
                    b.section_id AS section_b_id,
                    a.page_number AS page_a,
                    b.page_number AS page_b,
                    a.subject,
                    a.value AS value_a,
                    b.value AS value_b
                FROM claims a
                JOIN claims b
                  ON a.doc_id = b.doc_id
                 AND a.subject = b.subject
                 AND a.claim_id < b.claim_id
                 AND a.value IS NOT NULL
                 AND b.value IS NOT NULL
                 AND a.value != b.value
                WHERE a.doc_id = ?
                LIMIT 50
                """,
                [doc_id],
            ).fetchall()
            columns = [
                "claim_a_text", "claim_b_text", "section_a_id", "section_b_id",
                "page_a", "page_b", "subject", "value_a", "value_b",
            ]
            return [dict(zip(columns, row)) for row in results]
        finally:
            con.close()
    except Exception as e:
        logger.error(f"DuckDB contradiction query failed: {e}")
        return []


async def contradiction_node(state: AgentState) -> AgentState:
    """
    Find and classify contradictions in the document.

    1. Query DuckDB for same-subject, different-value claims.
    2. Ask Llama-70B to classify: genuine contradiction vs. explainable.
    3. Populate state["contradictions"] with ContradictionReport objects.
    """
    client = get_groq_client()
    doc = state["doc"]
    query = state["query"]

    # 1. Get candidates from DuckDB
    candidates = _get_contradiction_candidates(doc.doc_id)

    if not candidates:
        state["_analysis_result"] = (
            "No potential contradictions found in the structured claims database."
        )
        state["contradictions"] = []
        return state

    # Format candidates for LLM
    candidate_text_parts = []
    for i, c in enumerate(candidates[:20]):  # limit to top 20
        candidate_text_parts.append(
            f"{i + 1}. Subject: {c['subject']}\n"
            f"   Claim A (p.{c['page_a']}): {c['claim_a_text'][:200]}\n"
            f"   Claim B (p.{c['page_b']}): {c['claim_b_text'][:200]}"
        )

    section_context = state.get("_section_context", "")[:3000]  # type: ignore[attr-defined]

    messages = PromptRegistry.render_messages(
        "query_detect_contradictions",
        {
            "question": query.question,
            "contradiction_candidates": "\n\n".join(candidate_text_parts),
            "context": section_context,
        },
    )

    raw = await client.complete(
        messages,
        model=PRIMARY_MODEL,
        response_format={"type": "json_object"},
    )

    contradictions: list[ContradictionReport] = []
    analysis_text = ""

    try:
        data = json.loads(raw)
        analysis_text = data.get("summary", "")

        for item in data.get("contradictions", []):
            if not item.get("is_genuine_contradiction"):
                continue
            report = ContradictionReport(
                contradiction_id=str(uuid.uuid4()),
                subject=item.get("subject", "Unknown"),
                claim_a_text=item.get("claim_a", ""),
                claim_a_section=item.get("claim_a_section", ""),
                claim_a_page=int(item.get("claim_a_page", 0)),
                claim_b_text=item.get("claim_b", ""),
                claim_b_section=item.get("claim_b_section", ""),
                claim_b_page=int(item.get("claim_b_page", 0)),
                explanation=item.get("explanation", ""),
                severity=item.get("severity", "medium"),
            )
            contradictions.append(report)

    except (json.JSONDecodeError, ValueError, KeyError) as e:
        logger.error(f"Contradiction classification failed: {e}")
        analysis_text = raw[:1000]

    # Build readable answer
    if contradictions:
        lines = [
            f"## Contradiction Analysis\n",
            f"Found **{len(contradictions)} genuine contradiction(s)** "
            f"from {len(candidates)} candidates reviewed.\n",
        ]
        for i, c in enumerate(contradictions):
            lines.append(
                f"### {i + 1}. {c.subject} [{c.severity.upper()} severity]\n"
                f"- **Claim A** [p.{c.claim_a_page}]: {c.claim_a_text}\n"
                f"- **Claim B** [p.{c.claim_b_page}]: {c.claim_b_text}\n"
                f"- **Explanation**: {c.explanation}\n"
            )
        if analysis_text:
            lines.append(f"\n**Overall Assessment:** {analysis_text}")
        analysis = "\n".join(lines)
    else:
        analysis = (
            f"## Contradiction Analysis\n\n"
            f"Reviewed {len(candidates)} potential contradictions. "
            f"No genuine contradictions found — all differences are "
            f"attributable to different time periods, methodologies, or rounding.\n\n"
            f"**Assessment:** {analysis_text}"
        )

    state["_analysis_result"] = analysis  # type: ignore[typeddict-unknown-key]
    state["contradictions"] = contradictions
    return state
