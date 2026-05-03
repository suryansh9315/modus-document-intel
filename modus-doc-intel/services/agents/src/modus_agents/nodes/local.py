"""
Local analysis node — per-section deep dive.

Used for: SUMMARIZE_SECTION, CROSS_SECTION_COMPARE
"""
from __future__ import annotations

import logging

from modus_prompts import PromptRegistry
from modus_schemas import AgentState, QueryType
from modus_agents.llm import get_groq_client, PRIMARY_MODEL

logger = logging.getLogger(__name__)


async def local_analysis_node(state: AgentState) -> AgentState:
    """
    Generate a detailed analysis for one or two specific sections.

    For CROSS_SECTION_COMPARE: renders the cross_compare template.
    For SUMMARIZE_SECTION: renders the summarize_section template.
    """
    client = get_groq_client()
    query = state["query"]
    doc = state["doc"]

    section_summaries = {s.section_id: s for s in doc.section_summaries}
    section_boundaries = {s.section_id: s for s in doc.section_boundaries}

    if query.query_type == QueryType.CROSS_SECTION_COMPARE and query.section_ids and len(query.section_ids) >= 2:
        sid_a, sid_b = query.section_ids[0], query.section_ids[1]
        s_a = section_summaries.get(sid_a)
        s_b = section_summaries.get(sid_b)
        b_a = section_boundaries.get(sid_a)
        b_b = section_boundaries.get(sid_b)

        if not (s_a and s_b):
            state["answer"] = "One or both sections not found."
            return state

        section_a_context = (
            f"{s_a.summary_text}\n\nKey Metrics: {s_a.key_metrics}"
        )
        section_b_context = (
            f"{s_b.summary_text}\n\nKey Metrics: {s_b.key_metrics}"
        )

        messages = PromptRegistry.render_messages(
            "query_cross_compare",
            {
                "question": query.question,
                "section_a_title": b_a.title if b_a else sid_a,
                "section_a_context": section_a_context,
                "section_b_title": b_b.title if b_b else sid_b,
                "section_b_context": section_b_context,
            },
        )
    else:
        # Single section summary
        section_context = state.get("_section_context", "")  # type: ignore[attr-defined]
        if not section_context and doc.section_summaries:
            # Fallback to first section
            s = doc.section_summaries[0]
            section_context = f"{s.summary_text}\n\nKey Metrics: {s.key_metrics}"

        messages = PromptRegistry.render_messages(
            "query_summarize_section",
            {
                "question": query.question,
                "section_context": section_context,
            },
        )

    analysis = await client.complete(messages, model=PRIMARY_MODEL)
    state["_analysis_result"] = analysis  # type: ignore[typeddict-unknown-key]
    return state
