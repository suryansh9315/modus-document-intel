# Modus Document Intelligence

Multi-agent AI system for processing large scanned documents (~500+ pages). Demonstrates hierarchical compression, cross-section reasoning, and contradiction detection without RAG or vector databases.

The provided sample document is the ICICI Bank Annual Report (341 pages, ~248K tokens), but the system works with any large scanned or text-native PDF.

## Architecture

```
PDF → OCR → Segment → L1 Analysis → L2 Cluster → L3 Global
                                ↓                    ↓
                             DuckDB              MongoDB
                                ↓                    ↓
User Query → LangGraph → Context Budget → Cerebras LLM → SSE → Browser
```

**Two phases:**
1. **Offline ingestion**: Async pipeline runs OCR + segmentation + hierarchical summarization
2. **Online query** (<10s): LangGraph assembles pre-computed context and routes to specialized agents

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full system diagram.

## Quick Start

### Prerequisites

- Docker + Docker Compose
- A Cerebras API key (free at [cloud.cerebras.ai](https://cloud.cerebras.ai))

### 1. Configure environment

```bash
cd modus-doc-intel
cp .env.example .env
# Edit .env and add your CEREBRAS_API_KEY
```

### 2. Start infrastructure

```bash
docker compose -f infra/docker-compose.yml up -d
```

This starts: MongoDB, Redis, FastAPI (port 8000), and Next.js (port 3000).

### 3. Upload a document

Upload any PDF via the web UI at [http://localhost:3000](http://localhost:3000), or use the seed script for the provided sample:

```bash
# Upload the ICICI Bank Annual Report (sample document)
python modus-doc-intel/scripts/seed_icici.py
```

Ingestion time scales with document size. The 341-page ICICI report takes approximately 3–5 minutes (OCR + parallel L1 summaries via llama3.1-8b on Cerebras, up to 4 concurrent). Re-uploading the same PDF skips OCR entirely via a local JSON cache.

### 4. Query the document

Visit [http://localhost:3000](http://localhost:3000) and start querying!

Or use the API directly:

```bash
curl -X POST http://localhost:8000/queries/ \
  -H "Content-Type: application/json" \
  -d '{
    "doc_id": "<your-doc-id>",
    "query_type": "SUMMARIZE_FULL",
    "question": "What are the key highlights of this document?",
    "stream": false
  }'
```

## Development Setup

### Python services (uv workspace)

```bash
# Install uv
pip install uv

# Install all packages
uv sync

# Run the API locally (with MongoDB running)
uv run uvicorn modus_api.main:app --reload
```

### Next.js frontend

```bash
cd apps/web
npm install
npm run dev
```

## Query Types

| Query Type | Description | Example |
|---|---|---|
| `SUMMARIZE_FULL` | Full document summary | "Summarize the entire document" |
| `SUMMARIZE_SECTION` | Specific section summary | "Summarize the Risk Management section" |
| `CROSS_SECTION_COMPARE` | Compare two sections | "Compare capital adequacy across two sections" |
| `EXTRACT_ENTITIES` | Extract named entities with values | "List all subsidiaries" |
| `EXTRACT_RISKS` | Extract risk factors | "What are the top risks?" |
| `EXTRACT_DECISIONS` | Extract strategic decisions | "What commitments were made?" |
| `DETECT_CONTRADICTIONS` | Find inconsistencies | "Are there any contradictions in the reported figures?" |

## Evaluation

```bash
# After seeding the ICICI report, run the 20-question golden evaluation
python scripts/eval.py <doc-id> http://localhost:8000
```

Results saved to `eval/results_<timestamp>.json`.

Baseline accuracy on the 20-question golden set: **6/20 (30%)**. After Phase 1 query-time improvements, target is **≥14/20 (70%)**. Phase 2 re-ingestion improvements push toward 80%+.

## Project Structure

```
modus-doc-intel/
├── packages/
│   ├── schemas/          # Shared Pydantic models
│   └── prompts/          # Jinja2 prompt templates
├── services/
│   ├── workers/          # Ingestion pipeline (OCR → L1 → L2 → L3)
│   └── agents/           # LangGraph query agents
├── apps/
│   ├── api/              # FastAPI gateway
│   └── web/              # Next.js 15 frontend
├── infra/                # Docker + docker-compose
├── scripts/              # Seed + eval scripts
├── eval/                 # Golden Q&A pairs (ICICI Bank sample)
└── docs/                 # Architecture docs
```

## Models Used

- **gpt-oss-120b** (via Cerebras): L2/L3 aggregation, contradiction analysis, query synthesis — 128K context
- **llama3.1-8b** (via Cerebras): L1 per-section summaries, structured extraction (JSON mode) — fast, low-cost
- **docTR** (local): OCR for scanned/image-only pages
- **pdfplumber** (local): Text extraction for text-native pages

## Context Strategy

A 341-page document (~248K tokens) is compressed into a hierarchical tree:
- **L1**: ~300 tokens per section (150–200 word summary + key_metrics + claims). Sections larger than 8K chars are split into overlapping chunks — one LLM call per chunk, results merged — so no content is silently truncated.
- **L2**: ~4K tokens per cluster (5–7 sections synthesized) + `consolidated_metrics` dict
- **L3**: ~3K tokens for the whole document (global digest) + `executive_summary` + `top_metrics` + `top_risks`

At query time, the aggregation node loads the right context levels within a **120K token budget**. Context assembly is query-type-aware:
- `EXTRACT_*` queries load sections sorted by content density (most metric-rich first) and seed the LLM with pre-extracted DuckDB claims.
- `SUMMARIZE_SECTION` loads up to 4 neighboring sections within ±20 pages of the requested section.
- `DETECT_CONTRADICTIONS` sorts candidates by question-keyword relevance before the top-20 cap.

See [docs/CONTEXT_STRATEGY.md](docs/CONTEXT_STRATEGY.md) for details.
