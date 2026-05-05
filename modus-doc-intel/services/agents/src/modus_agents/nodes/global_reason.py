"""
Global reasoning node — full-document synthesis.

Used for: SUMMARIZE_FULL
"""
from __future__ import annotations

import logging

from modus_prompts import PromptRegistry
from modus_schemas import AgentState
from modus_agents.llm import get_groq_primary_client, PRIMARY_MODEL

logger = logging.getLogger(__name__)


async def global_reasoning_node(state: AgentState) -> AgentState:
    """
    Synthesize an answer using the full hierarchical context (L3 + L2 + L1).
    """
    client = get_groq_primary_client()
    query = state["query"]

    global_context = state.get("_global_context", "")
    cluster_context = state.get("_cluster_context", "")

    messages = PromptRegistry.render_messages(
        "query_summarize_full",
        {
            "question": query.question,
            "global_context": global_context or "[Global digest not yet available]",
            "cluster_context": cluster_context or "[Cluster digests not yet available]",
        },
    )

    try:
        analysis = await client.complete(messages, model=PRIMARY_MODEL)
    except Exception as e:
        logger.error(f"global_reasoning_node LLM call failed: {e}")
        analysis = f"Summary unavailable due to API error: {e}"

    state["_analysis_result"] = analysis
    return state
