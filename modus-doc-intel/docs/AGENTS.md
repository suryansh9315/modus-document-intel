# Agents: Multi-Agent System Reference

## Agent Inventory

| # | Agent | Input | Output | Model | Provider | Role |
|---|-------|-------|--------|-------|----------|------|
| 1 | **Ingestion** | PDF path + page range | `list[PageOCR]` | docTR / pdfplumber (rules) | Local | OCR + table extraction |
| 2 | **Segmentation** | `list[PageOCR]` | `list[SectionBoundary]` | Rule-based (regex) | Local | Heading detection, fallback chunking |
| 3 | **Local Analysis (L1)** | Section text (~8K chars) | `SectionSummary` | `llama3.1-8b` | Cerebras | Per-section summary + claim extraction |
| 4 | **Cluster Aggregation (L2)** | `list[SectionSummary]` | `ClusterDigest` | `llama3.1-8b` | Cerebras | Hierarchical compression of 5–7 sections |
| 5 | **Global Aggregation (L3)** | `list[ClusterDigest]` | `GlobalDigest` | `llama3.1-8b` | Cerebras | Whole-document synthesis |
| 6 | **Query Router** | `QueryRequest` + `AgentState` | routing decision (str) | Rule-based | Local | Intent classification → branch |
| 7 | **Global Reasoning** | L3 + L2 + L1 context | final answer | `llama-4-scout` | Groq | Full-doc synthesis for SUMMARIZE_FULL |
| 8 | **Local Analysis (Query)** | Section context | final answer | `llama-4-scout` | Groq | Section/cross-compare answering |
| 9 | **Extractor** | L3+L2+L1 context + DuckDB seed claims | JSON entities/risks/decisions | `llama-4-scout` | Groq | Structured extraction in JSON mode |
| 10 | **Contradiction** | DuckDB candidates + context | `list[ContradictionReport]` | `llama-4-scout` | Groq | Conflict classification and explanation |
| 11 | **Query Node** | `_analysis_result` from branch | final answer (passthrough) | — | — | Full passthrough — no LLM call |

---

## LLM Routing Summary

Every query type makes **exactly 1 LLM call**. The query node is a passthrough for all types.

| Query Type | Branch Node | Model | Provider |
|---|---|---|---|
| `EXTRACT_ENTITIES` | extraction_node | `llama-4-scout` | Groq |
| `EXTRACT_RISKS` | extraction_node | `llama-4-scout` | Groq |
| `EXTRACT_DECISIONS` | extraction_node | `llama-4-scout` | Groq |
| `SUMMARIZE_FULL` | global_reasoning_node | `llama-4-scout` | Groq |
| `SUMMARIZE_SECTION` | local_analysis_node | `llama-4-scout` | Groq |
| `CROSS_SECTION_COMPARE` | local_analysis_node | `llama-4-scout` | Groq |
| `DETECT_CONTRADICTIONS` | contradiction_node | `llama-4-scout` | Groq |

---

## Agent Details

### 1. Ingestion Agent (OCR Task)
**File:** `services/workers/tasks/ocr.py`

**Strategy:**
1. Try `pdfplumber` — fast, high-accuracy for text-native PDFs.
2. If text content < 100 chars, fall back to `docTR` (neural OCR for scanned pages).
3. For pages with tables, `pdfplumber.extract_tables()` is called separately.
   Table data is serialized as Markdown and prepended to the raw text.
   This prevents LLMs from hallucinating numbers by re-deriving them from prose.

**Output schema:** `PageOCR(page_number, raw_text, confidence, ocr_engine, has_tables, table_markdown)`

---

### 2. Segmentation Agent (Rule-Based)
**File:** `services/workers/tasks/segment.py`

**Heading patterns (in priority order):**
- `^(?:CHAPTER|SECTION|PART)\s+[\dIVXLCM]+` → SectionKind.CHAPTER
- `^\d+\.\s+[A-Z][A-Z\s]{5,}$` → SectionKind.SECTION
- `^[A-Z][A-Z\s&,\-:]{10,}$` → SectionKind.SECTION (ALL CAPS lines)
- `^\d+\.\d+\s+[A-Z]` → SectionKind.SUBSECTION
- `^ANNEX(?:URE)?\s+[A-Z\d]` → SectionKind.APPENDIX

**Fallback:** If < 5 sections detected, splits into equal 30-page chunks.
This guarantees sufficient granularity for the L1/L2/L3 pipeline regardless of document formatting.

---

