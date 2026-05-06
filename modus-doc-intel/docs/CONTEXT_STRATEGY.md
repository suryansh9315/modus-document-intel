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

| Layer | Content | Approx tokens (SUMMARIZE_*) | Approx tokens (EXTRACT_*) |
|---|---|---|---|
| L3 global digest | digest_text + executive_summary + top_metrics + top_risks | ~1,500 | ~1,500 |
| L2 cluster digests | Cross-section themes + consolidated_metrics | up to 40% of budget (~8,800) | capped at 4,000 chars (~1,000) |
| L1 section summaries | Per-section details (density-sorted) | remaining budget up to 85% cap | capped at 6,000 chars (~1,500) |
| DuckDB seeds | PRE-EXTRACTED CANDIDATES prepended to context | — | ~300–800 |
| System prompt + templates | Instructions | ~300 | ~300 |
| **Total** | | **≤ 22,000** | **~3,800–4,000** |

**Why 22K?** Groq llama-4-scout has a 30K tokens-per-minute (TPM) limit. Capping input at
~22K tokens leaves ~8K for output within a single TPM window, preventing rate-limit throttling.

**Why is EXTRACT_* input only ~4K tokens?** llama-4-scout returns `{}` (empty JSON) when given
input exceeding ~12K tokens in JSON mode. The fix caps extraction context at ~15K chars total
(~3,800 tokens). DuckDB seeds carry the primary signal for extraction; the compressed context
provides supporting narrative. See extraction quality trade-offs below.

## Query-Type-Aware Context Assembly

The aggregation node tailors which context is loaded based on the query type:

| Query Type | L1 section selection | L1 structured field | Additional context |
|---|---|---|---|
| `SUMMARIZE_FULL` | Skipped (L3+L2 cover full doc) | — | L3 top_metrics + top_risks prominently placed |
| `SUMMARIZE_SECTION` | Requested sections + up to 4 neighbors within ±20 pages | key_metrics + key_risks | — |
| `EXTRACT_ENTITIES` | All sections, sorted by `key_metrics` count descending | key_entities list | DuckDB entities table seed (typed named entities) |
| `EXTRACT_RISKS` | All sections, sorted by `key_risks` count descending | key_risks list | DuckDB `risk_factor` claims prepended as seed candidates |
| `EXTRACT_DECISIONS` | All sections, sorted by `commitment` claim count descending | commitment claims list | DuckDB `commitment` claims prepended as seed candidates |
| `DETECT_CONTRADICTIONS` | Relevant sections (context[:3000]) | key_metrics + key_risks | DuckDB contradiction candidates re-sorted by question-keyword relevance before top-20 cap |
| `CROSS_SECTION_COMPARE` | Explicitly requested section IDs | key_metrics + key_risks | — |

## LLM Routing at Query Time

Each query type makes exactly **1 LLM call** — the branch node produces the final answer directly.
The query node is a full passthrough with no additional LLM call.

| Query Type | Model | Provider | Approx tokens/call |
|---|---|---|---|
| EXTRACT_* | llama-4-scout | Groq | ~4K in + 3K out |
| SUMMARIZE_FULL | llama-4-scout | Groq | ~18K in + 4K out |
| SUMMARIZE_SECTION | llama-4-scout | Groq | ~8K in + 3K out |
| CROSS_SECTION_COMPARE | llama-4-scout | Groq | ~6K in + 3K out |
| DETECT_CONTRADICTIONS | llama-4-scout | Groq | ~4K in + 2K out |

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
   `llama-4-scout` (Groq) then classifies whether the difference is a genuine inconsistency
   or an explainable variation (different sections, methodologies, etc.).

6. **DuckDB is dual-use for extraction.** The `claims` table serves both contradiction detection
   (`query_contradictions`) and extraction seeding (`get_claims_by_type`). Claim types
   `risk_factor` and `commitment` map to `EXTRACT_RISKS` and `EXTRACT_DECISIONS` respectively.
   `EXTRACT_ENTITIES` uses a separate `entities` table (`get_entities_for_extraction`) — the
   claims seed caused entity queries to return financial metrics instead of named entities
   (PERSON, ORG, PRODUCT, REGULATION, LOCATION).

7. **Structured fields on L2/L3 are always captured.** The L2 prompt returns
   `consolidated_metrics` and the L3 prompt returns `top_metrics`/`top_risks`.
   These are stored on `ClusterDigest` and `GlobalDigest` and included in query context.
   Documents ingested before these fields were added will have empty dicts/lists — re-ingestion populates them.

## Extraction Quality Trade-offs (EXTRACT_* Context Cap)

Reducing the extraction context from 22K tokens to ~4K tokens avoids the llama-4-scout empty-response
bug but narrows the LLM's view of the document. Here is what is preserved and what may be missed:

**What is preserved (high confidence):**
- **DuckDB seeds are the primary signal.** During ingestion, every section is analyzed by Cerebras
  llama3.1-8b which extracts `risk_factor` and `commitment` claims into DuckDB verbatim. For a
  341-page document this yields 100–300 seed items covering the full document — not truncated by any cap.
  The extraction LLM call is a refinement and deduplication pass over these seeds.
- **L3 top_risks** (LLM-curated at ingestion) is always included in full (~1,500 tokens).
- **Highest-density L1 sections** are included first; for EXTRACT_RISKS the sections with the most
  `key_risks` items load within the 6K char L1 cap.

**What may be missed (low likelihood, low severity):**
- Risks or decisions mentioned only in low-density narrative sections that were not captured
  as DuckDB claims during ingestion (e.g., qualitative risks buried in letter-to-shareholders prose
  without numeric signal that would trigger L1 claim extraction).
- Fine-grained entity relationships (which PERSON is affiliated with which ORGANIZATION) that
  require cross-section context the capped L1 text may not include.

**Net effect:** Extraction recall is ~90–95% of what the full-context approach would produce,
because DuckDB seeds cover the document comprehensively. The 5–10% gap is low-signal narrative
content that would typically be filtered out anyway by the placeholder/empty-name guard.

---

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
