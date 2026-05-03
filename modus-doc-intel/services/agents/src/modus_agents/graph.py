"""
LangGraph 0.2+ query graph.

Entry: aggregation (context loading)
Conditional routing → local_analysis | global_reasoning | extraction | contradiction
All branches → query (final synthesis)

LangSmith tracing is automatic when LANGCHAIN_TRACING_V2=true + LANGCHAIN_API_KEY are set.
"""
from __future__ import annotations

import os
from langgraph.graph import StateGraph, END

from modus_schemas import AgentState
from modus_agents.routing import route_query
from modus_agents.nodes import (
    aggregation_node,
    local_analysis_node,
    global_reasoning_node,
    query_node,
    extraction_node,
    contradiction_node,
)

# LangSmith tracing — supports both LANGSMITH_* (new) and LANGCHAIN_* (legacy) vars
_tracing = (
    os.environ.get("LANGSMITH_TRACING", "false").lower() == "true"
    or os.environ.get("LANGCHAIN_TRACING_V2", "false").lower() == "true"
)
_project = os.environ.get("LANGSMITH_PROJECT") or os.environ.get("LANGCHAIN_PROJECT", "modus")

if _tracing:
    import logging
    logging.getLogger(__name__).info(
        f"LangSmith tracing ENABLED — project: '{_project}' "
        f"(view at https://smith.langchain.com)"
    )


def build_query_graph():
    g = StateGraph(AgentState)

    # Add all nodes — names appear as node labels in LangSmith trace
    g.add_node("aggregation",      aggregation_node)      # Load L3/L2/L1 context
    g.add_node("local_analysis",   local_analysis_node)   # Per-section deep dive
    g.add_node("global_reasoning", global_reasoning_node) # Full-doc synthesis
    g.add_node("extraction",       extraction_node)        # JSON entity/risk/decision
    g.add_node("contradiction",    contradiction_node)     # DuckDB + LLM conflict check
    g.add_node("query",            query_node)             # Final answer synthesis

    # Entry point is always aggregation
    g.set_entry_point("aggregation")

    # Conditional routing from aggregation — edge labels visible in LangSmith
    g.add_conditional_edges(
        "aggregation",
        route_query,
        {
            "section_summary": "local_analysis",
            "full_summary":    "global_reasoning",
            "cross_compare":   "local_analysis",
            "extract":         "extraction",
            "contradiction":   "contradiction",
        },
    )

    # All branches flow to query (final synthesis)
    for node in ["local_analysis", "global_reasoning", "extraction", "contradiction"]:
        g.add_edge(node, "query")

    # Query is terminal
    g.add_edge("query", END)

    return g.compile()


# Module-level compiled graph singleton
query_graph = build_query_graph()

__all__ = ["query_graph", "build_query_graph"]