### 3. Local Analysis Agent — Ingestion (L1)
**File:** `services/workers/tasks/summarize.py`
**Model:** `llama3.1-8b` via Cerebras

**Process:**
1. Merges sections with fewer than 4 pages into their neighbor before processing (reduces API calls).
2. Concatenates all pages in a section into a single text blob.
3. Tables are prepended in Markdown format to ensure numbers are preserved.
4. **Chunking:** If the full section text exceeds **8K chars**, it is split into overlapping chunks of 8K chars with a 500-char overlap. One `llama3.1-8b` call is made per chunk. Results are merged: `key_metrics` are unioned (later chunks override the same key), `key_entities`/`key_risks`/`claims` are deduplicated and concatenated. This ensures sections of any length are fully analyzed.
5. Extracts per chunk: `summary_text` (~150–200 words), `key_metrics`, `key_entities`, `key_risks`, `claims`.
6. Claims become `ExtractedClaim` objects with normalized subject for contradiction detection.

**Rate limiting:** `asyncio.Semaphore(1)` with 6-second inter-request sleep. TPM-aware sleep: `(tokens_used / 6000) * 60` seconds, capped at minimum 6s. Cerebras `llama3.1-8b` limit: 6K TPM.

---

### 4 & 5. Aggregation Agents — Ingestion (L2 + L3)
**File:** `services/workers/tasks/summarize.py`
**Model:** `llama3.1-8b` via Cerebras (both L2 and L3)

**L2:** Clusters 5–7 consecutive sections (by page proximity) into a `ClusterDigest`.
Input: all L1 summary texts + key metrics concatenated.
Output: ~4K token digest preserving cross-section themes.
Schema fields: `digest_text`, `consolidated_metrics: dict[str, str]` — curated metrics across the cluster, captured from the LLM's JSON output.

**L3:** Synthesizes all cluster digests into a `GlobalDigest`.
Output: ~3K token digest + 300-word executive summary.
Schema fields: `digest_text`, `executive_summary`, `top_metrics: dict[str, str]`, `top_risks: list[str]` — LLM-curated cross-document key figures and risk factors.
This is the "entry point" for the query-time context budget.

---

### 6. Query Router (Rule-Based)
**File:** `services/agents/routing.py`

Maps `QueryType` enum → routing key string used by LangGraph conditional edges:

| QueryType | Route Key | Branch Node |
|---|---|---|
| SUMMARIZE_SECTION | section_summary | local_analysis |
| SUMMARIZE_FULL | full_summary | global_reasoning |
| CROSS_SECTION_COMPARE | cross_compare | local_analysis |
| EXTRACT_ENTITIES | extract | extraction |
| EXTRACT_RISKS | extract | extraction |
| EXTRACT_DECISIONS | extract | extraction |
| DETECT_CONTRADICTIONS | contradiction | contradiction |

---

### 7. Global Reasoning Agent
**File:** `services/agents/nodes/global_reason.py`
**Model:** `meta-llama/llama-4-scout-17b-16e-instruct` via Groq

**Used for:** SUMMARIZE_FULL queries.

Assembles **L3 + L2 + L1 context** and synthesizes an answer using the `query_summarize_full.j2` template. The L3 `global_context` block includes:
- `digest_text` — full narrative synthesis
- `executive_summary` — ≤300-word data-driven summary
- `top_metrics` — LLM-curated key figures
- `top_risks` — LLM-curated top risks

The result flows into `_analysis_result`. The query node passes it through directly — no second LLM call.

---

### 8. Local Analysis Agent — Query
**File:** `services/agents/nodes/local.py`
**Model:** `meta-llama/llama-4-scout-17b-16e-instruct` via Groq

**Used for:** SUMMARIZE_SECTION, CROSS_SECTION_COMPARE

For `CROSS_SECTION_COMPARE`: renders the `query_cross_compare.j2` template with two section contexts.
For `SUMMARIZE_SECTION`: renders the `query_summarize_section.j2` template with the assembled section context (requested section + up to 4 neighbors within ±20 pages).

The result flows into `_analysis_result`. The query node passes it through directly.

---

### 9. Extractor Agent
**File:** `services/agents/nodes/extraction.py`
**Model:** `meta-llama/llama-4-scout-17b-16e-instruct` via Groq

**Used for:** EXTRACT_ENTITIES, EXTRACT_RISKS, EXTRACT_DECISIONS

Uses Groq llama-4-scout with JSON mode (`response_format: {"type": "json_object"}`) and full 22K token context — no truncation.

**Context assembly (from aggregation state):**

