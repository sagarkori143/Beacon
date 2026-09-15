# Engineering tradeoffs

What was chosen, what was rejected, and what is deliberately unfinished.

---

## Decisions

### PostgreSQL for vectors, not a dedicated vector database

Qdrant or Milvus would retrieve faster at scale. They would also put the
document, its chunks and their embeddings in two systems that cannot share a
transaction — and [atomic activation](versioning.md) is the guarantee that makes
uploading a revision of a live policy safe. Splitting the store would mean
distributed-transaction machinery to preserve it, or giving it up.

pgvector handles millions of chunks comfortably. `VectorStore` and
`SearchProvider` exist as separate seams precisely so a dedicated vector database
can take over the semantic arm later while the lexical arm stays in PostgreSQL —
that is the migration the architecture is shaped for.

**Revisit when:** a single tenant exceeds a few million chunks, or p95 search
latency exceeds ~200ms after partitioning by `organization_id`.

### Reciprocal Rank Fusion by default

Weighted-sum fusion needs normalization, and normalization has a specific failure
mode: when one arm returns a tight cluster of near-identical scores — very common
for cosine similarity over a small tenant — min-max stretches meaningless
differences across the full 0–1 range and that arm dominates the ranking. RRF
consumes ranks, so it cannot do that, and it needs no special case when one arm
returns nothing.

The cost is that RRF discards score magnitude. When one arm is *confidently*
right, RRF only knows it ranked first. `WeightedScoreFusion` is one config value
away for corpora where magnitude carries signal.

### Topic-key suppression rather than an LLM conflict resolver

Asking a model "do these two passages conflict?" costs a call per candidate pair
and is unreliable in exactly the ambiguous cases that matter. Instead, chunk
headings are normalized to a subject key, and a location passage suppresses an
organization passage on the same subject.

The mechanism is coarse but the **matching is strict**: exact key match or a
strict subset, nothing looser. The failure modes are not symmetric — a missed
override leaves both passages in the context, where the scope labels and the
prompt's precedence rule handle it; a wrong suppression silently deletes a
correct answer. So it is tuned to miss rather than over-fire.

Known limits: documents whose headings do not describe their subject; conflicts
*within* a single section; two location documents disagreeing with each other.
The first is the common one, and the honest answer is that section headings are
the structure this design depends on.

### Section-aware chunking rather than fixed windows

A chunk spanning the end of the pet policy and the start of the smoking policy
answers neither question — and, worse, breaks the override logic, which keys on
subject. So chunks are built inside sections, never across a top-level boundary,
with the heading breadcrumb prefixed into the text that gets embedded.

This costs a heading detector with a dozen heuristics. Two of them carry most of
the weight: penalizing repeated lines (otherwise every page's running header
becomes an `h1` and the section tree is destroyed) and clustering near-identical
font sizes (17.9997pt and 18.0pt are one heading level, not two).

For a document with no detectable structure, the chunker degrades to paragraph
packing with sentence-boundary overlap — which is the fixed-window approach, just
arrived at rather than assumed.

### Semantic SSE events; tool turns do not stream

Streaming tool-call arguments is unreliable across vendors — some emit the whole
object at the end, some fragment it, some stop streaming entirely when tools are
present — and a half-streamed arguments blob is useless to a user anyway. You
cannot act on half a tool call.

So tool turns run non-streaming and the connection is kept meaningful with
semantic events: `stage`, `search`, `conflict`, `tool_call`, `tool_result`. The
user sees "searching", "found 8 sources", "location override applied" instead of
JSON fragments. `ToolCallDelta` stays in the event union so a vendor that streams
reliably can be adopted without a protocol change.

### Tool failures are returned to the model, not raised

Unknown tool name, invalid arguments, timeout, permission denied — all come back
as tool *results*. Models correct themselves reliably when shown the actual
validation error; an exception ends the turn and produces nothing useful.

Internal exception text never reaches the model or the user: it is logged with
the trace id, and the model gets a generic failure. A stack trace in the context
window is both a leak and a distraction.

### Argon2id over bcrypt

Memory-hard, and no 72-byte input truncation. Costs ~50ms per login, which is the
point.

---

## Three things found while building this

These were caught by tests and are worth recording, because each is the kind of
failure that produces no error message.

### `websearch_to_tsquery` joins terms with AND

`"breakfast dining room hours"` becomes `'breakfast' & 'dine' & 'room' & 'hour'`,
and a chunk reading *"Breakfast is served from 7:00 AM to 10:00 AM in the main
dining room"* does not contain "hours" — so it does not match, and **the lexical
arm returns nothing**.

Everything still worked. The vector arm answered, the endpoint responded, and
"hybrid" search had quietly become vector-only. It surfaced only because an
integration test asserts that both arms contribute
([`test_both_arms_contribute`](../tests/integration/test_retrieval.py)).

Conjunctions are now relaxed to disjunctions, leaving `ts_rank_cd` to
discriminate. `RETRIEVAL__LEXICAL_RELAX_TO_OR=false` restores AND for
keyword-shaped corpora.

### A superuser silently bypasses Row Level Security

