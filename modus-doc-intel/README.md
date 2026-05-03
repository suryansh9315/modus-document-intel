# Modus Document Intelligence

Multi-agent AI system for processing large financial PDFs. Built for the ICICI Bank Annual Report (341 pages, ~248K tokens) — demonstrating hierarchical compression, cross-section reasoning, and contradiction detection without RAG or vector databases.

## Architecture

```
PDF → OCR → Segment → L1 Analysis → L2 Cluster → L3 Global
                                ↓                    ↓
                             DuckDB              MongoDB
                                ↓                    ↓
User Query → LangGraph → Context Budget → Groq LLM → SSE → Browser
```

**Two phases:**
1. **Offline ingestion** (~30-45 min): Prefect orchestrates OCR + segmentation + hierarchical summarization
2. **Online query** (<10s): LangGraph assembles pre-computed context and routes to specialized agents

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full system diagram.

## Quick Start

### Prerequisites

- Docker + Docker Compose
- A Groq API key (free at [console.groq.com](https://console.groq.com))

### 1. Configure environment

```bash
cd modus-doc-intel
cp .env.example .env
# Edit .env and add your GROQ_API_KEY
```

### 2. Start infrastructure

```bash
docker compose -f infra/docker-compose.yml up -d
```

This starts: MongoDB, Redis, FastAPI (port 8000), and Next.js (port 3000).

### 3. Upload and process the ICICI Bank PDF

```bash
# From the modus/ directory (where the PDF is)
python modus-doc-intel/scripts/seed_icici.py
```

This uploads the ICICI Bank Report PDF and polls ingestion status.
**Expect 30-45 minutes** for the full 341-page report.

### 4. Query the document

Visit [http://localhost:3000](http://localhost:3000) and start querying!

Or use the API directly:

```bash
# Non-streaming query
curl -X POST http://localhost:8000/queries/ \
  -H "Content-Type: application/json" \
  -d '{
    "doc_id": "<your-doc-id>",
    "query_type": "SUMMARIZE_FULL",
    "question": "What are the key financial highlights?",
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
| `SUMMARIZE_FULL` | Full document summary | "Summarize the annual report" |
| `SUMMARIZE_SECTION` | Specific section summary | "Summarize Risk Management" |
| `CROSS_SECTION_COMPARE` | Compare two sections | "Compare capital adequacy across sections" |
| `EXTRACT_ENTITIES` | Extract named entities with values | "List all subsidiaries" |
| `EXTRACT_RISKS` | Extract risk factors | "What are the top risks?" |
| `EXTRACT_DECISIONS` | Extract strategic decisions | "What commitments were made?" |
| `DETECT_CONTRADICTIONS` | Find inconsistencies | "Are there any NPA contradictions?" |

## Evaluation

```bash
# After seeding, run the 20-question golden evaluation
python scripts/eval.py <doc-id>
```

Results saved to `eval/results_<timestamp>.json`.

## Project Structure

```
modus-doc-intel/
├── packages/
│   ├── schemas/          # Shared Pydantic models
│   └── prompts/          # Jinja2 prompt templates
├── services/
│   ├── workers/          # Prefect ingestion flows
│   └── agents/           # LangGraph query agents
├── apps/
│   ├── api/              # FastAPI gateway
│   └── web/              # Next.js 15 frontend
├── infra/                # Docker + docker-compose
├── scripts/              # Seed + eval scripts
├── eval/                 # Golden Q&A pairs
└── docs/                 # Architecture docs
```

## Models Used

- **Llama-3.3-70B-Versatile** (via Groq): Primary reasoning, summarization, contradiction analysis
- **Llama-3.1-8B-Instant** (via Groq): Fast routing, structured extraction
- **docTR** (local): OCR for scanned pages
- **pdfplumber** (local): Text extraction for text-native pages

## Context Strategy

The full 341-page document (~248K tokens) is compressed ~80× into a hierarchical tree:
- **L1**: ~1.5K tokens per section
- **L2**: ~4K tokens per cluster (5-7 sections)
- **L3**: ~3K tokens for the whole document

At query time, the aggregation node loads the right context levels within a **120K token budget** — well under Llama's 128K limit.

See [docs/CONTEXT_STRATEGY.md](docs/CONTEXT_STRATEGY.md) for details.
