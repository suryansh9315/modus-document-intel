"""
LangGraph agent state definition.
AgentState is also defined in modus_schemas — this re-exports for convenience.
"""
from modus_schemas import AgentState, ContradictionReport, DocumentRecord, QueryRequest

__all__ = ["AgentState", "ContradictionReport", "DocumentRecord", "QueryRequest"]
