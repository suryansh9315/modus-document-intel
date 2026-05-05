# Context Strategy: Hierarchical Compression Tree

## The Core Problem

A large scanned document (500+ pages) expands to well over 100K tokens when OCR'd.
The raw document cannot fit in a single LLM call at query time.

Example: the ICICI Bank Annual Report (341 pages) ≈ **248K tokens** raw.

## Solution: Hierarchical Compression Tree

Rather than RAG (retrieve chunks), we build a **summary tree** during ingestion:

```
L0: Raw text           (~248K tokens for 341-page example)
L1: Section summaries  (~300 tokens each × 40 sections  = 12K tokens)
     key_metrics dict + claims list (always exact values, never paraphrased)
     Large sections chunked (8K char chunks, 500-char overlap, one call per chunk, merged)
L2: Cluster digests    (~4K tokens each × 7 clusters    = 28K tokens)
     + consolidated_metrics dict (LLM-curated cluster-level metrics)
L3: Global digest      (~3K tokens, 1 document)
     + executive_summary (~300 words, data-driven)
     + top_metrics dict  (LLM-curated cross-document key figures)
     + top_risks list    (LLM-curated top risk factors)
```

**Compression ratios (341-page example):**
- L0 → L1: ~25× compression per section (150–200 word summaries)
- L1 → L2: ~13× compression per cluster
- L2 → L3: ~9× compression
- **Total: ~200× compression** (248K → ~3K for L3 alone)

The compression ratios scale proportionally for larger or smaller documents.

## The Trade-Off Triangle

| Property | RAG (chunks) | This system |
|---|---|---|
| **Retention** of exact numbers | Medium (chunk may miss context) | High (explicit key_metrics dict) |
| **Cross-section reasoning** | Low (chunks are isolated) | High (L2/L3 cross-section themes) |
| **Latency** at query time | ~2–5s (embedding lookup) | ~5–30s (context assembly + Groq) |
| **Hallucination risk** | Medium (chunk may be misleading) | Low (numbers pass verbatim) |
| **Infrastructure** | Vector DB + embedding model | MongoDB + DuckDB (simpler) |
| **Freshness** | Instant re-indexing | Requires re-ingestion |

## Context Budget

At query time, the aggregation node assembles context within a **22K token budget**,
driven by Groq llama-4-scout's 30K TPM rate limit (22K input + ~8K output headroom).

| Layer | Content | Approx tokens |
|---|---|---|
| L3 global digest | digest_text + executive_summary + top_metrics + top_risks | ~1,500 |
| L2 cluster digests (up to 40% of budget = 8,800 tokens) | Cross-section themes + consolidated_metrics | ~5,000–8,800 |
| L1 section summaries (remaining budget up to 85% cap) | Per-section details (density-sorted for EXTRACT_*) | ~5,000–12,000 |
| System prompt + templates | Instructions | ~300 |
| **Total** | | **≤ 22,000** |

**Why 22K?** Groq llama-4-scout has a 30K tokens-per-minute (TPM) limit. Capping input at
~22K tokens leaves ~8K for output within a single TPM window, preventing rate-limit throttling.

## Query-Type-Aware Context Assembly

The aggregation node tailors which context is loaded based on the query type:

| Query Type | L1 section selection | Additional context |
|---|---|---|
| `SUMMARIZE_FULL` | All sections within budget (density order) | L3 top_metrics + top_risks prominently placed |
| `SUMMARIZE_SECTION` | Requested sections + up to 4 neighbors within ±20 pages | — |
| `EXTRACT_ENTITIES` | All sections, sorted by `key_metrics` count descending | DuckDB `metric` claims prepended as seed candidates |
| `EXTRACT_RISKS` | All sections, sorted by `key_risks` count descending | DuckDB `risk_factor` claims prepended as seed candidates |
| `EXTRACT_DECISIONS` | All sections, sorted by `commitment` claim count descending | DuckDB `commitment` claims prepended as seed candidates |
| `DETECT_CONTRADICTIONS` | Relevant sections (context[:3000]) | DuckDB contradiction candidates re-sorted by question-keyword relevance before top-20 cap |
| `CROSS_SECTION_COMPARE` | Explicitly requested section IDs | — |

