# API

Base path `/api/v1`. Health is unversioned at `/health`, so probes do not have to
track the API version. Interactive docs at `/docs`.

All responses below are **real output** from the demo tenant, lightly trimmed.
The answer text is a stub because these were captured with the fake model
provider; everything else — retrieval, scoping, citations, routing — is genuine.

Errors are [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) problem documents
with a stable `type` and the `request_id` to quote in a bug report.

---

## Authentication

Every endpoint except `/health` and `/auth/login` needs
`Authorization: Bearer <access_token>`.

**The token carries the tenant.** No endpoint accepts an `organization_id` from
the client.

### `POST /auth/login`

```json
{"email": "ginza@sagarhotels.example", "password": "demo-password-12345"}
```

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6...",
  "token_type": "bearer",
  "expires_in": 1800,
  "organization": "Sagar Hotels",
  "location": "Sagar Ginza",
  "role": "USER"
}
```

The organization and location are echoed so a client can render "signed in to
Sagar Hotels, Ginza" without a second round trip.

### `POST /auth/refresh`

Re-reads the user, so a deactivation or role change takes effect here rather than
at the refresh token's natural expiry two weeks later.

### `GET /auth/me`

```json
{
  "user": {
    "id": "9001234f-9cba-408c-84bf-998bfe3f2f94",
    "email": "ginza@sagarhotels.example",
    "full_name": "Ginza Front Desk",
    "role": "USER",
    "location_id": "e214e4bf-28bf-4b80-809f-30196d897205",
    "is_active": true
  },
  "organization": {"id": "d04e29a5-…", "name": "Sagar Hotels", "slug": "sagar-hotels"},
  "location": {
    "name": "Sagar Ginza",
    "timezone": "Asia/Tokyo",
    "settings": {
      "address": "6-10-1 Ginza, Chuo-ku, Tokyo",
      "phone": "+81-3-5555-0101",
      "front_desk_hours": "24 hours",
      "amenities": ["fitness centre", "pool", "valet parking", "Ginza Grill"]
    }
  },
  "scopes": ["knowledge:read", "tools:basic"]
}
```

`location.settings` is what the `location_info` tool answers from — structured
facts that belong in a field rather than a document.

### `POST /auth/users` *(admin)*

Creates a user in the caller's organization. A `location_id`, if given, is
validated against that organization.

---

## Documents

### `POST /documents` *(admin, multipart)* → `202 Accepted`

```bash
curl -X POST localhost:8000/api/v1/documents \
  -H "authorization: Bearer $TOKEN" \
  -F file=@handbook.pdf \
  -F title="Sagar Hotels Guest Handbook" \
  -F document_type=policy
  # omit location_id for organization-wide knowledge
