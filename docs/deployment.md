# Deployment

The topology is split on purpose. The backend is stateless containers next to a
database; the model server is somewhere else entirely, reached over HTTP.

```
       ┌───────────── your platform (Railway, Fly, ECS, Kubernetes) ─────────────┐
       │                                                                          │
   ────┼──►  api × N  ──┐                                                          │
       │                ├──► PostgreSQL 16 + pgvector   (managed)                  │
       │   worker × N ──┘                                                          │
       │        │       ├──► Redis 7                     (managed)                 │
       │        │       └──► object storage              (S3 / GCS / volume)       │
       └────────┼─────────────────────────────────────────────────────────────────┘
                │
                └── HTTPS over a private network ──►  your model server (Ollama/vLLM)
                                                      GPU box, on-prem, wherever
```

Nothing in the backend assumes the model is local, and nothing inspects host
hardware. `OLLAMA_BASE_URL` is the entire interface.

---

## Local

```bash
cp .env.example .env     # point OLLAMA_BASE_URL at your model server
make up                  # postgres, redis, api, worker
make seed
```

`docker-compose.yml` deliberately has **no Ollama service** in the default stack.
For a same-machine model server use `http://host.docker.internal:11434`; the
compose file adds the `host-gateway` mapping that makes that resolve.

If you do want one alongside for convenience:

```bash
docker compose --profile local-llm up -d
docker compose exec ollama ollama pull qwen2.5:7b-instruct
docker compose exec ollama ollama pull nomic-embed-text
```

Nothing in the code depends on it.

---

## Images

One `docker/Dockerfile`, two targets. The api and worker share a base and a
dependency set, so there is one build and no chance of the two drifting apart.

They differ in exactly one thing: **the worker gets Tesseract**. The worker is
the only process that parses uploaded files, untrusted input does not belong in
the process serving requests, and the API image stays ~150MB smaller for it.

```bash
docker build -f docker/Dockerfile --target api    -t agent-api:latest .
docker build -f docker/Dockerfile --target worker -t agent-worker:latest .
```

Both run as UID 10001, not root.

Extra OCR languages go in the worker stage:

```dockerfile
RUN apt-get install -y tesseract-ocr-jpn tesseract-ocr-deu
```

---

## Railway (or any container platform)

Three services from this repository, plus two managed add-ons.

**1. PostgreSQL.** Must have pgvector. On Railway, the Postgres plugin supports
`CREATE EXTENSION vector`; the first migration does it.

**2. Redis.** Any Redis 7. Set `maxmemory-policy noeviction` — the ingestion
queue lives here, and evicting a queue entry loses a document someone uploaded.

**3. api** — `uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers`
**4. worker** — `python -m app.workers.runner`, scaled independently
**5. migrate** — `alembic upgrade head`, run once per deploy before the others

### Environment

```bash
APP_ENV=production
LOG_FORMAT=json

# As the unprivileged role. A superuser silently bypasses Row Level Security;
# in production the boot guard refuses to start rather than serve without it.
DATABASE_URL=postgresql+asyncpg://app_rw:...@host:5432/agentdb
REDIS_URL=redis://host:6379/0

# Your model server, over a private network.
OLLAMA_BASE_URL=https://ollama.internal.example.com
OLLAMA_MODEL=qwen2.5:7b-instruct
OLLAMA_EMBEDDING_MODEL=nomic-embed-text
EMBEDDING_DIM=768

SECURITY__JWT_SECRET=<48+ random bytes>
SECURITY__CORS_ORIGINS=["https://app.example.com"]

STORAGE__PROVIDER=s3
STORAGE__BUCKET=...
STORAGE__REGION=...
STORAGE__ACCESS_KEY_ID=...
STORAGE__SECRET_ACCESS_KEY=...

# ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY -- all optional
```

On a platform with no writable filesystem, supply the provider manifest as
`PROVIDERS_JSON` instead of mounting `config/providers.yaml`. It takes precedence.

### The two database roles

