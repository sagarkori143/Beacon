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

You need Docker, and **Ollama running and reachable before you seed** — seeding
embeds the demo documents, so it fails without it. Ollama normally runs on a
separate machine (a GPU box, another server); the backend only ever knows a URL.

```bash
# 1. On your model server — both models, and leave it running
ollama pull qwen2.5:7b-instruct      # or whatever you prefer
ollama pull nomic-embed-text

# 2. Here
cp .env.example .env
#    Ollama on this machine?  OLLAMA_BASE_URL=http://host.docker.internal:11434  (default)
#    Ollama elsewhere?        OLLAMA_BASE_URL=http://<host>:11434

docker compose up -d --build         # postgres, redis, api, worker + migrations
docker compose exec -T -e DATABASE_URL="postgresql+asyncpg://app:app@postgres:5432/agentdb" \
    api python -m scripts.seed_demo  # the Sagar Hotels demo tenant
```

With GNU Make installed those last two are `make up` and `make seed`; `make help`
lists the rest. Everything works without Make — the Makefile is a convenience,
not a dependency.

Seeding runs **inside** the api container and as the database owner, because the
`postgres` hostname only resolves on the compose network, and creating a tenant
is deliberately outside what the application role can do
([why](docs/tenant-isolation.md#2-row-level-security)).

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

### The website

```bash
make web-install     # once
make web             # http://localhost:3000
```

Three surfaces, and only two of them need a password:

| | | |
|---|---|---|
| `/` | **anyone** | every published company; open one and ask it questions |
| `/admin` | organization administrator | upload knowledge and watch it process, manage branches and people |
| `/owner` | platform operator | create organizations and their first administrator |

**Customers do not sign in.** The landing page lists every organization, and a
visitor picks one and asks. Answers cite the document and page they came from.
Each request reaches exactly one organization, so asking Sagar Hotels about
breakfast cannot surface anything Aurora Clinics uploaded.

Tokens for the two consoles live in httpOnly cookies set by Next route handlers
that call the API server-side, so no credential is readable by page JavaScript
and the browser only ever talks to `localhost:3000`.

The API is also usable directly at `http://localhost:8000/docs`.

### Your own account, and creating real tenants

`seed_demo` makes a demo tenant. For your own, create a **platform operator** —
an account that belongs to no organization and exists to provision them:

```bash
docker compose exec \
  -e DATABASE_URL="postgresql+asyncpg://app:app@postgres:5432/agentdb" \
  api python -m scripts.create_owner --email you@example.com
```

It prints a generated password once (`--password` sets your own). Then:

```bash
OWNER=$(curl -s localhost:8000/api/v1/platform/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"you@example.com","password":"<printed>"}' \
  | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

# An organization, with its first admin. The admin password is returned ONCE.
curl -s -X POST localhost:8000/api/v1/platform/organizations \
  -H "authorization: Bearer $OWNER" -H 'content-type: application/json' \
  -d '{"name":"Northwind Dental","admin_email":"admin@northwind.example"}'

# A customer in it. role=USER, optionally pinned to a location.
curl -s -X POST localhost:8000/api/v1/platform/organizations/<org-id>/users \
  -H "authorization: Bearer $OWNER" -H 'content-type: application/json' \
  -d '{"email":"guest@northwind.example","password":"guest-password-123","role":"USER"}'
```

That admin then logs in at the ordinary `/auth/login` and manages their own
organization; the customer logs in there too and can only search and chat.

**The operator token is not a master key.** It provisions tenants; it cannot read
anyone's documents, search or conversations, and every tenant endpoint rejects
it. That is deliberate — it keeps "no credential can see two organizations' data"
true without exceptions. Full reference: [docs/api.md](docs/api.md#platform-operators)
and [docs/tenant-isolation.md](docs/tenant-isolation.md#platform-operators).

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
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"   # .venv/bin on unix
docker compose up -d postgres redis                             # services only

# Migrations and seeding connect as the owner; the app itself uses app_rw.
export DATABASE_URL="postgresql+asyncpg://app:app@localhost:5432/agentdb"
.venv/Scripts/python -m alembic upgrade head
.venv/Scripts/python -m scripts.seed_demo
unset DATABASE_URL

.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
.venv/Scripts/python -m app.workers.runner                      # another terminal

.venv/Scripts/python -m pytest tests/unit    # no services needed
.venv/Scripts/python -m pytest tests         # everything
```

With Make: `make install`, `make services`, `make migrate`, `make seed-local`,
`make api`, `make worker`, `make test`, `make test-all`, `make check`.

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