```

```json
{
  "document_id": "a82b2186-813e-4ce3-acbc-2a8502bf33e7",
  "version_id": "7c1e0d18-2c0a-4f5e-9d4a-5a1f2b3c4d5e",
  "version_number": 1,
  "job_id": "9f8b5f00-8be0-473a-a7d0-e567e6df42cc",
  "status": "QUEUED",
  "message": "Upload accepted. Track progress with GET /ingestion/jobs/{job_id}."
}
```

Returns as soon as the file is stored and the job is queued. Parsing, OCR and
embedding happen in a worker.

A file whose title slug matches an existing document becomes **the next version
of it**, not a second document. The previous version keeps serving every query
until the new one passes validation.

Validation before anything is stored: magic-byte sniff against the declared
content type, size cap, allowed MIME types. `413`/`422` with a problem document
on rejection.

### `GET /documents`, `GET /documents/{id}`, `GET /documents/{id}/versions`

A user sees their location's documents plus organization-wide ones.
`GET /documents/{id}` includes every version and which is active.

### `POST /documents/{id}/versions` *(admin)*

A new version of a specific document, inheriting its title, scope and language.

### `POST /documents/versions/{version_id}/activate` *(admin)*

Rollback. Runs the identical transaction as a forward activation, gates included
— a version that has since become unretrievable will not activate.

---

## Ingestion

### `GET /ingestion/jobs/{job_id}`

```json
{
  "id": "9f8b5f00-…",
  "document_version": 1,
  "status": "COMPLETED",
  "current_stage": "COMPLETED",
  "progress": 1.0,
  "attempts": 1,
  "error_message": null,
  "started_at": "2026-09-15T18:22:35.4Z",
  "completed_at": "2026-09-15T18:22:38.9Z"
}
```

### `GET /ingestion/jobs/{job_id}/events`

Per-stage history with timings and detail. The OCR decision is recorded on
**every** document, not only failures:

```json
[
  {"stage": "PARSING", "status": "COMPLETED", "duration_ms": 412.8,
   "detail": {"pages": 12, "layout_lines": 384, "repeating_lines": 2}},
  {"stage": "OCR", "status": "COMPLETED", "duration_ms": 8.1,
   "detail": {"mode": "NATIVE", "coverage": 1.0, "ocr_page_count": 0,
              "reasons": ["native_text (coverage=1.00)"]}},
  {"stage": "CHUNKING", "status": "COMPLETED", "duration_ms": 96.4,
   "detail": {"chunks": 6, "mean_tokens": 61.2, "sections": 6}},
  {"stage": "VALIDATING", "status": "COMPLETED", "duration_ms": 41.0,
   "detail": {"passed": true, "gates": [{"name": "chunk_count", "passed": true}, "…"]}}
]
```

So "why did this take four minutes?" is one request, not an investigation.

### `GET /ingestion/queue`

Depth, pending count, oldest idle time, dead-letter length. Operational counters
only — no document or organization identifiers.

---

## Search

### `POST /search`

Retrieval without an LLM. Useful for debugging what the agent actually sees.

```json
{"query": "what time is breakfast served", "top_k": 2}
```

**As the Ginza user:**

```json
{
  "query": "what time is breakfast served",
  "hits": [
    {
      "chunk_id": "f32af79e-…",
      "source": "Sagar Ginza Property Guide.md",
      "scope": "LOCATION",
      "section": "Sagar Ginza Property Guide > Breakfast Hours",
      "page_from": 1,
      "content": "…breakfast is served from 7:00 AM to 11:00 AM in the Ginza Grill…",
      "score": 0.016721,
      "vector_score": 0.266997,
      "keyword_score": 0.722222,
      "matched": ["vector", "keyword"]
    }
  ],
  "total": 2,
  "took_ms": 36.1,
  "suppressed_by_override": 1,
  "overridden_topics": ["breakfast"],
  "degraded": false
}
```

**As the Chiyoda user, same query:** the group's 7:00–10:00, `suppressed_by_override: 0`,
and nothing from Ginza. One stored document, two correct answers.

Both arms' raw scores and ranks are returned, because "why did this rank here?"
is the question you always end up asking. `degraded: true` means the ANN arm lost
recall to tenant filtering and a wider probe recovered it.

Set `"hierarchical": false` to skip the override merge and see the raw ranking —
what you want when diagnosing why a passage did or did not surface.

---

## Chat

### `POST /chat`

```json
{"message": "What time is breakfast?", "include_trace": true}
```

```json
{
  "answer": "Breakfast at Sagar Ginza is served from 7 AM to 11 AM [S1].",
  "conversation_id": "660caa96-0c74-4487-9c5d-252487f83913",
  "citations": [
    {"ref": "S1", "source": "Sagar Ginza Property Guide.md",
     "locator": "Sagar Ginza Property Guide.md p.1",
     "section": "Sagar Ginza Property Guide > Breakfast Hours",
     "scope": "LOCATION", "document_version": 1, "score": 0.0167}
  ],
  "provider": "local",
  "model": "qwen2.5:7b-instruct",
  "usage": {"prompt_tokens": 1240, "completion_tokens": 38, "estimated_cost_usd": 0.0},
  "grounding_ratio": 1.0,
  "low_confidence": false,
  "finish_reason": "stop",
  "latency_ms": 2184.6
}
```

Only the sources the answer **actually cited** are returned. Listing six under an
answer that drew on two overstates the evidence.

`grounding_ratio` is the fraction of substantive sentences carrying a valid
citation. It cannot tell you whether a cited passage supports the claim — that
needs a judge model — but it reliably catches the failure that matters most here:
an answer produced from the model's own priors while sources sat unused.

`low_confidence: true` means sources were available and the answer cited none.

`include_trace: true` adds routing, plan, retrieval and tool diagnostics:

```json
{
  "plan": {"intent": "answer_from_knowledge", "needs_retrieval": true,
           "queries": ["breakfast hours serving time"], "tools": []},
  "routing": {"provider": "local", "model": "qwen2.5:7b-instruct",
              "rule": "default_balanced", "reason": "balanced tier",
              "fallbacks": ["claude/claude-haiku-4-5"]},
  "context": {"passages": 8, "tokens": 458, "dropped_duplicates": 0,
              "scopes": ["LOCATION", "ORGANIZATION"], "chunk_ids": ["…"]},
  "cited_refs": ["S1"],
  "spans": [{"name": "agent.plan", "duration_ms": 180.2}, "…"]
}
```

Other fields: `tools` (restrict which tools this turn may use), `force_retrieval`
(override the planner), `model` (`"provider/model"`, honoured only if the
organization's allow-list permits it), `language`.

### `POST /chat/stream`

Server-Sent Events. The events are **semantic**, not raw model output — tool
turns do not stream, because a half-streamed arguments blob tells a user nothing,
and "looking up availability for 2026-10-02" is genuinely useful progress.

```
event: stage
data: {"stage": "planning"}

