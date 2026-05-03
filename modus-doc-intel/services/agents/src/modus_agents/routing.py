"""
Conditional edge function for the LangGraph query graph.
Maps AgentState → routing key string.
"""
from __future__ import annotations

from modus_schemas import AgentState, QueryType


def route_query(state: AgentState) -> str:
    """
    Classify the query type and return the routing key.

    Called as a conditional edge from the 'aggregation' node.
    """
    query_type = state["query"].query_type

    route_map = {
        QueryType.SUMMARIZE_SECTION: "section_summary",
        QueryType.SUMMARIZE_FULL: "full_summary",
        QueryType.CROSS_SECTION_COMPARE: "cross_compare",
        QueryType.EXTRACT_ENTITIES: "extract",
        QueryType.EXTRACT_RISKS: "extract",
        QueryType.EXTRACT_DECISIONS: "extract",
        QueryType.DETECT_CONTRADICTIONS: "contradiction",
    }

    route = route_map.get(query_type, "full_summary")
    # Store route in state for downstream nodes
    state["route"] = route
    return route
