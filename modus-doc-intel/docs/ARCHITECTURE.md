# Architecture: Modus Document Intelligence System

## System Overview

A two-phase multi-agent system for processing large scanned documents (500+ pages):

```
                        ┌─────────────────────────────────────────────────┐
                        │          PHASE 1: OFFLINE INGESTION              │
                        │           (async pipeline, FastAPI)               │
                        └─────────────────────────────────────────────────┘
                                          │
                    ┌─────────────────────▼──────────────────────┐
PDF ──────────────► │                                              │
                    │   OCR Task                                   │
                    │   (docTR + pdfplumber hybrid)                │
                    │   → L0 PageOCR records                       │
                    └─────────────────────┬──────────────────────┘
                                          │ N PageOCR objects (one per page)
                    ┌─────────────────────▼──────────────────────┐
                    │   Segmentation Task                          │
                    │   (heading regex + fallback chunking)        │
                    │   → SectionBoundary records                  │
                    └─────────────────────┬──────────────────────┘
                                          │ ~5–50+ sections
                    ┌─────────────────────▼──────────────────────┐
                    │   Local Analysis (parallel, llama3.1-8b)     │
                    │   Semaphore(4), no throttle (Cerebras)       │
                    │   → L1 SectionSummary + ExtractedClaim       │
                    └──────────┬──────────────────────┬───────────┘
                               │                      │
                    ┌──────────▼──────┐    ┌──────────▼──────────┐
                    │   MongoDB       │    │   DuckDB             │
                    │   (documents)   │    │   claims table       │
                    └──────────┬──────┘    └─────────────────────┘
                               │
                    ┌──────────▼──────────────────────────────────┐
                    │   Aggregation Task (Llama-70B)               │
                    │   Cluster 5–7 sections → L2 ClusterDigest   │
                    │   All clusters → L3 GlobalDigest             │
                    └──────────┬──────────────────────────────────┘
                               │ Ready!
                        MongoDB stores full DocumentRecord


                        ┌─────────────────────────────────────────────────┐
                        │          PHASE 2: ONLINE QUERY                   │
                        │              (LangGraph)                          │
                        └─────────────────────────────────────────────────┘
                                          │
User Query ──────────── FastAPI ─────────► Aggregation Node
                                          │ (load L3+L2+L1, budget 120K tokens)
                                          │
                              ┌───────────▼────────────────────┐
                              │         Router                   │
                              │    (QueryType classification)    │
                              └──┬────────┬───────┬──────┬──────┘
                                 │        │       │      │
                          SECTION FULL  CROSS  EXTRACT CONTRA
                          SUMMARY  SUM  COMP    DICT   DICT
                                 │        │       │      │
                          Local  Global  Local  Extract Contra
                          Node   Node    Node   Node    Node
                                 │        │       │      │
                              ┌──┴────────┴───────┴──────┴──────┐
                              │         Query Node               │
                              │    (final synthesis + citations)  │
                              └──────────────────────────────────┘
                                          │ SSE stream
                              FastAPI → Next.js → Browser
```

## Two-Phase Design

### Phase 1: Offline Ingestion

**Purpose:** Pre-process the entire document into hierarchical summaries that can be
loaded into a 128K context window at query time.

The ingestion pipeline runs as plain async Python, triggered as a background task
by FastAPI on document upload. No external orchestrator is required.

**Pipeline:**
1. **OCR** (`modus_workers/tasks/ocr.py`) — pdfplumber for text-native pages, docTR
   for scanned/image pages. Tables serialized as markdown to prevent hallucination.
2. **Segmentation** (`modus_workers/tasks/segment.py`) — heading regex detection with
   fallback to equal 30-page chunks (ensures ≥5 sections for L1/L2/L3 pipeline).
