"""
Aggregation node — always the first node in the query graph.

Loads context from MongoDB (L3 global, relevant L2 clusters, relevant L1 sections)
with token budget accounting. Determines which context to pass downstream.
"""
from __future__ import annotations

import logging
import os

import tiktoken

from modus_schemas import AgentState, QueryType

logger = logging.getLogger(__name__)

# Token budget: 120K (safety margin under 128K)
TOKEN_BUDGET = 120_000

# Rough token limits for each compression level
L3_TOKEN_BUDGET = 3_500
L2_TOKEN_BUDGET = 4_500   # per cluster digest
L1_TOKEN_BUDGET = 1_800   # per section summary

# SUMMARIZE_FULL budget for llama3.1-8b (8K context window).
# 8192 total - 4096 max_tokens output - ~900 prompt overhead = ~3200 for context.
SUMMARIZE_FULL_CONTEXT_BUDGET = 3_200

_encoder = None

EXTRACT_TYPES = {QueryType.EXTRACT_ENTITIES, QueryType.EXTRACT_RISKS, QueryType.EXTRACT_DECISIONS}


def _count_tokens(text: str) -> int:
    global _encoder
    if _encoder is None:
        _encoder = tiktoken.get_encoding("cl100k_base")
    return len(_encoder.encode(text))


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    global _encoder
    if _encoder is None:
        _encoder = tiktoken.get_encoding("cl100k_base")
    tokens = _encoder.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return _encoder.decode(tokens[:max_tokens])


def _select_sections_for_query(
    state: AgentState,
) -> list[str]:
    """
    Determine which section IDs are most relevant to the query.
    For cross-compare: use state["query"].section_ids.
    For section summary: use state["query"].section_ids (plus neighbors — handled in aggregation_node).
    For EXTRACT_* queries: sort by content density so dense sections load first.
    Otherwise: use all sections (budget permitting).
    """
    query = state["query"]
    doc = state["doc"]

    if query.section_ids:
        return query.section_ids

    # For EXTRACT_* queries: sort by content density so information-rich sections
    # are loaded first and aren't cut off by the token budget.
    if query.query_type in EXTRACT_TYPES:
        section_summaries_list = list(doc.section_summaries)
        if query.query_type == QueryType.EXTRACT_ENTITIES:
            section_summaries_list.sort(key=lambda s: len(s.key_metrics), reverse=True)
        elif query.query_type == QueryType.EXTRACT_RISKS:
            section_summaries_list.sort(key=lambda s: len(s.key_risks), reverse=True)
        elif query.query_type == QueryType.EXTRACT_DECISIONS:
            section_summaries_list.sort(
                key=lambda s: sum(1 for c in s.claims if c.claim_type == "commitment"),
                reverse=True,
            )
        return [s.section_id for s in section_summaries_list]

    # For full summary: return all section IDs (L1 budget will limit)
    return [s.section_id for s in doc.section_boundaries]


