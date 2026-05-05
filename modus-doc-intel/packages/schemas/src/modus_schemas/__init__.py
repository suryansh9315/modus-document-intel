"""
Shared Pydantic models — single source of truth for every service.
"""
from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, Literal, TypedDict

import logging

from pydantic import BaseModel, Field, field_validator

_schema_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class DocumentStatus(str, Enum):
    PENDING = "PENDING"
    INGESTING = "INGESTING"
    SEGMENTING = "SEGMENTING"
    ANALYZING = "ANALYZING"
    AGGREGATING = "AGGREGATING"
    READY = "READY"
    ERROR = "ERROR"


class QueryType(str, Enum):
    SUMMARIZE_SECTION = "SUMMARIZE_SECTION"
    SUMMARIZE_FULL = "SUMMARIZE_FULL"
    CROSS_SECTION_COMPARE = "CROSS_SECTION_COMPARE"
    EXTRACT_ENTITIES = "EXTRACT_ENTITIES"
    EXTRACT_RISKS = "EXTRACT_RISKS"
    EXTRACT_DECISIONS = "EXTRACT_DECISIONS"
    DETECT_CONTRADICTIONS = "DETECT_CONTRADICTIONS"


class SectionKind(str, Enum):
    CHAPTER = "CHAPTER"
    SECTION = "SECTION"
    SUBSECTION = "SUBSECTION"
    APPENDIX = "APPENDIX"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# Ingestion models
# ---------------------------------------------------------------------------

class PageOCR(BaseModel):
    page_number: int
    raw_text: str
    confidence: float
    ocr_engine: Literal["doctr", "pdfplumber"]
    has_tables: bool = False
    table_markdown: str | None = None


class SectionBoundary(BaseModel):
    section_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    doc_id: str
    title: str
    kind: SectionKind = SectionKind.UNKNOWN
    start_page: int
    end_page: int

    @property
    def page_count(self) -> int:
        return self.end_page - self.start_page + 1


# ---------------------------------------------------------------------------
# Claims / Entities (fuel for contradiction detection)
# ---------------------------------------------------------------------------

_VALID_CLAIM_TYPES = {"metric", "statement", "commitment", "risk_factor", "constraint"}


class ExtractedClaim(BaseModel):
    claim_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    doc_id: str
    section_id: str
    page_number: int
    claim_text: str
    claim_type: Literal["metric", "statement", "commitment", "risk_factor", "constraint"]
    subject: str  # normalized, e.g. "Net Interest Margin"
    value: str | None = None  # e.g. "4.27%"
    confidence: float = 1.0

    @field_validator("claim_type", mode="before")
    @classmethod
    def coerce_claim_type(cls, v: object) -> str:
        if v not in _VALID_CLAIM_TYPES:
            _schema_logger.warning(f"Unknown claim_type {v!r}, coercing to 'statement'")
            return "statement"
        return v  # type: ignore[return-value]


class ExtractedEntity(BaseModel):
    entity_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    doc_id: str
    section_id: str
    entity_type: str  # PERSON | ORG | PRODUCT | REGULATION | METRIC
    name: str
    normalized: str
    page_numbers: list[int] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Hierarchical compression tree (L1 → L2 → L3)
# ---------------------------------------------------------------------------

class SectionSummary(BaseModel):
    """L1 node — one per section, ~1.5K tokens"""
    section_id: str
    doc_id: str
    summary_text: str
    key_metrics: dict[str, str] = Field(default_factory=dict)  # never paraphrased
    key_entities: list[str] = Field(default_factory=list)
    key_risks: list[str] = Field(default_factory=list)
    claims: list[ExtractedClaim] = Field(default_factory=list)


class ClusterDigest(BaseModel):
    """L2 node — cluster of 5-7 sections, ~4K tokens"""
    cluster_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    doc_id: str
    digest_text: str
    section_ids: list[str] = Field(default_factory=list)
    cluster_index: int = 0
    consolidated_metrics: dict[str, str] = Field(default_factory=dict)  # P2-2


class GlobalDigest(BaseModel):
    """L3 node — whole document, ~3K tokens"""
    doc_id: str
    digest_text: str
    executive_summary: str  # ~300 tokens
    top_metrics: dict[str, str] = Field(default_factory=dict)   # P2-1
    top_risks: list[str] = Field(default_factory=list)           # P2-1


# ---------------------------------------------------------------------------
# Contradiction detection
# ---------------------------------------------------------------------------

class ContradictionReport(BaseModel):
    contradiction_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    subject: str
    claim_a_text: str
    claim_a_section: str
    claim_a_page: int
    claim_b_text: str
    claim_b_section: str
    claim_b_page: int
    explanation: str
    severity: Literal["low", "medium", "high"] = "medium"


# ---------------------------------------------------------------------------
# Document record (stored in MongoDB)
# ---------------------------------------------------------------------------

class DocumentRecord(BaseModel):
    doc_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    filename: str
    total_pages: int = 0
    status: DocumentStatus = DocumentStatus.PENDING
    error_message: str | None = None
    section_boundaries: list[SectionBoundary] = Field(default_factory=list)
    section_summaries: list[SectionSummary] = Field(default_factory=list)
    cluster_digests: list[ClusterDigest] = Field(default_factory=list)
    global_digest: GlobalDigest | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def model_dump_mongo(self) -> dict[str, Any]:
        """Dump for MongoDB storage (uses _id alias)."""
        data = self.model_dump()
        data["_id"] = data.pop("doc_id")
        return data


# ---------------------------------------------------------------------------
# Query models
# ---------------------------------------------------------------------------

class QueryRequest(BaseModel):
    doc_id: str
    query_type: QueryType
    question: str
    section_ids: list[str] | None = None
    stream: bool = True


class QueryResponse(BaseModel):
    answer: str
    sources: list[dict[str, Any]] = Field(default_factory=list)
    contradictions: list[ContradictionReport] = Field(default_factory=list)
    context_used: list[str] = Field(default_factory=list)
    token_budget_used: int = 0


# ---------------------------------------------------------------------------
# LangGraph agent state
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    query: QueryRequest
    doc: DocumentRecord
    context_used: list[str]
    token_budget_used: int
    token_budget_limit: int  # 120_000
    answer: str
    sources: list[dict[str, Any]]
    contradictions: list[ContradictionReport]
    route: str  # internal routing decision
    # Private keys passed between nodes (must be declared for LangGraph to track them)
    _global_context: str
    _cluster_context: str
    _section_context: str
    _analysis_result: str
    _extracted_items: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Ingestion job (for status polling)
# ---------------------------------------------------------------------------

class IngestionJob(BaseModel):
    job_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    doc_id: str
    status: DocumentStatus = DocumentStatus.PENDING
    progress_pct: float = 0.0
    message: str = ""
    error: str | None = None


__all__ = [
    "DocumentStatus",
    "QueryType",
    "SectionKind",
    "PageOCR",
    "SectionBoundary",
    "ExtractedClaim",
    "ExtractedEntity",
    "SectionSummary",
    "ClusterDigest",
    "GlobalDigest",
    "ContradictionReport",
    "DocumentRecord",
    "QueryRequest",
    "QueryResponse",
    "AgentState",
    "IngestionJob",
]
