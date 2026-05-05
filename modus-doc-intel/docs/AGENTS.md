# Agents: Multi-Agent System Reference

## Agent Inventory

| # | Agent | Input | Output | Model | Role |
|---|-------|-------|--------|-------|------|
| 1 | **Ingestion** | PDF path + page range | `list[PageOCR]` | docTR / pdfplumber (rules) | OCR + table extraction |
| 2 | **Segmentation** | `list[PageOCR]` | `list[SectionBoundary]` | Rule-based (regex) | Heading detection, fallback chunking |
| 3 | **Local Analysis** | Section text (~8K chars) | `SectionSummary` (L1) | llama3.1-8b (Cerebras) | Per-section summary + claim extraction |
| 4 | **Cluster Aggregation** | `list[SectionSummary]` | `ClusterDigest` (L2) | gpt-oss-120b (Cerebras) | Hierarchical compression of 5–7 sections |
| 5 | **Global Aggregation** | `list[ClusterDigest]` | `GlobalDigest` (L3) | gpt-oss-120b (Cerebras) | Whole-document synthesis |
| 6 | **Query Router** | `QueryRequest` + `AgentState` | routing decision (str) | Rule-based | Intent classification → branch |
| 7 | **Global Reasoning** | L3 + L2 + L1 context | draft answer | gpt-oss-120b (Cerebras) | Full-doc synthesis for SUMMARIZE_FULL |
| 8 | **Extractor** | L3+L2+L1 context + DuckDB seed claims | JSON entities/risks/decisions | llama3.1-8b (Cerebras) | Structured extraction in JSON mode |
| 9 | **Contradiction** | DuckDB candidates (relevance-sorted) + context | `list[ContradictionReport]` | llama3.1-8b (Cerebras) | Conflict classification and explanation |
| 10 | **Query Synthesizer** | All upstream outputs | final answer + sources | gpt-oss-120b (Cerebras) | Citation-grounded answer generation |

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

### 3. Local Analysis Agent (L1)
**File:** `services/workers/tasks/summarize.py`

**Process:**
1. Merges sections with fewer than 4 pages into their neighbor before processing (reduces API calls).
2. Concatenates all pages in a section into a single text blob.
3. Tables are prepended in Markdown format to ensure numbers are preserved.
4. **Chunking (P2-3):** If the full section text exceeds **8K chars**, it is split into overlapping chunks of 8K chars with a 500-char overlap. One `llama3.1-8b` call is made per chunk. Results are merged: `key_metrics` are unioned (later chunks override the same key), `key_entities`/`key_risks`/`claims` are deduplicated and concatenated. This ensures sections of any length are fully analyzed.
5. Extracts per chunk: `summary_text` (~150–200 words), `key_metrics`, `key_entities`, `key_risks`, `claims`.
6. Claims become `ExtractedClaim` objects with normalized subject for contradiction detection.

**Concurrency:** `asyncio.Semaphore(1)` with a 3-second inter-request sleep for rate-limit compliance. Sections run concurrently via `asyncio.gather`; chunks within one section run sequentially through the semaphore.

---

### 4 & 5. Aggregation Agents (L2 + L3)
**File:** `services/workers/tasks/summarize.py`

