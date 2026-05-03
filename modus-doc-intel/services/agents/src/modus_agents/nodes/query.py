"""
Query synthesis node — always the last node.

Produces a final, citation-grounded answer by synthesizing upstream analysis.
Streams the final answer token by token.
"""
from __future__ import annotations

import logging

from modus_prompts import PromptRegistry
from modus_schemas import AgentState
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

    analysis_result = state.get("_analysis_result", "")  # type: ignore[attr-defined]
    context_used = state.get("context_used", [])

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
    answer_parts = []
    async for token in client.stream(messages, model=PRIMARY_MODEL):
        answer_parts.append(token)

    state["answer"] = "".join(answer_parts)

    # Build sources list from context_used
    sources = []
    for ctx in context_used:
        if ctx.startswith("L1:"):
            sid = ctx[3:]
            sources.append({"type": "section", "id": sid, "level": "L1"})
        elif ctx.startswith("L2:"):
            sources.append({"type": "cluster", "id": ctx[3:], "level": "L2"})
        elif ctx.startswith("L3:"):
            sources.append({"type": "global", "id": "global", "level": "L3"})

    state["sources"] = sources
    return state