Migrations connect as the **owner**; the application connects as `app_rw`.
`docker/postgres/init.sql` creates the second role for the Compose stack. On a
managed database, run it once by hand:

```sql
CREATE ROLE app_rw LOGIN PASSWORD '...' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
GRANT CONNECT ON DATABASE agentdb TO app_rw;
GRANT USAGE ON SCHEMA public TO app_rw;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_rw;
ALTER ROLE app_rw SET idle_in_transaction_session_timeout = '15s';
```

Why it matters: [tenant isolation](tenant-isolation.md#the-two-role-split).
`GET /health/ready` reports `application_role` so you can confirm it.

### Storage

`STORAGE__PROVIDER=local` needs a persistent volume mounted at the same path in
**both** the api and worker — the api writes the original, the worker reads it.
On a platform without shared volumes, use `s3`. The S3 provider signs requests
directly (SigV4, no boto3) and works against MinIO, R2 and B2 via
`STORAGE__ENDPOINT_URL`.

---

## The model server

**Ollama has no authentication.** Binding it to a public interface hands anyone
your GPU, and — worse — a channel into your network. Do not do it.

Pick one:

| | |
|---|---|
| **Private network** | VPC peering, or a Tailscale/WireGuard mesh with the backend. Simplest when both ends are yours. |
| **Cloudflare Tunnel** | `cloudflared` on the GPU box, Cloudflare Access in front. No inbound ports. |
| **Authenticating proxy** | nginx/Caddy with a bearer token; set `OLLAMA_API_KEY` and the provider sends it. |

Then:

```bash
OLLAMA_BASE_URL=https://ollama.internal.example.com
OLLAMA_API_KEY=<token, if behind a proxy>
```

### Moving to vLLM

vLLM serves an OpenAI-compatible API, so it is a manifest change, not code:

```yaml
llm:
  - name: local
    type: openai_compatible
    base_url: http://10.0.0.5:8000/v1
    privacy: local
    models:
      - name: meta-llama/Llama-3.1-8B-Instruct
        tier: balanced
        context_window: 131072
        supports_tools: true
```

### When it is unreachable

The API starts and serves. `/health` stays green; `/health/ready` reports the
provider as unhealthy. Uploads, search over already-indexed data, document
management and versioning all keep working. Chat returns `503` with `Retry-After`
once the circuit breaker opens.

Ingestion behaves differently and deliberately: the EMBEDDING stage is
**retryable**, so the queue redelivers rather than failing the document. An
unreachable GPU box delays ingestion; it does not reject an upload.

---

## Scaling

| | |
|---|---|
| **api** | Stateless. Add instances behind a load balancer. Gate it on `/health`, never `/health/ready`. |
| **worker** | Add consumers to the group. Watch `GET /ingestion/queue`: `pending` and `oldest_pending_idle_ms` are the numbers that matter. |
| **PostgreSQL** | Vertical first. Read replicas for search. Partition `chunks` by `organization_id` past ~2M rows per tenant. |
| **model server** | Independent. Raise `max_concurrency` in the manifest only as far as `OLLAMA_NUM_PARALLEL` on the server allows — Ollama serializes beyond it, and queued requests just consume the client timeout. |

---

## Operating

**Metrics worth alerting on**

- `oldest_pending_idle_ms` rising — workers are down or wedged
- `dead_letter_length` non-zero — documents are failing terminally
- LLM p95 latency and TTFT — the model server is under-provisioned
- `search_recall_recovered` — the ANN arm is losing recall to tenant filtering
- `grounding_ratio` falling — retrieval quality regressed

**Logs** are structured JSON, correlated by `request_id`, `trace_id` and
`job_id`. The `trace_id` on an upload follows it through the queue into the
worker, so an ingestion problem traces back to the request that started it.

Deliberately never logged: prompts, document content, retrieved passages,
credentials. Counts, ids, latencies and decisions only.

**Backups** must include object storage as well as PostgreSQL. The database has
chunks and metadata; the originals are the only copy of what was uploaded.
