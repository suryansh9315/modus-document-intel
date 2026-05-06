# Architecture: Modus Document Intelligence System

## System Overview

A two-phase multi-agent system for processing large PDF documents (500+ pages):

```
                        ┌─────────────────────────────────────────────────┐
                        │          PHASE 1: OFFLINE INGESTION              │
                        │      (async pipeline — Cerebras llama3.1-8b)     │
                        └─────────────────────────────────────────────────┘
                                          │
                    ┌─────────────────────▼──────────────────────┐
PDF ──────────────► │                                              │
                    │   OCR Task                                   │
                    │   (pdfplumber — tables as markdown)          │
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
                    │   Local Analysis — L1 (llama3.1-8b)          │
                    │   Cerebras · Semaphore(1) · 6s interval      │
                    │   → L1 SectionSummary + ExtractedClaim       │
                    └──────────┬──────────────────────┬───────────┘
                               │                      │
                    ┌──────────▼──────┐    ┌──────────▼──────────┐
                    │   MongoDB       │    │   DuckDB             │
                    │   (documents)   │    │   claims table       │
                    └──────────┬──────┘    └─────────────────────┘
                               │
                    ┌──────────▼──────────────────────────────────┐
                    │   Aggregation — L2 + L3 (llama3.1-8b)        │
                    │   Cerebras · 5–7 sections → ClusterDigest    │
                    │   All clusters → GlobalDigest                │
                    └──────────┬──────────────────────────────────┘
                               │ Ready!
                        MongoDB stores full DocumentRecord


                        ┌─────────────────────────────────────────────────┐
                        │          PHASE 2: ONLINE QUERY                   │
                        │    (LangGraph — 1 Groq call per query)           │
                        └─────────────────────────────────────────────────┘
                                          │
User Query ──────────── FastAPI ─────────► Aggregation Node
                                          │ (load L3+L2+L1, budget 22K tokens)
                                          │
                              ┌───────────▼────────────────────┐
                              │         Router                   │
                              │    (QueryType classification)    │
                              └──┬──────┬────┬──────┬───────────┘
                                 │      │    │      │
                           EXTRACT_* FULL SECTION CONTRA
                           ENTITIES  SUM  /CROSS   DICT
                                 │      │    │      │
                          Groq   │ Groq │ Groq│  Groq
                          Extract│Global│Local│ Contra
                          Node   │Reason│ Node│ Node
                                 │      │    │      │
                              ┌──┴──────┴────┴──────┴──────────┐
                              │         Query Node               │
                              │         (full passthrough)       │
                              │         no LLM call              │
                              └──────────────────────────────────┘
                                          │ SSE stream
                              FastAPI → Next.js → Browser
```

## Two-Phase Design

### Phase 1: Offline Ingestion

**Purpose:** Pre-process the entire document into hierarchical summaries that can be
loaded into the query-time context budget.

**LLM provider:** Cerebras (`api.cerebras.ai/v1`) — `llama3.1-8b` for all stages.
**Groq is never called during ingestion.**

**Pipeline:**
1. **OCR** (`modus_workers/tasks/ocr.py`) — pdfplumber for all pages. Tables serialized
   as markdown to prevent hallucination.
2. **Segmentation** (`modus_workers/tasks/segment.py`) — heading regex detection with
   fallback to equal 30-page chunks (ensures ≥5 sections for L1/L2/L3 pipeline).
3. **L1 Analysis** (`modus_workers/tasks/summarize.py`) — Cerebras `llama3.1-8b`.
   `asyncio.Semaphore(1)` with 6-second inter-request sleep + TPM-aware throttle (6K TPM limit).
   Small sections (< 4 pages) merged before processing to reduce total API calls.
   Sections exceeding 8K chars split into overlapping 8K-char chunks (500-char overlap);
   one LLM call per chunk, results merged (key_metrics unioned, claims deduplicated).
   OCR output cached as `{stem}_ocr.json` to skip re-OCR on retries.
4. **DuckDB Write** (`modus_workers/tasks/duckdb_write.py`) — ExtractedClaims and
   ExtractedEntities written to DuckDB for contradiction detection and extraction seeding.
