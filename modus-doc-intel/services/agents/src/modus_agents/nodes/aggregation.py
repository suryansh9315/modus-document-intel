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

_encoder = None


def _count_tokens(text: str) -> int:
    global _encoder
    if _encoder is None:
        _encoder = tiktoken.get_encoding("cl100k_base")
    return len(_encoder.encode(text))


def _select_sections_for_query(
    state: AgentState,
) -> list[str]:
    """
    Determine which section IDs are most relevant to the query.
    For cross-compare: use state["query"].section_ids.
    For section summary: use state["query"].section_ids.
    Otherwise: use all sections (budget permitting).
    """
    query = state["query"]
    doc = state["doc"]

    if query.section_ids:
        return query.section_ids

    # For full summary: return all section IDs (L1 budget will limit)
    return [s.section_id for s in doc.section_boundaries]


async def aggregation_node(state: AgentState) -> AgentState:
    """
    Load and assemble hierarchical context for the query.
    Respects the 120K token budget.
    """
    doc = state["doc"]
    budget_used = 0
    context_used: list[str] = []

    # Always load L3 global digest
    global_context = ""
    if doc.global_digest:
        global_context = doc.global_digest.digest_text
        budget_used += _count_tokens(global_context)
        context_used.append("L3:global")

    # Load L2 cluster digests (up to budget)
    cluster_context_parts = []
    for cd in doc.cluster_digests:
        tokens = _count_tokens(cd.digest_text)
        if budget_used + tokens < TOKEN_BUDGET * 0.4:  # max 40% for L2
            cluster_context_parts.append(
                f"[Cluster {cd.cluster_index + 1}]\n{cd.digest_text}"
            )
            budget_used += tokens
            context_used.append(f"L2:cluster_{cd.cluster_index}")

    cluster_context = "\n\n---\n\n".join(cluster_context_parts)

    # Load relevant L1 section summaries
    relevant_section_ids = _select_sections_for_query(state)
    section_summaries = {
        s.section_id: s for s in doc.section_summaries
    }

    section_context_parts = []
    for sid in relevant_section_ids:
        if sid not in section_summaries:
            continue
        s = section_summaries[sid]
        summary_text = (
            f"[Section: {sid}]\n{s.summary_text}\n"
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
    state["_global_context"] = global_context        # type: ignore[typeddict-unknown-key]
    state["_cluster_context"] = cluster_context      # type: ignore[typeddict-unknown-key]
    state["_section_context"] = section_context      # type: ignore[typeddict-unknown-key]
    state["context_used"] = context_used
    state["token_budget_used"] = budget_used

    logger.info(
        f"Aggregation: {budget_used} tokens used, "
        f"{len(context_used)} context nodes loaded"
    )
    return state
