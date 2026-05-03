# Context Strategy: Hierarchical Compression Tree

## The Core Problem

The ICICI Bank Annual Report is ~341 pages ≈ **248K tokens** raw.
Llama-3.3-70B has a 128K context window.
A single LLM call cannot hold the full document.

## Solution: Hierarchical Compression Tree

Rather than RAG (retrieve chunks), we build a **lossless summary tree** during ingestion:

```
L0: Raw text (248K tokens, 341 pages)
L1: Section summaries (~1.5K tokens each × 40 sections = 60K tokens)
L2: Cluster digests (~4K tokens each × 7 clusters = 28K tokens)
L3: Global digest (~3K tokens, 1 document)
```

**Compression ratios:**
- L0 → L1: ~6× compression per section
- L1 → L2: ~3.5× compression per cluster
- L2 → L3: ~9× compression
- **Total: ~80× compression** (248K → ~3K for L3 alone)

## The Trade-Off Triangle

| Property | RAG (chunks) | This system |
|---|---|---|
| **Retention** of exact numbers | Medium (chunk may miss context) | High (explicit key_metrics dict) |
| **Cross-section reasoning** | Low (chunks are isolated) | High (L2/L3 cross-section themes) |
| **Latency** at query time | ~2-5s (embedding lookup) | ~3-8s (context assembly) |
| **Hallucination risk** | Medium (chunk may be misleading) | Low (numbers pass verbatim) |
| **Infrastructure** | Vector DB + embedding model | MongoDB + DuckDB (simpler) |
| **Freshness** | Instant re-indexing | Requires re-ingestion |

## Context Budget (Worst Case)

At query time, the aggregation node assembles:

| Layer | Content | Tokens |
|---|---|---|
| L3 global digest | Executive summary of full doc | ~3,000 |
| L2 cluster digests (3 most relevant) | Cross-section themes | ~12,000 |
| L1 section summaries (5 sections) | Per-section details | ~7,500 |
| System prompt + templates | Instructions | ~2,000 |
| Conversation history | Prior turns | ~5,000 |
| **Total** | | **~29,500** |

**Headroom:** 120,000 − 29,500 = **90,500 tokens remaining** for answer generation.
This is well within the 128K context limit (using 120K as safety budget).

## Invariants (Never Violated)

1. **Numbers pass through verbatim.** Every financial metric is stored in
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

## Why Not RAG?

For financial cross-section reasoning, RAG has fundamental limitations:

1. **Contradictions span sections.** A contradiction between page 45 and page 200
   requires comparing two chunks that are unlikely to be retrieved together.

2. **Numbers require context.** A chunk containing "Net Interest Margin: 4.27%"
   is meaningless without knowing which fiscal year and calculation methodology.
   L1 analysis captures fiscal_year in every claim.

3. **Investor questions are holistic.** "What are all the risks?" requires
   synthesizing 20+ sections, not finding the single most similar chunk.

4. **No hallucination from embedding similarity.** RAG can retrieve slightly
   wrong chunks (wrong section, wrong year) that look similar to the query.
   Our system knows exactly which sections to load.

## Scalability

For documents > 500 pages (beyond ICICI Bank scope):
- Add L4: group L3 digests from multiple documents
- Use section kind (CHAPTER vs SUBSECTION) to weight context allocation
- DuckDB claims table scales to millions of claims without architecture changes
- MongoDB DocumentRecord for a 1000-page doc would be ~350KB (well under 16MB limit)

## Token Counting

We use `tiktoken.get_encoding("cl100k_base")` (GPT tokenizer) as a proxy for
Llama's tokenizer. This gives a ~5-10% overestimate vs. the actual Llama tokenizer,
which is a **safe margin** — we consume slightly less context than we account for,
never more. The budget limit is set to 120K (not 128K) for an additional safety layer.
