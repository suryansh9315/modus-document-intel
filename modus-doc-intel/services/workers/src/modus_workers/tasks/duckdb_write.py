"""
DuckDB schema creation and write operations for claims and entities.

Thread safety: all writes use a threading.Lock.
Read-only connections used at query time.
"""
from __future__ import annotations

import logging
import os
import threading
import uuid
from pathlib import Path

import duckdb

from modus_schemas import ExtractedClaim, ExtractedEntity, SectionSummary

logger = logging.getLogger(__name__)

_write_lock = threading.Lock()
_DUCKDB_PATH: str | None = None


def get_duckdb_path() -> str:
    global _DUCKDB_PATH
    if _DUCKDB_PATH is None:
        _DUCKDB_PATH = os.environ.get("DUCKDB_PATH", "/data/modus.duckdb")
    return _DUCKDB_PATH


def init_schema(db_path: str | None = None) -> None:
    """Create tables if they don't exist."""
    path = db_path or get_duckdb_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    with _write_lock:
        con = duckdb.connect(path)
        try:
            con.execute("""
                CREATE TABLE IF NOT EXISTS claims (
                    claim_id    VARCHAR PRIMARY KEY,
                    doc_id      VARCHAR NOT NULL,
                    section_id  VARCHAR NOT NULL,
                    page_number INTEGER,
                    claim_text  TEXT,
                    claim_type  VARCHAR,
                    subject     VARCHAR,
                    value       VARCHAR,
                    confidence  FLOAT
                )
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS entities (
                    entity_id    VARCHAR PRIMARY KEY,
                    doc_id       VARCHAR NOT NULL,
                    section_id   VARCHAR NOT NULL,
                    entity_type  VARCHAR,
                    name         VARCHAR,
                    normalized   VARCHAR,
                    page_numbers VARCHAR
                )
            """)
            con.execute("""
                CREATE INDEX IF NOT EXISTS idx_claims_doc
                ON claims (doc_id, subject)
            """)
            con.execute("""
                CREATE INDEX IF NOT EXISTS idx_entities_doc
                ON entities (doc_id, entity_type)
            """)
            logger.info(f"DuckDB schema initialized at {path}")
        finally:
            con.close()