| Context level | Content |
|---|---|
| L3 global digest | digest_text + executive_summary + top_metrics + top_risks |
| L2 cluster digests | digest_text + consolidated_metrics per cluster |
| L1 section summaries | summary_text + key_metrics + key_risks (density-sorted) |

**DuckDB seed claims:** Before the LLM call, pre-extracted claims are fetched from DuckDB and prepended as "PRE-EXTRACTED CANDIDATES":
- `EXTRACT_ENTITIES` → no seed (metric seeds caused financial metrics to be returned instead of named entities)
- `EXTRACT_RISKS` → `claim_type = "risk_factor"` claims
- `EXTRACT_DECISIONS` → `claim_type = "commitment"` claims

The LLM refines, deduplicates, and augments the candidate list rather than starting from scratch. Seed fetch failures degrade gracefully.

**Entity extraction scope:** The extraction prompt instructs the model to extract named entities only — PERSON (executives, directors), ORGANIZATION (subsidiaries, regulators, partners), PRODUCT (financial products, services), REGULATION (laws, guidelines, frameworks), LOCATION (countries, cities, offices). Financial metrics and ratios are explicitly excluded from entity extraction.

**Section ordering:** For `EXTRACT_*` queries, `aggregation_node` sorts L1 sections by content density (most `key_metrics` / `key_risks` / commitment claims first) so the most information-rich sections load within the token budget.

**Robustness:** Items with empty, null, or placeholder names ("Unknown", "n/a") are filtered out.

Output schema per item: `{name, value, description, section, page}`

---

### 10. Contradiction Agent
**File:** `services/agents/nodes/contradiction.py`
**Model:** `meta-llama/llama-4-scout-17b-16e-instruct` via Groq

**Two-stage process:**
1. **SQL query** to DuckDB via `duckdb_write.query_contradictions()`: finds `(claim_a, claim_b)` pairs where
   `a.doc_id = b.doc_id AND a.subject = b.subject AND a.value != b.value`.
   Subject fields are normalized to lowercase for matching.
2. **Topic-relevance sort:** Before the top-20 cap, candidates are re-sorted by overlap between the candidate's `subject` words and meaningful words in the user's question (stopwords excluded).
3. **LLM classification**: for each candidate pair in the top 20, determines:
   - Is this a genuine contradiction (same metric, different value)?
   - Or an explainable difference (different sections, different methodologies)?
   - Severity: high / medium / low.

Uses `duckdb.connect(read_only=True)` for concurrent query-time safety.

**JSON robustness:** Uses `_parse_json_response()` with markdown-fence stripping and brace-extraction fallback. Parse failures return an empty contradictions list rather than surfacing raw model output.

---

### 11. Query Node (Full Passthrough)
**File:** `services/agents/nodes/query.py`
**Model:** None — no LLM call made

**Always the last node.** Passes `_analysis_result` directly as `state["answer"]` for all query types. No synthesis LLM call is made for any query type — each branch node produces final-quality output directly using Groq llama-4-scout.

```python
_PASSTHROUGH_TYPES = {
    QueryType.EXTRACT_ENTITIES,
    QueryType.EXTRACT_RISKS,
    QueryType.EXTRACT_DECISIONS,
    QueryType.DETECT_CONTRADICTIONS,
    QueryType.SUMMARIZE_FULL,
    QueryType.SUMMARIZE_SECTION,
    QueryType.CROSS_SECTION_COMPARE,
}
```

---

## LangGraph State

```python
class AgentState(TypedDict):
    query: QueryRequest
    doc: DocumentRecord
    context_used: list[str]      # ["L3:global", "L2:cluster_0", "L1:<section_id>", ...]
    token_budget_used: int
    token_budget_limit: int      # 22_000
    answer: str
    sources: list[dict]
    contradictions: list[ContradictionReport]
    route: str                   # internal routing decision
```

Private state keys (not in TypedDict but used at runtime):
- `_global_context`: L3 digest text
- `_cluster_context`: concatenated L2 digests
- `_section_context`: concatenated L1 summaries for relevant sections
- `_analysis_result`: final answer from branch node (passed through by query_node)

---

## Ingestion Pipeline

The ingestion pipeline (`services/workers/flows/ingest_document.py`) is plain async Python —
no external orchestrator required. The `ingest_document_flow` function runs the full
OCR → Segment → L1 → DuckDB → L2 → L3 sequence in a single async call, triggered
as a background task by FastAPI on document upload.

**All ingestion LLM calls use Cerebras only.** Groq is never called during ingestion.
