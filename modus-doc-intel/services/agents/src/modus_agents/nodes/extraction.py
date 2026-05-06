"""
Extraction node — structured entity/risk/decision extraction using Llama-8B (JSON mode).

Used for: EXTRACT_ENTITIES, EXTRACT_RISKS, EXTRACT_DECISIONS
"""
from __future__ import annotations

import json
import logging
import re

from modus_prompts import PromptRegistry
from modus_schemas import AgentState, QueryType
from modus_agents.llm import get_groq_primary_client, PRIMARY_MODEL

logger = logging.getLogger(__name__)


def _parse_json_response(raw: str) -> dict:
    """
    Parse a JSON response from the LLM, handling common wrapping patterns.
    Some models return JSON wrapped in markdown code fences despite json_object mode.
    """
    # Try direct parse first
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Strip markdown code fences: ```json ... ``` or ``` ... ```
    cleaned = raw.strip()
    fence_match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", cleaned)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass

    # Find the first {...} block in the string
    brace_match = re.search(r"\{[\s\S]+\}", cleaned)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError:
            pass

    raise json.JSONDecodeError("No valid JSON found", raw, 0)


EXTRACTION_TYPE_MAP = {
    QueryType.EXTRACT_ENTITIES: "entities",
    QueryType.EXTRACT_RISKS: "risks",
    QueryType.EXTRACT_DECISIONS: "decisions",
}

# Maps query type to the DuckDB claim_type used as seed data.
# EXTRACT_ENTITIES has no seed — metric seeds caused the LLM to output metrics instead of entities.
CLAIM_TYPE_MAP = {
    QueryType.EXTRACT_RISKS: "risk_factor",
    QueryType.EXTRACT_DECISIONS: "commitment",
}


async def extraction_node(state: AgentState) -> AgentState:
    """
    Extract structured data (entities, risks, or decisions) from document context.
    Uses Llama-8B with JSON mode for speed and structured output.
    """
    client = get_groq_primary_client()
    query = state["query"]
    doc = state["doc"]

    extraction_type = EXTRACTION_TYPE_MAP.get(query.query_type, "entities")

    # Compose context from available levels.
    # Fix 4: remove hard truncations — L3 and L2 are already compressed digests
    # and are well within the model's context budget for extraction tasks.
    section_context = state.get("_section_context", "")
    cluster_context = state.get("_cluster_context", "")
    global_context = state.get("_global_context", "")

    logger.info(f"extraction_node context lengths — global:{len(global_context)} cluster:{len(cluster_context)} section:{len(section_context)}")

    context = "\n\n".join(filter(None, [
        global_context,             # Full L3 (~800 tokens)
        cluster_context,            # Full L2 (~5-15K tokens)
        section_context[:32_000],   # First 32K chars of L1 (was 8K)
    ]))

    # EXTRACT_ENTITIES seeds from the entities table (typed named entities)
    if query.query_type == QueryType.EXTRACT_ENTITIES:
        try:
            from modus_workers.tasks.duckdb_write import get_entities_for_extraction
            seed_entities = get_entities_for_extraction(doc.doc_id)
            if seed_entities:
                seed_lines = "\n".join(
                    f"- {e['name']} [{e['entity_type']}]"
                    for e in seed_entities[:50]
                )
                context = f"PRE-EXTRACTED CANDIDATES:\n{seed_lines}\n\n---\n\n{context}"
        except Exception:
            pass  # graceful degradation — extraction still runs without seeds

    # Fix 3b: Seed extraction with pre-extracted DuckDB claims so the LLM
    # refines and augments rather than starting from scratch.
    seed_claim_type = CLAIM_TYPE_MAP.get(query.query_type)
    if seed_claim_type:
        try:
            from modus_workers.tasks.duckdb_write import get_claims_by_type
            seed_claims = get_claims_by_type(doc.doc_id, seed_claim_type)
            if seed_claims:
                seed_lines = "\n".join(
                    f"- {c['subject']}: {c['value'] or c['claim_text'][:120]} [p.{c['page_number']}]"
                    for c in seed_claims[:50]  # cap at 50 seed items
                )
                context = f"PRE-EXTRACTED CANDIDATES:\n{seed_lines}\n\n---\n\n{context}"
        except Exception:
            pass  # graceful degradation — extraction still runs without seeds

    logger.info(f"extraction_node final context length: {len(context)} chars, preview: {context[:200]!r}")

    messages = PromptRegistry.render_messages(
        "query_extract",
        {
            "extraction_type": extraction_type,
            "question": query.question,
            "context": context,
        },
    )
    total_msg_chars = sum(len(m["content"]) for m in messages)
    logger.info(f"extraction_node messages: {len(messages)} messages, {total_msg_chars} total chars")

    try:
        raw = await client.complete(
            messages,
            model=PRIMARY_MODEL,
            response_format={"type": "json_object"},
        )
    except Exception as e:
        logger.error(f"extraction_node LLM call failed: {e}")
        state["_analysis_result"] = f"## Extracted {extraction_type.capitalize()}\n\nExtraction unavailable due to API error: {e}"
        state["_extracted_items"] = []
        return state

    try:
        data = _parse_json_response(raw)
        if isinstance(data, list):
            items = data
            summary = ""
        else:
            items = data.get("items") or []
            summary = data.get("summary") or ""
        # Guard: LLM sometimes nests the full JSON object inside the summary field
        if summary and isinstance(summary, str) and summary.strip().startswith(("{", "[")):
            try:
                nested = json.loads(summary)
                if isinstance(nested, dict):
                    if nested.get("items"):
                        items = nested.get("items") or []
                    summary = nested.get("summary") or ""
            except json.JSONDecodeError:
                summary = ""  # unparseable blob — discard
        # Guard: summary might be a dict (LLM returned wrong type)
        if isinstance(summary, dict):
            summary = ""
    except json.JSONDecodeError:
        items = []
        summary = ""
        logger.warning("extraction_node: could not parse JSON from LLM response")

    # Fix 5a: filter null/empty/placeholder names
    items = [
        item for item in items
        if isinstance(item, dict)
        and str(item.get("name", "")).strip()
        and str(item.get("name", "")).strip().lower() not in ("unknown", "n/a", "none")
    ]

    # Format as readable answer
    lines = [f"## Extracted {extraction_type.capitalize()}\n"]
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name", "Unknown")
        value = item.get("value", "")
        desc = item.get("description", "")
        # Sanitize page: LLM may return null, "null", 0, or a real int
        raw_page = item.get("page")
        page_num: int | None = None
        try:
            p = int(raw_page)
            if p > 0:
                page_num = p
        except (TypeError, ValueError):
            pass
        line = f"- **{name}**"
        if value:
            line += f": {value}"
        if desc:
            line += f" — {desc}"
        if page_num:
            line += f" [p.{page_num}]"
        lines.append(line)

    if summary:
        lines.append(f"\n**Summary:** {summary}")

    state["_analysis_result"] = "\n".join(lines)
    state["_extracted_items"] = items
    return state