def write_claims(claims: list[ExtractedClaim], db_path: str | None = None) -> int:
    """Write extracted claims to DuckDB. Returns count written."""
    if not claims:
        return 0
    path = db_path or get_duckdb_path()

    with _write_lock:
        con = duckdb.connect(path)
        try:
            rows = [
                (
                    c.claim_id,
                    c.doc_id,
                    c.section_id,
                    c.page_number,
                    c.claim_text,
                    c.claim_type,
                    c.subject.lower().strip(),  # normalize for contradiction detection
                    c.value,
                    c.confidence,
                )
                for c in claims
            ]
            # Use INSERT OR REPLACE to handle re-runs
            con.executemany(
                """
                INSERT OR REPLACE INTO claims
                (claim_id, doc_id, section_id, page_number, claim_text,
                 claim_type, subject, value, confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            logger.info(f"Wrote {len(rows)} claims to DuckDB")
            return len(rows)
        finally:
            con.close()


def write_entities(entities: list[ExtractedEntity], db_path: str | None = None) -> int:
    """Write extracted entities to DuckDB. Returns count written."""
    if not entities:
        return 0
    path = db_path or get_duckdb_path()

    with _write_lock:
        con = duckdb.connect(path)
        try:
            rows = [
                (
                    e.entity_id,
                    e.doc_id,
                    e.section_id,
                    e.entity_type,
                    e.name,
                    e.normalized,
                    ",".join(str(p) for p in e.page_numbers),
                )
                for e in entities
            ]
            con.executemany(
                """
                INSERT OR REPLACE INTO entities
                (entity_id, doc_id, section_id, entity_type, name, normalized, page_numbers)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            logger.info(f"Wrote {len(rows)} entities to DuckDB")
            return len(rows)
        finally:
            con.close()


def write_section_claims(
    summaries: list[SectionSummary], db_path: str | None = None
) -> int:
    """Extract and write all claims from a batch of section summaries.

    Also synthesizes metric claims from key_metrics dicts so the contradiction
    self-join has consistent subject strings to match on across sections.
    """
    all_claims = [c for s in summaries for c in s.claims]

    for s in summaries:
        base_page = min((c.page_number for c in s.claims), default=0)
        for key, val in s.key_metrics.items():
            if val:
                all_claims.append(ExtractedClaim(
                    doc_id=s.doc_id,
                    section_id=s.section_id,
                    page_number=base_page,
                    claim_text=f"{key}: {val}",
                    claim_type="metric",
                    subject=key.lower().strip(),
                    value=val,
                    confidence=1.0,
                ))

    return write_claims(all_claims, db_path)


def write_section_entities(
    summaries: list[SectionSummary], db_path: str | None = None
) -> int:
    """Convert SectionSummary.key_entities to ExtractedEntity and write to DuckDB."""
    all_entities: list[ExtractedEntity] = []
    for s in summaries:
        base_page = min((c.page_number for c in s.claims), default=0)
        for item in s.key_entities:
            name = item.get("name", "") if isinstance(item, dict) else str(item)
            etype = item.get("type", "UNKNOWN") if isinstance(item, dict) else "UNKNOWN"
            if not name:
                continue
            all_entities.append(ExtractedEntity(
                doc_id=s.doc_id,
                section_id=s.section_id,
                entity_type=etype,
                name=name,
                normalized=name.lower().strip(),
                page_numbers=[base_page] if base_page else [],
            ))
    return write_entities(all_entities, db_path)


def get_entities_for_extraction(
    doc_id: str, db_path: str | None = None
) -> list[dict]:
    """Get all named entities for a document, ordered by entity_type then name."""
    path = db_path or get_duckdb_path()
    con = duckdb.connect(path, read_only=True)
    try:
        results = con.execute(
            "SELECT name, entity_type, page_numbers "
            "FROM entities WHERE doc_id = ? "
            "ORDER BY entity_type, name",
            [doc_id],
        ).fetchall()
        cols = ["name", "entity_type", "page_numbers"]
        return [dict(zip(cols, row)) for row in results]
    finally:
        con.close()


def query_contradictions(
    doc_id: str, db_path: str | None = None
) -> list[dict]:
    """
    Find potential contradictions: same subject, same doc, different values.

    Uses read-only connection for thread safety at query time.
    """
    path = db_path or get_duckdb_path()
    # Read-only for concurrent query access
    con = duckdb.connect(path, read_only=True)
    try:
        results = con.execute(
            """
            SELECT
                a.claim_text AS claim_a_text,
                b.claim_text AS claim_b_text,
                a.section_id AS section_a_id,
                b.section_id AS section_b_id,
                a.page_number AS page_a,
                b.page_number AS page_b,
                a.subject,
                a.value AS value_a,
                b.value AS value_b
            FROM claims a
            JOIN claims b
              ON a.doc_id = b.doc_id
             AND a.subject = b.subject
             AND a.claim_id < b.claim_id
             AND a.value IS NOT NULL
             AND b.value IS NOT NULL
             AND a.value != b.value
            WHERE a.doc_id = ?
            ORDER BY a.subject, a.page_number
            """,
            [doc_id],
        ).fetchall()

        columns = [
            "claim_a_text", "claim_b_text", "section_a_id", "section_b_id",
            "page_a", "page_b", "subject", "value_a", "value_b",
        ]
        return [dict(zip(columns, row)) for row in results]
    finally:
        con.close()


def get_claims_by_type(
    doc_id: str, claim_type: str, db_path: str | None = None
) -> list[dict]:
    """Get all claims of a specific type for a document, ordered by confidence."""
    path = db_path or get_duckdb_path()
    con = duckdb.connect(path, read_only=True)
    try:
        results = con.execute(
            "SELECT claim_text, subject, value, page_number, section_id "
            "FROM claims WHERE doc_id = ? AND claim_type = ? "
            "ORDER BY confidence DESC",
            [doc_id, claim_type],
        ).fetchall()
        cols = ["claim_text", "subject", "value", "page_number", "section_id"]
        return [dict(zip(cols, row)) for row in results]
    finally:
        con.close()


def get_claims_for_doc(doc_id: str, db_path: str | None = None) -> list[dict]:
    """Get all claims for a document (read-only)."""
    path = db_path or get_duckdb_path()
    con = duckdb.connect(path, read_only=True)
    try:
        results = con.execute(
            "SELECT * FROM claims WHERE doc_id = ? ORDER BY page_number",
            [doc_id],
        ).fetchall()
        columns = [
            "claim_id", "doc_id", "section_id", "page_number", "claim_text",
            "claim_type", "subject", "value", "confidence",
        ]
        return [dict(zip(columns, row)) for row in results]
    finally:
        con.close()
