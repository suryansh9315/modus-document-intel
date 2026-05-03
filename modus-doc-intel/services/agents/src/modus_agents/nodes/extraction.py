"""
Extraction node — structured entity/risk/decision extraction using Llama-8B (JSON mode).

Used for: EXTRACT_ENTITIES, EXTRACT_RISKS, EXTRACT_DECISIONS
"""
from __future__ import annotations

import json
import logging

from modus_prompts import PromptRegistry
from modus_schemas import AgentState, QueryType
from modus_agents.llm import get_groq_client, FAST_MODEL

logger = logging.getLogger(__name__)

EXTRACTION_TYPE_MAP = {
    QueryType.EXTRACT_ENTITIES: "entities",
    QueryType.EXTRACT_RISKS: "risks",
    QueryType.EXTRACT_DECISIONS: "decisions",
}


async def extraction_node(state: AgentState) -> AgentState:
    """
    Extract structured data (entities, risks, or decisions) from document context.
    Uses Llama-8B with JSON mode for speed and structured output.
    """
    client = get_groq_client()
    query = state["query"]

    extraction_type = EXTRACTION_TYPE_MAP.get(query.query_type, "entities")

    # Compose context from available levels
    section_context = state.get("_section_context", "")     # type: ignore[attr-defined]
    cluster_context = state.get("_cluster_context", "")     # type: ignore[attr-defined]
    global_context = state.get("_global_context", "")       # type: ignore[attr-defined]

    context = "\n\n".join(
        filter(None, [global_context[:1000], cluster_context[:3000], section_context[:8000]])
    )

    messages = PromptRegistry.render_messages(
        "query_extract",
        {
            "extraction_type": extraction_type,
            "question": query.question,
            "context": context,
        },
    )

    raw = await client.complete(
        messages,
        model=FAST_MODEL,
        response_format={"type": "json_object"},
    )

    try:
        data = json.loads(raw)
        items = data.get("items", [])
        summary = data.get("summary", "")
    except json.JSONDecodeError:
        items = []
        summary = raw[:500]

    # Format as readable answer
    lines = [f"## Extracted {extraction_type.capitalize()}\n"]
    for item in items:
        name = item.get("name", "Unknown")
        value = item.get("value", "")
        desc = item.get("description", "")
        page = item.get("page")
        fy = item.get("fiscal_year", "")

        line = f"- **{name}**"
        if value:
            line += f": {value}"
        if fy:
            line += f" ({fy})"
        if desc:
            line += f" — {desc}"
        if page:
            line += f" [p.{page}]"
        lines.append(line)

    if summary:
        lines.append(f"\n**Summary:** {summary}")

    state["_analysis_result"] = "\n".join(lines)  # type: ignore[typeddict-unknown-key]
    state["_extracted_items"] = items               # type: ignore[typeddict-unknown-key]
    return state