event: plan
data: {"intent": "answer_from_knowledge", "needs_retrieval": true, "queries": ["What time is breakfast?"], "tools": []}

event: stage
data: {"stage": "retrieving"}

event: search
data: {"queries": ["What time is breakfast?"], "hits": 8, "per_scope": {"LOCATION": 4, "ORGANIZATION": 6}, "degraded": false}

event: conflict
data: {"suppressed": 1, "topics": ["breakfast"]}

event: citation
data: {"ref": "S1", "source": "Sagar Ginza Property Guide.md", "scope": "LOCATION", "section": "… > Breakfast Hours", "score": 0.0167}

event: stage
data: {"stage": "generating", "provider": "local", "model": "qwen2.5:7b-instruct"}

event: token
data: {"text": "Breakfast "}

event: usage
data: {"prompt_tokens": 1240, "completion_tokens": 38, "estimated_cost_usd": 0.0}

event: done
data: {"conversation_id": "660caa96-…", "trace_id": "8f2c…", "finish_reason": "stop", "grounding_ratio": 1.0}
```

The `conflict` event surfaces the override to the client rather than applying it
silently: "we used the Ginza figure, not the group-wide one" is exactly what a
user needs when an answer surprises them.

A closed connection cancels the run and aborts the upstream request, so a closed
browser tab does not hold a model-server slot.

| Event | Meaning |
|---|---|
| `stage` | `planning` · `retrieving` · `tools` · `generating` |
| `plan` | The structured routing decision |
| `search` | Queries issued, hits, per-scope counts, recall degradation |
| `conflict` | Location knowledge overrode group knowledge |
| `citation` | One source, emitted before generation starts |
| `tool_call` / `tool_result` | Tool activity, with latency and outcome |
| `token` | Answer text |
| `usage` | Tokens and estimated cost |
| `done` | Terminal. Carries `conversation_id` and `trace_id` |
| `error` | Terminal. `code`, `message`, `retryable` |

---

## Organizations and locations

`GET /organizations/me`, `GET /locations`, `GET /locations/{id}`,
`POST /locations` *(admin)*.

There is no "list organizations": a caller's organization comes from their token,
and the only one they can ever see is their own. A user pinned to one location
sees only that location in `GET /locations`.

---

## Health

| | |
|---|---|
| `GET /health` | **Liveness.** Database and Redis only. This is what a load balancer should gate on. |
| `GET /health/ready` | **Readiness.** Adds every provider and the model server, plus the application-role check. Reports `degraded` rather than failing when only providers are unhealthy. |
| `GET /health/models` | Every routable model with its declared capabilities — the fastest way to answer "why did it pick that model?" |

```json
{
  "status": "ok",
  "checks": {
    "database": {"ok": true, "pgvector": "0.8.6"},
    "redis": {"ok": true},
    "application_role": {"ok": true, "role": "app_rw",
                         "superuser": false, "bypasses_rls": false},
    "embeddings": {"ok": true, "model": "nomic-embed-text", "dimension": 768},
    "llm:local": {"ok": true, "latency_ms": 41.2, "installed_models": 3}
  }
}
```

**Do not gate a load balancer on `/health/ready`.** An unreachable model server
degrades chat; document management, search over already-indexed data and
everything else still work. Pulling the instance out of rotation would turn a
partial outage into a total one.
