# Architecture

Two flows matter: a question becoming an answer, and a file becoming knowledge.
Everything else is plumbing around those.

---

## Answering a question

```
POST /api/v1/chat
  │
  ├─ 1. Authenticate ────────► Principal (org, location, role) from the JWT
  │                            Tenant scope comes from here and nowhere else.
  │
  ├─ 2. Admit ──────────────► rate limit, input caps, injection screen
  │
  ├─ 3. Plan ───────────────► one cheap structured call:
  │                            intent · needs_retrieval · search_queries · tools
  │                            (validated against a Pydantic schema before use)
  │
  ├─ 4. Retrieve ───────────► for each level of the hierarchy, most specific first
  │        ├── location-scoped: hybrid search (vector + lexical, fused)
  │        └── org-scoped:      hybrid search (vector + lexical, fused)
  │                            then merge: boost location, suppress overridden
  │
  ├─ 5. Build context ──────► dedupe · token budget · label scope · cite sources
  │
  ├─ 6. Route ──────────────► pick provider+model from declared capabilities
  │
  ├─ 7. Tool loop ──────────► bounded by iterations, total calls, and wall clock
  │
  └─ 8. Generate ───────────► stream tokens, extract citations, measure grounding
```

Steps 4–8 each open their own short database transaction and close it before the
next provider call. That discipline is what keeps the connection pool healthy;
see [tradeoffs](tradeoffs.md#holding-a-transaction-across-a-model-call).

### Where the guarantees live

| Guarantee | Enforced by |
|---|---|
| A tenant cannot read another's data | PostgreSQL RLS + an SQL predicate, under a non-superuser role |
| Location knowledge overrides group knowledge | Topic-key suppression in the merge, plus scope labels in the prompt |
| The answer is attributable | Citations carried per passage, validated against what was supplied |
| Nothing is invented when sources are missing | Empty context is stated explicitly to the model |

---

## Ingesting a document

```
POST /api/v1/documents          (returns immediately)
  │
  ├─ validate: magic bytes, size, MIME    ← uploads are untrusted input
  ├─ create document or next version      ← same title = new version
  ├─ store the original                   ← StorageProvider
  ├─ create the job                       ← status QUEUED
  └─ enqueue                              ← Redis Streams, tenant in the payload

Worker (any instance)
  │
  ├─ reclaim abandoned work first, then take new work
  │
  ├─ PARSING     PyMuPDF: text + layout (font sizes, boldness, gaps, repetition)
  ├─ OCR         assess the text layer → NATIVE | HYBRID | OCR
  │              only the pages that need it are rendered and recognized
  ├─ CLEANING    normalize; persist, so re-chunking never re-parses or re-OCRs
  ├─ CHUNKING    heading tree → section-aware chunks with breadcrumbs
  ├─ EMBEDDING   batched, bounded concurrency, retryable (it is a network hop)
  ├─ INDEXING    write chunks **inactive**, with vector + tsvector
  ├─ VALIDATING  seven gates, cheapest first
  └─ ACTIVATING  lock the document · demote · promote · make chunks visible
```

Every stage is idempotent, because queue delivery is at-least-once. Nothing is
visible to search until ACTIVATING, because visibility is a column rather than
the absence of rows — so a worker that dies at 80% of EMBEDDING leaves nothing
behind for anyone to clean up.

---

## Layers

```
app/api          HTTP only. Translates domain errors to RFC 9457 responses.
app/services     Domain logic. Depends on provider *interfaces*, never on an
                 implementation. Takes a UnitOfWork, never a session.
app/repositories The only place SQL lives. Takes a session, never holds one.
app/providers    Interchangeable infrastructure, built from configuration.
app/tools        Agent capabilities, each with a Pydantic input schema.
app/workers      The ingestion worker. Same image as the API.
```

Two boundaries are enforced by a [test that reads the source](../tests/unit/test_architecture.py)
rather than by review:

- no module under `services/` or `tools/` may import a concrete provider;
- no `AsyncSession` may appear in an agent or provider signature.

Both would decay silently otherwise, and the consequences — a provider that is
no longer swappable, a pool exhausted under load — take a long time to trace
back to the commit that caused them.

---

## Scaling

| Component | Scales by | Shared state |
|---|---|---|
| API | Adding stateless instances behind a load balancer | none |
| Worker | Adding consumers to the queue's consumer group | none |
| PostgreSQL | Read replicas; partition `chunks` by organization past ~2M rows/tenant | — |
| Redis | The queue and conversation state; a single instance goes a long way | — |
| Model server | Independent of everything above; reached over HTTP | — |

The API and worker are the same image with different entrypoints, so there is
one build and no chance of the two drifting apart.
