"""
Global reasoning node — full-document synthesis.

Used for: SUMMARIZE_FULL
"""
from __future__ import annotations

import logging

from modus_prompts import PromptRegistry
from modus_schemas import AgentState
from modus_agents.llm import get_groq_client, PRIMARY_MODEL

logger = logging.getLogger(__name__)


async def global_reasoning_node(state: AgentState) -> AgentState:
    """
    Synthesize an answer using the full hierarchical context (L3 + L2 + L1).
    """
    client = get_groq_client()
    query = state["query"]

    global_context = state.get("_global_context", "")    # type: ignore[attr-defined]
    cluster_context = state.get("_cluster_context", "")  # type: ignore[attr-defined]
    section_context = state.get("_section_context", "")  # type: ignore[attr-defined]

    messages = PromptRegistry.render_messages(
        "query_summarize_full",
        {
            "question": query.question,
            "global_context": global_context or "[Global digest not yet available]",
            "cluster_context": cluster_context or "[Cluster digests not yet available]",
            "section_context": section_context or "[Section summaries not yet available]",
        },
    )

    analysis = await client.complete(messages, model=PRIMARY_MODEL)
    state["_analysis_result"] = analysis  # type: ignore[typeddict-unknown-key]
    return state
