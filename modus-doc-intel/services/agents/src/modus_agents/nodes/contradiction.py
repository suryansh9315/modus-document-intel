"""
Contradiction detection node.

Queries DuckDB for same-subject, different-value claims.
Uses Llama-70B to determine genuine vs. explainable contradictions.
"""
from __future__ import annotations

import json
import logging
import uuid

from modus_prompts import PromptRegistry
from modus_schemas import AgentState, ContradictionReport
from modus_agents.llm import get_groq_client, FAST_MODEL, PRIMARY_MODEL

logger = logging.getLogger(__name__)


def _get_contradiction_candidates(doc_id: str) -> list[dict]:
    """Query DuckDB for potential contradictions (thread-safe read-only)."""
    try:
        from modus_workers.tasks.duckdb_write import query_contradictions
        return query_contradictions(doc_id)
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

    # Fix 7: Sort candidates by topic relevance before slicing to top 20.
    # The DuckDB query returns results ordered alphabetically by subject, which
    # means alphabetically-late subjects may be excluded by the [:20] cap.
    # Re-sorting by question keyword overlap ensures the most relevant subjects
    # surface first.
    _STOP = {"the", "a", "an", "are", "is", "do", "in", "of", "any", "all",
             "and", "or", "to", "what", "how", "does", "there", "across"}
    question_words = {
        w.lower().strip("?.,") for w in query.question.split()
        if len(w) > 3 and w.lower() not in _STOP
    }

    def _relevance_score(c: dict) -> int:
        subject_words = set(c.get("subject", "").lower().replace("_", " ").split())
        return len(question_words & subject_words)

    candidates.sort(key=_relevance_score, reverse=True)

    # Format candidates for LLM
    candidate_text_parts = []
    for i, c in enumerate(candidates[:20]):  # limit to top 20
        candidate_text_parts.append(
            f"{i + 1}. Subject: {c['subject']}\n"
            f"   Claim A (p.{c['page_a']}): {c['claim_a_text'][:200]}\n"
            f"   Claim B (p.{c['page_b']}): {c['claim_b_text'][:200]}"
        )

    section_context = state.get("_section_context", "")[:3000]

    messages = PromptRegistry.render_messages(
        "query_detect_contradictions",
        {
            "question": query.question,
            "contradiction_candidates": "\n\n".join(candidate_text_parts),
            "context": section_context,
        },
    )

    try:
        raw = await client.complete(
            messages,
            model=FAST_MODEL,
            response_format={"type": "json_object"},
        )
    except Exception as e:
        logger.error(f"contradiction_node LLM call failed: {e}")
        state["_analysis_result"] = f"## Contradiction Analysis\n\nAnalysis unavailable due to API error: {e}"
        state["contradictions"] = []
        return state

    contradictions: list[ContradictionReport] = []
    analysis_text = ""

    try:
        data = json.loads(raw)
        analysis_text = data.get("summary") or ""

        for item in (data.get("contradictions") or []):
            if not isinstance(item, dict):
                continue
            if not item.get("is_genuine_contradiction"):
                continue
            raw_severity = item.get("severity", "medium")
            severity = raw_severity if raw_severity in {"low", "medium", "high"} else "medium"

            def _safe_page(val) -> int:
                try:
                    return max(0, int(val))
                except (TypeError, ValueError):
                    return 0

            report = ContradictionReport(
                contradiction_id=str(uuid.uuid4()),
                subject=item.get("subject") or "Unknown",
                claim_a_text=item.get("claim_a") or "",
                claim_a_section=item.get("claim_a_section") or "",
                claim_a_page=_safe_page(item.get("claim_a_page")),
                claim_b_text=item.get("claim_b") or "",
                claim_b_section=item.get("claim_b_section") or "",
                claim_b_page=_safe_page(item.get("claim_b_page")),
                explanation=item.get("explanation") or "",
                severity=severity,
            )
            contradictions.append(report)

    except (json.JSONDecodeError, KeyError) as e:
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
        default_assessment = (
            "All differences are attributable to different time periods, "
            "methodologies, or rounding."
        )
        analysis = (
            f"## Contradiction Analysis\n\n"
            f"Reviewed {len(candidates)} potential contradictions. "
            f"No genuine contradictions found.\n\n"
            f"**Assessment:** {analysis_text or default_assessment}"
        )

    state["_analysis_result"] = analysis
    state["contradictions"] = contradictions
    return state
