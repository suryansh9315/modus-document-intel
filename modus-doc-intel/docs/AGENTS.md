# Agents: Multi-Agent System Reference

## Agent Inventory

| # | Agent | Input | Output | Model | Role |
|---|-------|-------|--------|-------|------|
| 1 | **Ingestion** | PDF path + page range | `list[PageOCR]` | docTR / pdfplumber (rules) | OCR + table extraction |
| 2 | **Segmentation** | `list[PageOCR]` | `list[SectionBoundary]` | Rule-based (regex) | Heading detection, fallback chunking |
| 3 | **Local Analysis** | Section text (~8K tok) | `SectionSummary` (L1) | Llama-3.3-70B | Per-section deep dive, claim extraction |
| 4 | **Cluster Aggregation** | `list[SectionSummary]` | `ClusterDigest` (L2) | Llama-3.3-70B | Hierarchical compression of 5-7 sections |
| 5 | **Global Aggregation** | `list[ClusterDigest]` | `GlobalDigest` (L3) | Llama-3.3-70B | Whole-document synthesis |
| 6 | **Query Router** | `QueryRequest` + `AgentState` | routing decision (str) | Rule-based | Intent classification → branch |
| 7 | **Global Reasoning** | L3 + L2 + L1 context | draft answer | Llama-3.3-70B | Full-doc synthesis for SUMMARIZE_FULL |
| 8 | **Extractor** | Claims index + schema | JSON entities/risks/decisions | Llama-3.1-8B | Structured extraction in JSON mode |
| 9 | **Contradiction** | DuckDB result + context | `list[ContradictionReport]` | Llama-3.3-70B | Conflict classification and explanation |
| 10 | **Query Synthesizer** | All upstream outputs | final answer + sources | Llama-3.3-70B | Citation-grounded answer generation |

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
This guarantees sufficient granularity for the L1/L2/L3 pipeline.

---

### 3. Local Analysis Agent (L1)
**File:** `services/workers/tasks/summarize.py`

**Process:**
1. Concatenates all pages in a section into a single text blob (max 40K chars).
2. Tables are prepended in Markdown format to ensure numbers are preserved.
3. Calls Llama-3.3-70B with JSON mode via Groq API.
4. Extracts: summary_text (~800-1200 words), key_metrics, key_entities, key_risks, claims.
5. Claims become `ExtractedClaim` objects with normalized subject for contradiction detection.

**Rate limiting:** `asyncio.Semaphore(5)` + 2s sleep between batches.

---

### 4 & 5. Aggregation Agents (L2 + L3)
**File:** `services/workers/tasks/summarize.py`

**L2:** Clusters 5-7 consecutive sections (by page proximity) into a `ClusterDigest`.
Input: all L1 summary texts + key metrics concatenated.
Output: ~4K token digest preserving cross-section themes and consolidated metrics.

**L3:** Synthesizes all cluster digests into a `GlobalDigest`.
Output: ~3K token digest + 300-word executive summary.
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

Assembles the full hierarchical context (L3 global digest + relevant L2 clusters +
relevant L1 sections) and synthesizes an answer using the `query_summarize_full.j2`
template. All citation page numbers come from the section summaries, not from raw text.

---

### 8. Extractor Agent
**File:** `services/agents/nodes/extraction.py`

**Used for:** EXTRACT_ENTITIES, EXTRACT_RISKS, EXTRACT_DECISIONS

Uses **Llama-3.1-8B** (smaller model) with JSON mode for structured, predictable output.
Saves tokens and latency compared to Llama-70B for structured extraction tasks.

Output schema per item: `{name, value, description, section, page, fiscal_year}`

---

### 9. Contradiction Agent
**File:** `services/agents/nodes/contradiction.py`

**Two-stage process:**
1. **SQL query** to DuckDB: finds `(claim_a, claim_b)` pairs where
   `a.doc_id = b.doc_id AND a.subject = b.subject AND a.value != b.value`.
   Subject fields are normalized to lowercase for matching.
2. **Llama-70B classification**: for each candidate pair, determines:
   - Is this a genuine contradiction (same metric, same period, different value)?
   - Or an explainable difference (different periods, different methodologies)?
   - Severity: high / medium / low.

Uses `duckdb.connect(read_only=True)` for concurrent query-time safety.

---

### 10. Query Synthesizer
**File:** `services/agents/nodes/query.py`

**Always the last node.** Streams the final answer using Llama-70B.
Assembles the upstream analysis result + context summary into the `query_synthesize.j2`
template, which enforces citation format (`[p.X]`) and structured markdown output.

Builds the `sources` list from the `context_used` entries in AgentState,
allowing the frontend to display which document layers were consulted.

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
