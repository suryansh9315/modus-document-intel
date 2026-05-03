# Architecture: Modus Document Intelligence System

## System Overview

A two-phase multi-agent system for processing large financial PDFs:

```
                        ┌─────────────────────────────────────────────────┐
                        │          PHASE 1: OFFLINE INGESTION              │
                        │                 (Prefect)                         │
                        └─────────────────────────────────────────────────┘
                                          │
                    ┌─────────────────────▼──────────────────────┐
PDF ──────────────► │                                              │
                    │   OCR Task                                   │
                    │   (docTR + pdfplumber hybrid)                │
                    │   → L0 PageOCR records                       │
                    └─────────────────────┬──────────────────────┘
                                          │ 341 PageOCR objects
                    ┌─────────────────────▼──────────────────────┐
                    │   Segmentation Task                          │
                    │   (heading regex + font heuristics)          │
                    │   → SectionBoundary records                  │
                    └─────────────────────┬──────────────────────┘
                                          │ ~30-50 sections
                    ┌─────────────────────▼──────────────────────┐
                    │   Local Analysis (parallel, Llama-70B)       │
                    │   1 Groq call per section, asyncio.Semaphore │
                    │   → L1 SectionSummary + ExtractedClaim       │
                    └──────────┬──────────────────────┬───────────┘
                               │                      │
                    ┌──────────▼──────┐    ┌──────────▼──────────┐
                    │   MongoDB       │    │   DuckDB             │
                    │   (documents)   │    │   claims + entities  │
                    └──────────┬──────┘    └─────────────────────┘
                               │
                    ┌──────────▼──────────────────────────────────┐
                    │   Aggregation Task (Llama-70B)               │
                    │   Cluster 5-7 sections → L2 ClusterDigest   │
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

### Phase 1: Offline Ingestion (Prefect)

**Purpose:** Pre-process the entire document into hierarchical summaries that can be
loaded into a 128K context window at query time.

**Pipeline:**
1. **OCR** (`modus_workers/tasks/ocr.py`) — pdfplumber for text-native pages, docTR
   for scanned/image pages. Tables serialized as markdown to prevent hallucination.
2. **Segmentation** (`modus_workers/tasks/segment.py`) — heading regex detection with
   fallback to equal 30-page chunks (ensures ≥5 sections for L1/L2/L3 pipeline).
3. **L1 Analysis** (`modus_workers/tasks/summarize.py`) — parallel Groq calls
   (Llama-70B), one per section. Rate-limited via `asyncio.Semaphore(5)`.
4. **DuckDB Write** (`modus_workers/tasks/duckdb_write.py`) — all ExtractedClaims
   written to DuckDB for SQL-based contradiction detection.
5. **L2 Aggregation** — 5-7 sections clustered and digested by Llama-70B.
6. **L3 Global** — all cluster digests synthesized into a single global digest.

**Runtime:** ~30-45 minutes for a 341-page PDF.

### Phase 2: Online Query (LangGraph)

**Purpose:** Answer user questions in <10 seconds by loading pre-computed summaries
into a structured context budget and routing to specialized agent nodes.

**Nodes:**
- `aggregation` — loads L3 + relevant L2 + L1 within 120K token budget
- `local_analysis` — deep-dive on specific sections
- `global_reasoning` — synthesizes full-document context
- `extraction` — Llama-8B JSON-mode extraction of entities/risks/decisions
- `contradiction` — DuckDB SQL + Llama-70B contradiction classification
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
                                      └─► L2 Aggregation (Llama-70B)
                                            └─► ClusterDigest[]  (L2) ──► MongoDB
                                                  └─► L3 Global (Llama-70B)
                                                        └─► GlobalDigest  (L3) ──► MongoDB

User Query ──► FastAPI ──► LangGraph ──► MongoDB (load context) ──► Groq ──► SSE ──► Browser
```

## Component Descriptions

| Component | Location | Responsibility |
|---|---|---|
| OCR Task | `services/workers/tasks/ocr.py` | Hybrid text extraction |
| Segmentation Task | `services/workers/tasks/segment.py` | Section boundary detection |
| Summarization Tasks | `services/workers/tasks/summarize.py` | L1/L2/L3 generation |
| DuckDB Task | `services/workers/tasks/duckdb_write.py` | Claims/entities storage |
| Ingestion Flow | `services/workers/flows/ingest_document.py` | Prefect orchestration |
| Groq Client | `services/agents/llm.py` | Direct httpx to Groq API |
| LangGraph Graph | `services/agents/graph.py` | Query orchestration |
| Agent Nodes | `services/agents/nodes/` | Specialized reasoning |
| FastAPI Gateway | `apps/api/` | REST + SSE API |
| Next.js Frontend | `apps/web/` | React UI |
| MongoDB | Docker | Document + summary storage |
| DuckDB | File-based | Claims + contradiction queries |