The Compose image's `POSTGRES_USER` is a superuser and owns the schema. Point
`DATABASE_URL` at it and every policy on every table is ignored — no error, no
failed query, every tenant sees everything.

The RLS integration tests initially "passed" while proving nothing, because they
connected as that role. Now: the application connects as `app_rw`
(`NOSUPERUSER NOBYPASSRLS`), a boot guard refuses to start in production
otherwise, `/health/ready` reports it, and
[a test asserts the test role itself cannot bypass RLS](../tests/integration/test_rls.py).

### A location boost large enough to bury relevant content

The first implementation boosted location passages by 1.25. With RRF the gap
between consecutive ranks is under 2%, so a 25% boost lifts a passage roughly
fifteen places — and in the very first demo run, Ginza's "Fitness Centre"
outranked the group's breakfast policy for a breakfast question.

The boost is now 1.02, derived from the arithmetic: at `k=60` a boost `b` lifts
rank `r` above rank 1 exactly when `b > (60+r)/61`, so 1.02 admits only rank 2.
Preference wins ties; suppression handles conflicts.

---

## Traps designed against up front

| | |
|---|---|
| **Tenant context leaking through the pool** | `SET` instead of `SET LOCAL`, or `SET LOCAL` outside a transaction, silently no-ops and the value persists on a pooled connection. One `tenant_session` path, `set_config(..., true)` inside an explicit transaction, `FORCE RLS`, and [a test that greps for `set_config(..., false)`](../tests/unit/test_architecture.py). |
| **Holding a transaction across a model call** | Tenant context lives in the transaction, so one long request-scoped transaction is the *natural* implementation — and it exhausts the pool at around twenty concurrent chats. Services take a `UnitOfWork`, `idle_in_transaction_session_timeout` is 15s on the app role, and `AsyncSession` is banned from agent signatures by a test. |
| **Filtered-ANN recall collapse** | HNSW traverses globally and filters afterwards, so a small tenant in a large corpus gets near-empty results. Partial index on `is_active`, `hnsw.iterative_scan` (detected, not assumed), a wider retry on older pgvector, and the lexical arm as a floor. See [hybrid search](hybrid-search.md#filtered-ann-recall). |
| **Embedding-space drift** | Changing to a different model of the *same* dimension writes incomparable vectors with no error at all. `embedding_spaces` + per-chunk stamping + a refuse-to-boot guard. |
| **Half-visible document versions** | Chunks written `is_active=false`, a row lock, demote-then-promote, and gates inside the lock. See [versioning](versioning.md). |

---

## Deliberately not built

Listed in the specification as later work, and left there on purpose.

| | Why, and where it would go |
|---|---|
| **Reranking** | A cross-encoder over the top ~50 would improve precision more than any tuning here. It slots between fusion and the context builder. Not built because it needs a second model and its value depends on a corpus that does not exist yet. |
| **Query classification models** | The planner is one structured call on the fast tier. A trained classifier would be cheaper per request and worse to debug. |
| **Adaptive fusion weights** | Weights are static config. Learning them needs labelled relevance data. |
| **Richer hierarchy** | The merge takes an *ordered list* of scoped result sets, not a hardcoded pair, so a region or brand tier is a change to `Retriever._levels` alone. Two levels are what the specification asked for. |
| **Distributed tracing export** | Spans are collected and returned in the trace. An OpenTelemetry exporter wraps `TraceContext` without changing call sites. |
| **SSE resume** | Events could be mirrored to a Redis stream and replayed from `Last-Event-ID`. A dropped connection currently means re-asking. |
| **Kubernetes manifests** | The images are plain containers; a Helm chart adds no information the compose file does not already carry. |
| **Enterprise SSO** | JWT issuance is one service. OIDC would replace `AuthService.login` and nothing else. |
| **Agent evaluation harness** | `grounding_ratio` is a signal, not a score. A real eval needs golden questions with known answers — the right next investment, and the one that would tell you whether any of the retrieval tuning above actually helped. |

---

## Honest limitations

- **Chunk quality depends on document structure.** A PDF with no heading
  hierarchy — a scanned fax, a spreadsheet export — degrades to paragraph
  packing, and the override mechanism has nothing to key on.
- **`grounding_ratio` measures citation, not correctness.** It catches an answer
  invented while sources went unused. It cannot tell you a cited passage
  actually supports the claim.
- **The demo's retrieval quality is not the system's.** Tests and the seed script
  use a hash-based fake embedder so they need no model server. It has no semantic
  understanding at all; ranking with a real model is substantially better.
- **OCR quality is Tesseract's.** Fine on clean scans, poor on photographs and
  handwriting. The `OCRProvider` seam exists so Document AI or Textract is one
  file away.
- **Conversation memory is per-conversation, not per-user.** There is no
  long-term user memory, deliberately: [conversational state is not
  organizational knowledge](../app/services/agent/memory.py).
- **Cost estimates are only as good as the manifest.** Anthropic's rates are
  filled in; the other vendors' are left at zero rather than guessed, because a
  stale published rate is worse than none — the router would optimize against it.
