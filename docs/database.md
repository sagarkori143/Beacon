# Database design

PostgreSQL 16 with pgvector. One database holds relational data, vectors and the
lexical index — which keeps a document, its chunks and their embeddings inside a
single transaction, and is the reason activation can be atomic at all.

---

## Tables

```
organizations ──┬── locations ──┬── users
                │               │
                ├── documents ──┴── document_versions ── chunks ── embedding_spaces
                │                          │
                │                          └── ingestion_jobs ── ingestion_job_events
                │
                ├── conversations ── conversation_messages
                └── audit_log

user_directory   (global: email → organization, for login only)
```

### Tenancy columns

`organization_id` is on every tenant table. `location_id` is nullable, and the
null is meaningful:

| `location_id` | Meaning |
|---|---|
| `NULL` | Organization-wide. Shared by every location, stored once. |
| set | Belongs to that location. Overrides organization content on its subject. |

`chunks` carries both **denormalized**, so every tenant filter is one indexed
predicate rather than a join. That matters on the hot path: the search query runs
two arms over this table on every request.

---

## `chunks`

The hardest table to change later — its column set decides whether filtered ANN,
lexical search, hierarchical override and deduplication all work.

| Column | Why |
|---|---|
| `organization_id`, `location_id` | Tenant filter and hierarchy, without a join |
| `document_id`, `document_version_id`, `document_version`, `ordinal` | Provenance and stable ordering |
| `content` | Breadcrumb-prefixed text. Embedded *and* stored — see below |
| `content_hash` | Exact-duplicate detection |
| `heading`, `section_path`, `section_key` | Citations and section lookup |
| `topic_key` | Normalized subject. Drives location override without an LLM |
| `page_from`, `page_to` | Citations that point at a page |
| `language` | Chooses the text-search configuration |
| `token_count` | Context budgeting without re-tokenizing |
| `embedding_space_id` | Vectors are only comparable within one space |
| `embedding vector(N)` | N fixed at migration time from `EMBEDDING_DIM` |
| `search_vector tsvector` | `setweight(heading,'A') || setweight(content,'B')` |
| `is_active` | Visibility. Flipped at activation — see [versioning](versioning.md) |

**Why the breadcrumb is inside `content`:** a chunk reading *"after 23:00, use the
side entrance"* is meaningless without "Check-in > Late arrival" attached. The
prefix is embedded as well as stored, so short chunks gain the context they need,
and `section_path` stays separately structured for citations.

---

## Indexes

```sql
-- Semantic arm. Partial on is_active: the graph then holds only rows a query
-- could return, which both speeds traversal and reduces how much of it the
-- tenant predicate discards. See docs/hybrid-search.md.
CREATE INDEX ix_chunks_embedding_hnsw ON chunks
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64)
    WHERE is_active;

-- Lexical arm. GIN pre-filters correctly, which makes it the recall floor.
CREATE INDEX ix_chunks_search_vector ON chunks USING gin (search_vector);

-- Hot path, in the order the planner wants it.
CREATE INDEX ix_chunks_tenant_active ON chunks (organization_id, is_active, location_id);

-- At most one active version per document. Enforced here, not in code.
CREATE UNIQUE INDEX uq_document_versions_one_active
    ON document_versions (document_id) WHERE status = 'ACTIVE';

-- At most one current embedding space.
CREATE UNIQUE INDEX uq_embedding_spaces_current
    ON embedding_spaces (is_current) WHERE is_current;
```

**HNSW over IVFFlat**: better recall at the same latency, and no training step —
IVFFlat needs representative data before its lists are meaningful, which a
freshly-provisioned tenant does not have.

**Cosine** (`vector_cosine_ops`) with vectors L2-normalized on write, so distance
reduces to a dot product and the deduplication threshold in the context builder
is a plain cosine similarity comparison.

Alembic is told to ignore the two search indexes
(`migrations/env.py::include_object`); they use options it cannot express and
would otherwise be proposed for deletion on every autogenerate run.

---

## Row Level Security

Every tenant table: `ENABLE` + `FORCE`, one policy on
`current_setting('app.current_org_id', true)`. `organizations` keys on its own
`id` and is enabled but not forced, so the owner role can still provision
tenants. `embedding_spaces` and `user_directory` are deliberately global.

Full reasoning in [tenant isolation](tenant-isolation.md).

---

## Migrations

```bash
make migrate                       # apply
make migration m="add x"           # autogenerate
make downgrade                     # roll back one
```

Migrations run through the same async driver as the application, so there is no
second PostgreSQL driver to install or keep in sync. They connect as the
**owner**; the application connects as `app_rw`, which owns nothing and cannot
bypass RLS.

`CREATE INDEX CONCURRENTLY` cannot run inside a transaction — Alembic wraps
migrations in one, so use:

```python
with op.get_context().autocommit_block():
    op.execute("CREATE INDEX CONCURRENTLY ...")
```

---

## Retention

Superseded versions and their chunks are kept for rollback and audit. They are
invisible to search (`is_active = false`), so they cost storage and nothing else.
A deployment that wants them gone should delete by age or by count of retained
versions per document — the schema does not do it automatically, because
"the previous version is still there" is precisely what makes rollback possible.

## Day-one SQLAlchemy async specifics

Three things that cost a day each if missed:

- `expire_on_commit=False` on the sessionmaker. Without it, touching an attribute
  after commit triggers a lazy refresh, which in async raises rather than
  quietly issuing a query.
- `lazy="raise"` on relationships, with explicit `selectinload` where a
  relationship is genuinely needed. Async has no implicit lazy load; without
  `raise` you get a confusing error far from the cause.
- Register pgvector's asyncpg codec per connection via a `connect` event
  listener — and write SQL that does not depend on it having succeeded
  (see [hybrid search](hybrid-search.md#the-query)).