5. **L2 Aggregation** — 5–7 sections clustered and digested by `llama3.1-8b`.
   Stores `consolidated_metrics: dict[str, str]` on `ClusterDigest`.
6. **L3 Global** — all cluster digests synthesized into a single global digest by `llama3.1-8b`.
   Stores `top_metrics: dict[str, str]` and `top_risks: list[str]` on `GlobalDigest`.

### Phase 2: Online Query (LangGraph)

**Purpose:** Answer user questions by loading pre-computed summaries into a 22K token
context budget and routing to a specialized agent node.

**LLM providers:**
- **Groq** (`api.groq.com/openai/v1`) — `meta-llama/llama-4-scout-17b-16e-instruct` — all query types (including contradiction detection)

**Every query type makes exactly 1 LLM call. The query_node is a pure passthrough.**

**Nodes:**
- `aggregation` — loads L3 + L2 + L1 within 22K token budget; for `EXTRACT_*` sorts L1 sections by content density; for `SUMMARIZE_SECTION` also loads up to 4 neighboring sections within ±20 pages
- `local_analysis` — Groq: direct answer for SUMMARIZE_SECTION and CROSS_SECTION_COMPARE
- `global_reasoning` — Groq: full-document synthesis for SUMMARIZE_FULL (L3 + L2 context; L1 skipped — L3+L2 already cover the full document)
- `extraction` — Groq JSON mode: structured extraction; EXTRACT_ENTITIES seeded from DuckDB entities table (typed named entities from ingestion); EXTRACT_RISKS/DECISIONS seeded with DuckDB claims
- `contradiction` — Groq JSON mode: DuckDB SQL + LLM classification; candidates re-sorted by question-keyword relevance before top-20 cap
- `query` — passthrough only; no LLM call for any query type

## Data Flow

```
PDF File
  └─► OCR (pdfplumber)
        └─► PageOCR[]  (L0)
              └─► Section Detection
                    └─► SectionBoundary[]
                          └─► L1 Analysis (llama3.1-8b · Cerebras)
                                ├─► SectionSummary[]  (L1) ──► MongoDB
                                ├─► ExtractedClaim[]   ─────► DuckDB (claims)
                                └─► ExtractedEntity[]  ─────► DuckDB (entities)
                                      └─► L2 Aggregation (llama3.1-8b · Cerebras)
                                            └─► ClusterDigest[]  (L2) ──► MongoDB
                                                  └─► L3 Global (llama3.1-8b · Cerebras)
                                                        └─► GlobalDigest  (L3) ──► MongoDB

User Query ──► FastAPI ──► LangGraph ──► MongoDB (load context)
                                              └─► Groq llama-4-scout (all query types)
                                                        └─► SSE ──► Browser
```

## Component Descriptions

| Component | Location | Responsibility |
|---|---|---|
| OCR Task | `services/workers/tasks/ocr.py` | PDF text extraction via pdfplumber |
| Segmentation Task | `services/workers/tasks/segment.py` | Section boundary detection |
| Summarization Tasks | `services/workers/tasks/summarize.py` | L1/L2/L3 generation (Cerebras) |
| DuckDB Task | `services/workers/tasks/duckdb_write.py` | Claims + entities storage, contradiction queries |
| Ingestion Flow | `services/workers/flows/ingest_document.py` | Async pipeline orchestration |
| Cerebras Client (workers) | `services/workers/groq_client.py` | httpx to Cerebras — ingestion only |
| Groq Client (agents) | `services/agents/llm.py` — `GroqPrimaryClient` | httpx to Groq — all query nodes |
| LangGraph Graph | `services/agents/graph.py` | Query orchestration |
| Agent Nodes | `services/agents/nodes/` | Specialized reasoning |
| FastAPI Gateway | `apps/api/` | REST + SSE API |
| Next.js Frontend | `apps/web/` | React UI |
| MongoDB | Docker | Document + summary storage |
| DuckDB | File-based | Claims + contradiction queries |

## API Keys Required

| Key | Provider | Used by |
|---|---|---|
| `CEREBRAS_API_KEY` | Cerebras | Workers (ingestion only) |
| `GROQ_API_KEY` | Groq | Agents (all query types) |
