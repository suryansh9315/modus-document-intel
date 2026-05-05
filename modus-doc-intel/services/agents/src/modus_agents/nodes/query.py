"""
Query synthesis node — always the last node.

Produces a final, citation-grounded answer by synthesizing upstream analysis.
Streams the final answer token by token.
"""
from __future__ import annotations

import logging

from modus_prompts import PromptRegistry
from modus_schemas import AgentState, QueryType
from modus_agents.llm import get_groq_client, PRIMARY_MODEL

logger = logging.getLogger(__name__)


async def query_node(state: AgentState) -> AgentState:
    """
    Synthesize the final answer from upstream analysis results.

    Pulls together:
    - The analysis result from the branch node (local/global/extraction/contradiction)
    - Context used summary
    - Query details

    Produces the final state["answer"] used by the streaming API.
    """
    client = get_groq_client()
    query = state["query"]

    analysis_result = state["_analysis_result"]
    context_used = state["context_used"]

    # Extraction and contradiction nodes produce complete formatted answers —
    # skip the synthesis LLM call to avoid a redundant PRIMARY_MODEL request.
    _PASSTHROUGH_TYPES = {
        QueryType.EXTRACT_ENTITIES,
        QueryType.EXTRACT_RISKS,
        QueryType.EXTRACT_DECISIONS,
        QueryType.DETECT_CONTRADICTIONS,
    }
    if query.query_type in _PASSTHROUGH_TYPES:
        state["answer"] = analysis_result or "No results available."
        state["sources"] = []
        return state

    context_summary = (
        f"Context loaded: {', '.join(context_used[:10])}"
        if context_used
        else "Context: document summaries"
    )

    messages = PromptRegistry.render_messages(
        "query_synthesize",
        {
            "question": query.question,
            "query_type": query.query_type.value,
            "analysis_results": analysis_result or "No analysis result available.",
            "context_summary": context_summary,
        },
    )

    # Collect streamed answer
    try:
        answer_parts = []
        async for token in client.stream(messages, model=PRIMARY_MODEL):
            answer_parts.append(token)
        state["answer"] = "".join(answer_parts)
    except Exception as e:
        logger.error(f"query_node stream failed: {e}")
        state["answer"] = f"Answer unavailable due to API error: {e}"

    state["sources"] = []
    return state
