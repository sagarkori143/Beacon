# Enterprise AI Agent & Runtime

A multi-tenant RAG and agent backend. Organizations store knowledge once at the
group level; individual locations store only what differs, and override the
group on the subjects they cover. Every LLM, embedding model, OCR engine,
storage backend and queue is a swappable provider chosen by configuration.

```
Sagar Hotels                     "What time is breakfast?"
  ├── Ginza    → 7:00 – 11:00     asked at Ginza    → 11:00  (location override)
  ├── Chiyoda  → 7:00 – 10:00     asked at Chiyoda  → 10:00  (group default)
  └── Meguro   → 7:00 – 10:00     one stored document, three correct answers
```

Chiyoda's answer comes from the *same* group document Ginza overrode. Nothing is
duplicated per location, and no location can reach another's knowledge.

---

## Quick start

You need Docker, and a model server reachable over HTTP. **Ollama normally runs
on a separate machine** — a GPU box, or another server. The backend only ever
knows a URL.

```bash
# 1. On your model server
ollama pull qwen2.5:7b-instruct      # or whatever you prefer
ollama pull nomic-embed-text

# 2. Here
cp .env.example .env                  # set OLLAMA_BASE_URL to your model server
make up                               # postgres, redis, api, worker
make seed                             # the Sagar Hotels demo tenant
```

Then open http://localhost:8000/docs, or:

```bash
TOKEN=$(curl -s localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"ginza@sagarhotels.example","password":"demo-password-12345"}' \
  | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

curl -s localhost:8000/api/v1/chat -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"message":"What time is breakfast?","include_trace":true}'
```

Ask the same question as `chiyoda@sagarhotels.example` and you get 10:00, from
the group document, with no trace of Ginza.

**No model server?** Everything except answer generation still works — upload,
processing, search, versioning. `GET /health` stays green; `GET /health/ready`
tells you what is degraded.

---

## What it does

| | |
|---|---|
| **Multi-tenancy** | Organization → location. Tenant scope comes from the JWT, never a request parameter. Enforced by PostgreSQL Row Level Security under a non-superuser role. |
| **Hierarchical knowledge** | Group knowledge stored once. Location documents override it by subject; unrelated group knowledge is preserved. |
| **Ingestion** | Async pipeline: parse → OCR (only when needed) → clean → chunk → embed → index → validate → activate. Progress and per-stage timings queryable. |
| **Versioning** | Upload v4 while v3 serves every query. v4 goes live only after it passes validation, in one atomic transaction. If it fails, v3 never moved. |
| **Retrieval** | pgvector (HNSW, cosine) + PostgreSQL full-text search in one SQL round trip, fused with Reciprocal Rank Fusion. |
| **Agent** | Plan → retrieve → route a model → bounded tool loop → build context → answer, with citations and a replayable trace. |
| **Providers** | Ollama, Anthropic, OpenAI, Gemini, and any OpenAI-compatible endpoint (vLLM, Groq, Together, OpenRouter, DeepSeek, Mistral, LM Studio). Adding one is a file plus a config entry. |
| **Streaming** | SSE with semantic events — `stage`, `search`, `conflict`, `tool_call`, `citation`, `token`. |

---

## Architecture

```
                                          ┌─ registry-built providers ─┐
 Client ── FastAPI (stateless, N)         │ ollama │ anthropic │ openai│
             ├── Agent ── ModelRouter ────┤ gemini │ openai_compatible │
             │      │   (routes on declared└────────────┬───────────────┘
             │      │    capability & cost)             │ HTTP
             │      ├── Retrieval ── hybrid search ── pgvector + FTS
             │      └── ToolRegistry
             └── Ingestion API ── Redis Streams ── Worker × N
                            │                        │
   PARSING → OCR → CLEANING → CHUNKING → EMBEDDING ──┘
                            → INDEXING → VALIDATING → ACTIVATING
```

Nothing lives in an API process. Workers are stateless and scale independently.
No model weights are in the backend images.

```
app/
  api/          HTTP layer: routers, dependencies, error mapping, middleware
  core/         config, db + tenancy, security, logging, tracing, errors
  models/       SQLAlchemy ORM
  repositories/ the only place SQL lives
  services/     domain logic: auth, documents, ingestion, retrieval, rag, agent
  providers/    llm, embeddings, vector_store, search, ocr, storage, queue
  tools/        agent tools, each with a Pydantic input schema
  workers/      the ingestion worker
```

`services/` and `tools/` may import a provider's `base.py` but never a concrete
implementation — [enforced by a test](tests/unit/test_architecture.py), not by
convention.

---

## Documentation

| | |
|---|---|
| [Architecture](docs/architecture.md) | How a request and a document flow through the system |
| [Tenant isolation](docs/tenant-isolation.md) | RLS, the two-role split, and the one deliberate exception |
| [Hybrid search](docs/hybrid-search.md) | Both arms, fusion, and the filtered-ANN recall problem |
| [Document versioning](docs/versioning.md) | Why activation is atomic, and the four mechanisms that make it so |
| [Providers](docs/providers.md) | Adding a vendor; the embedding-space migration |
| [Database](docs/database.md) | Schema, indexes, and why each one exists |
| [API](docs/api.md) | Endpoints with real requests and responses |
| [Deployment](docs/deployment.md) | Railway/containers, and reaching a private model server |
| [Tradeoffs](docs/tradeoffs.md) | What was chosen, what was rejected, and what is deliberately unfinished |

---

## Development

```bash
make install          # venv + dependencies
make services         # postgres + redis only
make migrate seed     # schema + demo data
make api              # uvicorn with reload
make worker           # in another terminal

make test             # unit tests, no services needed
make test-all         # everything (needs postgres + redis)
make check            # lint + type-check + unit tests
```

Tests never require a model server: the fake LLM and embedding providers
implement the same interfaces, so the code under test takes the production path.

The suite includes the two guarantees the specification calls critical:

- `tests/integration/test_retrieval.py` — one tenant can never retrieve
  another's data, across organizations *and* across locations.
- `tests/integration/test_versioning.py` — the previous version keeps serving
  until the new one passes validation.

---

## Configuration

Everything is in [`.env.example`](.env.example) and
[`config/providers.yaml`](config/providers.yaml). Secrets only ever come from the
environment; the provider manifest references them as `${VAR}` and is safe to
commit.

The settings worth knowing about:

| | |
|---|---|
| `OLLAMA_BASE_URL` | Your model server. The backend never inspects local hardware or picks a model for you. |
| `EMBEDDING_DIM` | Pinned into the vector column at migration time. Changing it is a [documented procedure](docs/providers.md#changing-the-embedding-model), not an env edit — a boot guard refuses to start on a mismatch. |
| `DATABASE_URL` | Must be the unprivileged `app_rw` role. A superuser bypasses Row Level Security silently; a boot guard warns, and refuses in production. |
| `RETRIEVAL__FUSION` | `rrf` (default) or `weighted`. |