**L2:** Clusters 5–7 consecutive sections (by page proximity) into a `ClusterDigest`.
Input: all L1 summary texts + key metrics concatenated.
Output: ~4K token digest preserving cross-section themes.
Schema fields: `digest_text`, `consolidated_metrics: dict[str, str]` (P2-2 — curated metrics across the cluster, captured from the LLM's JSON output).

**L3:** Synthesizes all cluster digests into a `GlobalDigest`.
Output: ~3K token digest + 300-word executive summary.
Schema fields: `digest_text`, `executive_summary`, `top_metrics: dict[str, str]`, `top_risks: list[str]` (P2-1 — LLM-curated cross-document key figures and risk factors, captured from the LLM's JSON output rather than discarded).
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

**Used for:** SUMMARIZE_FULL queries.

Assembles **L3 + L2 context only** (no L1 sections) and synthesizes an answer using
the `query_summarize_full.j2` template. L1 sections are deliberately excluded for
SUMMARIZE_FULL — loading all 50+ section summaries at once would balloon the prompt
past practical limits, and L3+L2 already provides a complete document synthesis at
the right level of abstraction.

The L3 `global_context` block now includes:
- `digest_text` — full narrative synthesis
- `executive_summary` — ≤300-word data-driven summary
- `top_metrics` — LLM-curated key figures (populated after re-ingestion)
- `top_risks` — LLM-curated top risks (populated after re-ingestion)
- `Key Metrics (All Sections)` — union of all L1 `key_metrics` dicts, appended for SUMMARIZE_FULL queries regardless of ingestion date

The prompt instructs the model to reference the "Key Metrics (All Sections)" block with exact figures rather than paraphrasing numbers.

---

### 8. Extractor Agent
**File:** `services/agents/nodes/extraction.py`

**Used for:** EXTRACT_ENTITIES, EXTRACT_RISKS, EXTRACT_DECISIONS

Uses **llama3.1-8b** with JSON mode for fast, structured output.

**Context assembly:**

| Context level | Characters used |
|---|---|
| L3 global digest (+ executive_summary + top_metrics/top_risks) | Full — no truncation |
| L2 cluster digests (+ consolidated_metrics) | Full — no truncation |
| L1 section summaries | First 32,000 chars (was 8,000) |

**DuckDB seed claims:** Before the LLM call, pre-extracted claims are fetched from DuckDB and prepended as "PRE-EXTRACTED CANDIDATES":
- `EXTRACT_ENTITIES` → `claim_type = "metric"` claims
- `EXTRACT_RISKS` → `claim_type = "risk_factor"` claims
- `EXTRACT_DECISIONS` → `claim_type = "commitment"` claims

The LLM refines, deduplicates, and augments the candidate list rather than starting from scratch. Seed fetch failures degrade gracefully — extraction still runs.

**Section ordering:** For `EXTRACT_*` queries without explicit `section_ids`, `aggregation_node` sorts L1 sections by content density (most `key_metrics` / `key_risks` / commitment claims first) so the most information-rich sections load within the token budget.

**Robustness:** Items with empty, null, or placeholder names ("Unknown", "n/a") are filtered out. If the result is empty after filtering, one retry is made with an explicit instruction to return at least 5 items using the pre-extracted candidates as seed.

Output schema per item: `{name, value, description, section, page}`

The structured result flows into `_analysis_result` and `_extracted_items` in
`AgentState`, then gets formatted and cited by the final `query_node`.

---

### 9. Contradiction Agent
**File:** `services/agents/nodes/contradiction.py`

**Two-stage process:**
1. **SQL query** to DuckDB via `duckdb_write.query_contradictions()`: finds `(claim_a, claim_b)` pairs where
   `a.doc_id = b.doc_id AND a.subject = b.subject AND a.value != b.value`.
   Subject fields are normalized to lowercase for matching.
2. **Topic-relevance sort:** Before the top-20 cap, candidates are re-sorted by overlap between the candidate's `subject` words and meaningful words in the user's question (stopwords excluded). This ensures that alphabetically-late subjects (e.g. "NPA ratio") are not excluded by the slice when the question is specifically about them.
3. **Llama-70B classification**: for each candidate pair in the top 20, determines:
   - Is this a genuine contradiction (same metric, different value)?
   - Or an explainable difference (different sections, different methodologies)?
   - Severity: high / medium / low.

Uses `duckdb.connect(read_only=True)` for concurrent query-time safety.

---

### 10. Query Synthesizer
**File:** `services/agents/nodes/query.py`

**Always the last node.** Streams the final answer using gpt-oss-120b.
Assembles the upstream analysis result + context summary into the `query_synthesize.j2`
template, which enforces citation format (`[p.N]`) and structured markdown output.

Extraction and contradiction query types bypass the synthesis LLM call entirely —
their branch nodes produce complete formatted output, so `query_node` passes through
`_analysis_result` directly to avoid a redundant PRIMARY_MODEL request.

---

## LangGraph State

```python
class AgentState(TypedDict):
    query: QueryRequest
    doc: DocumentRecord
    context_used: list[str]      # ["L3:global", "L2:cluster_0", "L1:<section_id>", ...]
    token_budget_used: int
    token_budget_limit: int      # 120_000
    answer: str
    sources: list[dict]
    contradictions: list[ContradictionReport]
    route: str                   # internal routing decision
```

Private state keys (not in TypedDict but used at runtime):
- `_global_context`: L3 digest text
- `_cluster_context`: concatenated L2 digests
- `_section_context`: concatenated L1 summaries for relevant sections
- `_analysis_result`: intermediate analysis from branch nodes

---

## Ingestion Pipeline

The ingestion pipeline (`services/workers/flows/ingest_document.py`) is plain async Python —
no external orchestrator required. The `ingest_document_flow` function runs the full
OCR → Segment → L1 → DuckDB → L2 → L3 sequence in a single async call, triggered
as a background task by FastAPI on document upload.

The `PREFECT_API_URL` and `PREFECT_SERVER_ALLOW_EPHEMERAL_MODE` environment variables
are present for optional future Prefect integration but are not used by the current pipeline.