3. **L1 Analysis** (`modus_workers/tasks/summarize.py`) — Cerebras calls (llama3.1-8b)
   via `asyncio.Semaphore(1)` with 3-second inter-request sleep for rate-limit compliance.
   Small sections (< 4 pages) are merged before processing to reduce total API calls.
   Sections exceeding 8K chars are split into overlapping 8K-char chunks (500-char overlap);
   one LLM call is made per chunk and results are merged (key_metrics unioned, claims deduplicated).
   OCR output is cached as `{stem}_ocr.json` alongside the PDF to skip re-OCR on retries.
4. **DuckDB Write** (`modus_workers/tasks/duckdb_write.py`) — all ExtractedClaims
   written to DuckDB for SQL-based contradiction detection and extraction seeding.
5. **L2 Aggregation** — 5–7 sections clustered and digested by gpt-oss-120b.
   Stores `consolidated_metrics: dict[str, str]` on `ClusterDigest` — the LLM-curated
   metrics across the cluster (previously generated but discarded).
6. **L3 Global** — all cluster digests synthesized into a single global digest by gpt-oss-120b.
   Stores `top_metrics: dict[str, str]` and `top_risks: list[str]` on `GlobalDigest` —
   LLM-curated cross-document key figures and risks (previously generated but discarded).

### Phase 2: Online Query (LangGraph)

**Purpose:** Answer user questions in <10 seconds by loading pre-computed summaries
into a structured context budget and routing to specialized agent nodes.

**Nodes:**
- `aggregation` — loads L3 + relevant L2 + L1 within 120K token budget; for `EXTRACT_*` queries sorts L1 sections by content density (most metric-rich first); for `SUMMARIZE_SECTION` also loads up to 4 neighboring sections within ±20 pages
- `local_analysis` — deep-dive on specific sections
- `global_reasoning` — synthesizes full-document context (L3 now includes executive_summary, top_metrics, top_risks, and an aggregated Key Metrics block)
- `extraction` — Llama-8B JSON-mode extraction; seeded with pre-extracted DuckDB claims; full context limits (no truncation at L3/L2)
- `contradiction` — DuckDB SQL + Llama-70B; candidates re-sorted by question-keyword relevance before top-20 cap
- `query` — final answer synthesis with page citations

## Data Flow

```
PDF File
  └─► OCR (pdfplumber / docTR)
        └─► PageOCR[]  (L0)
              └─► Section Detection
                    └─► SectionBoundary[]
                          └─► L1 Analysis (Llama-70B, per-section)
                                ├─► SectionSummary[]  (L1) ──► MongoDB
                                └─► ExtractedClaim[]  ──────► DuckDB
                                      └─► L2 Aggregation (llama-3.3-70b)
                                            └─► ClusterDigest[]  (L2) ──► MongoDB
                                                  └─► L3 Global (llama-3.3-70b)
                                                        └─► GlobalDigest  (L3) ──► MongoDB

User Query ──► FastAPI ──► LangGraph ──► MongoDB (load context) ──► Cerebras ──► SSE ──► Browser
```

## Component Descriptions

| Component | Location | Responsibility |
|---|---|---|
| OCR Task | `services/workers/tasks/ocr.py` | Hybrid text extraction |
| Segmentation Task | `services/workers/tasks/segment.py` | Section boundary detection |
| Summarization Tasks | `services/workers/tasks/summarize.py` | L1/L2/L3 generation |
| DuckDB Task | `services/workers/tasks/duckdb_write.py` | Claims storage + contradiction queries |
| Ingestion Flow | `services/workers/flows/ingest_document.py` | Async pipeline orchestration |
| Cerebras Client | `services/agents/llm.py` | Direct httpx to Cerebras API |
| LangGraph Graph | `services/agents/graph.py` | Query orchestration |
| Agent Nodes | `services/agents/nodes/` | Specialized reasoning |
| FastAPI Gateway | `apps/api/` | REST + SSE API |
| Next.js Frontend | `apps/web/` | React UI |
| MongoDB | Docker | Document + summary storage |
| DuckDB | File-based | Claims + contradiction queries |