## LLM Routing at Query Time

Each query type makes exactly **1 LLM call** — the branch node produces the final answer directly.
The query node is a full passthrough with no additional LLM call.

| Query Type | Model | Provider | Approx tokens/call |
|---|---|---|---|
| EXTRACT_* | llama-4-scout | Groq | ~22K in + 3K out |
| SUMMARIZE_FULL | llama-4-scout | Groq | ~18K in + 4K out |
| SUMMARIZE_SECTION | llama-4-scout | Groq | ~8K in + 3K out |
| CROSS_SECTION_COMPARE | llama-4-scout | Groq | ~6K in + 3K out |
| DETECT_CONTRADICTIONS | llama3.1-8b | Cerebras | ~4K in + 2K out |

## Invariants (Never Violated)

1. **Numbers pass through verbatim.** Every metric is stored in
   `key_metrics: dict[str, str]` at L1 and never paraphrased. The LLM is
   instructed: "copy numbers EXACTLY — never round, estimate, or paraphrase."

2. **Every claim has page provenance.** `ExtractedClaim.page_number` is always
   set, enabling `[p.X]` citations in every answer.

3. **Tables are serialized as Markdown, not prose.** For pages where
   `pdfplumber.extract_tables()` returns data, the table is prepended as
   Markdown before the raw text is passed to L1 analysis. This prevents the
   LLM from re-deriving table values through inaccurate prose.

4. **Token budget is enforced, not hoped for.** The aggregation node uses
   `tiktoken.get_encoding("cl100k_base")` to count tokens before assembly.
   If the budget is exhausted, lower-priority context (additional L1 sections)
   is dropped, not truncated mid-sentence.

5. **Contradiction detection uses normalized subjects.** `ExtractedClaim.subject`
   is stored in DuckDB and normalized to lowercase for matching. Candidate pairs
   share the same `doc_id` and `subject` but have differing `value` fields —
   `llama3.1-8b` then classifies whether the difference is a genuine inconsistency
   or an explainable variation (different sections, methodologies, etc.).

6. **DuckDB claims are dual-use.** The `claims` table serves both contradiction detection
   (`query_contradictions`) and extraction seeding (`get_claims_by_type`). Claim types
   `metric`, `risk_factor`, and `commitment` map directly to `EXTRACT_ENTITIES`,
   `EXTRACT_RISKS`, and `EXTRACT_DECISIONS` respectively.

7. **Structured fields on L2/L3 are always captured.** The L2 prompt returns
   `consolidated_metrics` and the L3 prompt returns `top_metrics`/`top_risks`.
   These are stored on `ClusterDigest` and `GlobalDigest` and included in query context.
   Documents ingested before these fields were added will have empty dicts/lists — re-ingestion populates them.

## Why Not RAG?

For cross-section reasoning over long documents, RAG has fundamental limitations:

1. **Contradictions span sections.** A contradiction between page 45 and page 200
   requires comparing two chunks that are unlikely to be retrieved together.

2. **Numbers require context.** A chunk containing "Net Interest Margin: 4.27%"
   is meaningless without knowing the calculation methodology and surrounding
   narrative. L1 analysis captures the full metric in `key_metrics` with its
   section context preserved.

3. **Holistic questions require full coverage.** "What are all the risks?" requires
   synthesizing many sections, not finding the single most similar chunk.

4. **No hallucination from embedding similarity.** RAG can retrieve slightly
   wrong chunks (wrong section, wrong year) that look similar to the query.
   Our system knows exactly which sections to load.

## Scalability

For documents larger than the sample:
- Add L4: group L3 digests from multiple documents for multi-document reasoning
- Use section kind (CHAPTER vs SUBSECTION) to weight context allocation
- DuckDB claims table scales to millions of claims without architecture changes
- MongoDB DocumentRecord for a 1000-page doc would be ~350KB (well under 16MB limit)

## Token Counting

We use `tiktoken.get_encoding("cl100k_base")` (GPT tokenizer) as a proxy for
Llama's tokenizer. This gives a ~5–10% overestimate vs. the actual Llama tokenizer,
which is a **safe margin** — we consume slightly less context than we account for,
never more. The budget limit is set to 22K with additional output headroom built in.
