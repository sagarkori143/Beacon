# Document versioning

Uploading a revision of a live policy must be safe. Concretely: while v4 is being
parsed, OCR'd, chunked and embedded — which can take minutes — v3 answers every
query, unchanged. v4 becomes visible in one instant, or not at all.

---

## States

```
PROCESSING ──► ACTIVE ──► INACTIVE ──► ACTIVE   (rollback)
     │
     └──────► FAILED
```

At most one `ACTIVE` version per document, enforced by the database:

```sql
CREATE UNIQUE INDEX uq_document_versions_one_active
    ON document_versions (document_id) WHERE status = 'ACTIVE';
```

Not by application code. Application code has bugs; a unique index does not.

Superseded versions and their chunks are kept for rollback and audit. Retrieval
filters to active chunks, so they cost storage and nothing else.

---

## The four mechanisms

Activation is correct because of four things working together. None is
sufficient alone.

### 1. Chunks are written invisible

```sql
INSERT INTO chunks (..., is_active) VALUES (..., FALSE)
```

Visibility is a column, not the absence of rows. A worker that dies at 80% of
EMBEDDING leaves rows behind — and no search can see them, **by construction**.
There is no cleanup path that can be forgotten, no orphan sweeper to schedule,
and no window in which a half-indexed document answers a question.

### 2. The parent document row is locked

```sql
SELECT id FROM documents WHERE id = :document_id FOR UPDATE;
```

Two concurrent activations of the same document serialize here. One memorable
rule — always take the document lock first — removes a whole class of deadlock.

### 3. Demote, then promote

```sql
UPDATE document_versions SET status='INACTIVE', deactivated_at=now() WHERE id = :previous;
UPDATE chunks            SET is_active = FALSE                        WHERE document_version_id = :previous;

UPDATE document_versions SET status='ACTIVE', activated_at=now()
 WHERE id = :new AND status = ANY(ARRAY['PROCESSING','INACTIVE']);
UPDATE chunks            SET is_active = TRUE WHERE document_version_id = :new;
```

The order is forced by the partial unique index, which is not deferrable: the old
version must leave `ACTIVE` before the new one enters it.

All of it is one transaction. The version flip and its chunks becoming visible
happen at the same instant, so no query can observe a version that is active but
has no searchable content.

### 4. The promote is conditional

`status = ANY(ARRAY['PROCESSING','INACTIVE'])` does two jobs:

- **Idempotency.** A redelivered queue message matches zero rows, notices, and
  verifies instead of corrupting. Queue delivery is at-least-once, so this is a
  normal occurrence rather than an edge case.
- **Rollback.** Re-activating a superseded version needs no separate code path,
  because `INACTIVE` is already an accepted source state. Rollback is therefore
  exactly as well-tested as a forward activation.

---

## Validation gates

Gates run **inside the activation transaction**, holding the document lock. That
placement is the point: a version that cannot be retrieved can never replace one
that could, and there is no window between "validated" and "activated" in which
anything could change.

Ordered cheapest-first, stopping at the first failure:

| Gate | Catches |
|---|---|
| `chunk_count` | A failed parse or empty OCR result. **The most important one** — activating it would replace a working policy with one that answers nothing. |
| `content` | Empty chunks. |
| `embeddings` | Missing vectors, or chunks spanning two embedding spaces. |
| `lexical_index` | Missing tsvectors — hybrid search would silently become vector-only. |
| `tenant_isolation` | Any chunk claiming a foreign organization. Should be structurally impossible; checked anyway, because this is the one number whose being wrong means a leak. |
| `scope_consistency` | Chunks whose location scope disagrees with their version's. |
| `smoke_retrieval` | A tsvector built with the wrong text-search configuration, or an index that exists but matches nothing. The counting gates pass happily in both cases. |

The smoke gate goes through `SearchProvider.search()` rather than its own SQL, so
it tests the code that will actually serve queries. It is lexical only
(`embedding=None`), so a model server that went down mid-pipeline cannot block
activation of work that already succeeded.

A failure raises `ValidationGateFailed`, the job records the reason, the version
becomes `FAILED` — and **the active version is never touched**.

---

## What an operator sees

```bash
curl -H "authorization: Bearer $TOKEN" \
  localhost:8000/api/v1/ingestion/jobs/$JOB_ID/events
```

```json
[
  {"stage": "PARSING",    "status": "COMPLETED", "duration_ms": 412.8,
   "detail": {"pages": 12, "layout_lines": 384, "repeating_lines": 2}},
  {"stage": "OCR",        "status": "COMPLETED", "duration_ms": 8.1,
   "detail": {"mode": "NATIVE", "coverage": 1.0, "ocr_page_count": 0,
              "reasons": ["native_text (coverage=1.00)"]}},
  {"stage": "VALIDATING", "status": "FAILED",    "duration_ms": 31.2,
   "message": "chunk_count: 0 chunks (minimum 1)",
   "detail": {"passed": false, "gates": [{"name": "chunk_count", "passed": false}]}}
]
```

The OCR decision is recorded on **every** document, not only failures — so "why
did this take four minutes?" is one request, not an investigation.

---

## Rollback

```bash
POST /api/v1/documents/versions/{version_id}/activate
```

Runs the identical transaction as a forward activation, including the gates. A
version that has since become unretrievable will not activate.

---

## What is tested

[`tests/integration/test_versioning.py`](../tests/integration/test_versioning.py):

- a first version activates; a same-titled upload becomes v2, not a second document;
- activation demotes the previous version, and only the active one is retrievable;
- **a second `ACTIVE` row is rejected by the database**, not by application code;
- chunks are invisible before activation;
- **a version failing validation leaves the previous one active, retrievable, and
  answering with its own content** — the guarantee the spec calls critical;
- the failure reason lands on the job;
- rollback restores the earlier content;
- re-activating an already-active version is a no-op, not a corruption.