async def aggregation_node(state: AgentState) -> AgentState:
    """
    Load and assemble hierarchical context for the query.
    Respects the 120K token budget.
    """
    query = state["query"]
    doc = state["doc"]
    budget_used = 0
    context_used: list[str] = []

    # Always load L3 global digest
    global_context = ""
    if doc.global_digest:
        global_context = doc.global_digest.digest_text
        # Fix 1: append executive_summary so SUMMARIZE_FULL has access to
        # the data-driven ~300-word summary that includes key metrics.
        if doc.global_digest.executive_summary:
            global_context += (
                "\n\n## Executive Summary\n" + doc.global_digest.executive_summary
            )
        # P2-1: append LLM-curated top_metrics and top_risks from the L3 digest
        if doc.global_digest.top_metrics:
            lines = "\n".join(f"- {k}: {v}" for k, v in doc.global_digest.top_metrics.items())
            global_context += f"\n\n## Top Metrics\n{lines}"
        if doc.global_digest.top_risks:
            risk_lines = "\n".join(f"- {r}" for r in doc.global_digest.top_risks)
            global_context += f"\n\n## Top Risks\n{risk_lines}"
        budget_used += _count_tokens(global_context)
        context_used.append("L3:global")

    # SUMMARIZE_FULL: llama3.1-8b has an 8K context window.
    # Truncate L3 to fit the budget and return early — skip L2 and L1.
    if query.query_type == QueryType.SUMMARIZE_FULL:
        global_context = _truncate_to_tokens(global_context, SUMMARIZE_FULL_CONTEXT_BUDGET)
        budget_used = _count_tokens(global_context)
        state["_global_context"] = global_context
        state["_cluster_context"] = ""
        state["_section_context"] = ""
        state["context_used"] = context_used
        state["token_budget_used"] = budget_used
        logger.info(f"SUMMARIZE_FULL: {budget_used} tokens (L3 only, capped for 8K model)")
        return state

    # Load L2 cluster digests (up to budget)
    cluster_context_parts = []
    for cd in doc.cluster_digests:
        tokens = _count_tokens(cd.digest_text)
        if budget_used + tokens < TOKEN_BUDGET * 0.4:  # max 40% for L2
            cluster_text = f"[Cluster {cd.cluster_index + 1}]\n{cd.digest_text}"
            # P2-2: append consolidated_metrics if available (populated after re-ingestion)
            if cd.consolidated_metrics:
                metric_lines = "\n".join(f"- {k}: {v}" for k, v in cd.consolidated_metrics.items())
                cluster_text += f"\nConsolidated Metrics:\n{metric_lines}"
            cluster_context_parts.append(cluster_text)
            budget_used += tokens
            context_used.append(f"L2:cluster_{cd.cluster_index}")

    cluster_context = "\n\n---\n\n".join(cluster_context_parts)

    # Load relevant L1 section summaries
    # SUMMARIZE_FULL skips L1 — L3+L2 already synthesize the full document,
    # and loading all sections risks a 413 Payload Too Large from the API.
    section_context_parts = []
    if query.query_type != QueryType.SUMMARIZE_FULL:
        relevant_section_ids = _select_sections_for_query(state)

        # Fix 6: for SUMMARIZE_SECTION with explicit section_ids, also load
        # adjacent sections (±20 pages) so multi-part sections get full context.
        if query.query_type == QueryType.SUMMARIZE_SECTION and query.section_ids:
            requested_set = set(query.section_ids)
            requested_pages = {
                s.section_id: (s.start_page, s.end_page)
                for s in doc.section_boundaries
                if s.section_id in requested_set
            }
            if requested_pages:
                min_page = min(r[0] for r in requested_pages.values())
                max_page = max(r[1] for r in requested_pages.values())
                sorted_boundaries = sorted(doc.section_boundaries, key=lambda s: s.start_page)
                neighbor_ids = [
                    b.section_id for b in sorted_boundaries
                    if b.section_id not in requested_set
                    and b.end_page >= min_page - 20
                    and b.start_page <= max_page + 20
                ]
                relevant_section_ids = list(query.section_ids) + neighbor_ids[:4]

        section_summaries = {
            s.section_id: s for s in doc.section_summaries
        }
        section_boundaries_map = {
            b.section_id: b for b in doc.section_boundaries
        }

        for sid in relevant_section_ids:
            if sid not in section_summaries:
                continue
            s = section_summaries[sid]
            b = section_boundaries_map.get(sid)
            page_ref = f" [pp.{b.start_page + 1}–{b.end_page + 1}]" if b else ""
            summary_text = (
                f"[Section: {sid}{page_ref}]\n{s.summary_text}\n"
                f"Key Metrics: {s.key_metrics}\n"
                f"Key Risks: {', '.join(s.key_risks[:5])}"
            )
            tokens = _count_tokens(summary_text)
            if budget_used + tokens < TOKEN_BUDGET * 0.85:  # leave 15% for answer
                section_context_parts.append(summary_text)
                budget_used += tokens
                context_used.append(f"L1:{sid}")
            else:
                logger.warning(f"Token budget reached — skipping section {sid}")
                break

    section_context = "\n\n---\n\n".join(section_context_parts)

    # Store assembled context in state for downstream nodes
    state["_global_context"] = global_context
    state["_cluster_context"] = cluster_context
    state["_section_context"] = section_context
    state["context_used"] = context_used
    state["token_budget_used"] = budget_used

    logger.info(
        f"Aggregation: {budget_used} tokens used, "
        f"{len(context_used)} context nodes loaded"
    )
    return state
