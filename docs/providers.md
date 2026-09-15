# Providers

The set of usable LLMs, embedding models, OCR engines, storage backends and
queues is **data, not code**. `config/providers.yaml` names a `type`, a registry
maps that string to a class, and the application never imports an
implementation.

---

## What ships

| Family | Types |
|---|---|
| LLM | `ollama`, `anthropic`, `openai`, `gemini`, `openai_compatible`, `fake` |
| Embeddings | `ollama`, `openai`, `voyage`, `openai_compatible`, `fake` |
| Vector store | `pgvector` |
| Search | `postgres_hybrid` |
| OCR | `tesseract`, `noop` |
| Storage | `local`, `s3`, `memory` |
| Queue | `redis_streams`, `memory` |

`openai_compatible` is one class covering many vendors — vLLM, Groq, Together,
Fireworks, OpenRouter, DeepSeek, Mistral, LM Studio, llama.cpp's server, Azure
OpenAI. Adding any of them is a manifest entry with a `base_url`, no code.

---

## The manifest

```yaml
llm:
  - name: local                      # your GPU box, wherever it is
    type: ollama
    base_url: ${OLLAMA_BASE_URL}
    privacy: local
    max_concurrency: 2               # Ollama serializes model execution
    models:
      - name: ${OLLAMA_MODEL:-qwen2.5:7b-instruct}
        tier: balanced
        context_window: 32768
        supports_tools: true
        supports_json_schema: true

  - name: claude
    type: anthropic
    api_key: ${ANTHROPIC_API_KEY}    # absent → provider skipped, not an error
    models:
      - name: claude-sonnet-5
        tier: balanced
        context_window: 1000000
        supports_tools: true
        supports_json_schema: true
        cost_per_1m_input: 2.00
        cost_per_1m_output: 10.00

embeddings:
  - name: default
    type: ollama
    model: ${OLLAMA_EMBEDDING_MODEL:-nomic-embed-text}
    dimension: 768
```

`${VAR}` and `${VAR:-default}` are interpolated from the environment at load
time, so the manifest carries no secrets and is safe to commit. An unset variable
with no default resolves to `None` rather than an empty string — which is how a
provider whose credentials are absent gets skipped cleanly instead of failing
later with a confusing 401.

A provider that fails to construct is **skipped with a warning, not fatal**. A
deployment with only a local model server must boot exactly as happily as one
with four vendors, and an expired cloud key must degrade routing rather than take
the API down. `GET /health/ready` lists what was skipped and why.

---

## Routing

The router reasons over **declared attributes** — `tier`, `context_window`,
`supports_tools`, `supports_json_schema`, `privacy`, cost — and never over
provider names. A vendor added to the manifest becomes routable immediately,
with no code change anywhere.

Rules, first match wins, each filtered afterwards by capability and health:

1. **Organization pin** — `organizations.settings.model_pins`. Enterprises pin
   models for compliance, so this outranks every heuristic below.
2. **Caller preference** — `{"model": "claude/claude-sonnet-5"}`, honoured only
   if the organization's allow-list permits that provider.
3. **Tool turns** take the *smallest* tool-capable model. Choosing which tool to
   call is an easier task than composing the answer, and on a serialized local
   model server the quality tier is a scarce slot.
4. **Cheap tasks** (classify, rewrite, title, plan) take the fast tier.
5. **High-stakes document types** (legal, hr, safety, compliance, medical) take
   the quality tier regardless of cost.
6. **Default**: balanced tier, preferring local at equal cost.
7. **Health filter**, always last: providers with an open circuit are dropped,
   `fallbacks` are walked in order, and `NoModelAvailable` (503 with
   `Retry-After`) is raised only when everything is down.

Hard requirements are filters, not preferences: `needs_json` removes models
without `supports_json_schema`, because routing a structured call to a model that
cannot constrain its output means falling back to prompt-and-pray parsing.

`GET /health/models` lists every routable model with its declared capabilities —
the fastest way to answer "why did it pick that model?".

---

## Adding a vendor

**If it speaks the Chat Completions shape**, there is no code:

```yaml
  - name: together
    type: openai_compatible
    base_url: https://api.together.xyz/v1
    api_key: ${TOGETHER_API_KEY}
    models:
      - name: meta-llama/Llama-3.3-70B-Instruct-Turbo
        tier: balanced
        context_window: 131072
        supports_tools: true
```

**If it has its own wire format**, one file:

```python
# app/providers/llm/cohere.py
@register_llm_provider("cohere")
class CohereProvider(HTTPProviderBase, ModelProvider):
    async def generate(self, *, model, messages, tools=None, params=..., trace=None) -> Completion: ...
    def generate_stream(self, ...) -> AsyncIterator[StreamEvent]: ...
    def capabilities(self, model) -> ModelInfo: ...
    async def list_models(self) -> Sequence[ModelInfo]: ...
    async def health(self) -> ProviderHealth: ...
    async def aclose(self) -> None: ...
```

Add it to the import list in `app/providers/llm/registry.py::_load_builtin_providers`
and it is routable. `HTTPProviderBase` already supplies the client, timeouts,
bounded concurrency, jittered retry honouring `Retry-After`, the circuit breaker
and error normalization — a provider file contains only that vendor's wire
format.

`generate_structured` has a working default (schema in the prompt, validate,
repair); override `_generate_json` when the vendor can constrain decoding
natively. The three shipped approaches are worth comparing: Ollama passes a
schema in `format`, OpenAI uses `response_format: json_schema`, and Anthropic —
which has no equivalent — forces a single tool whose `input_schema` is the target
schema, which gives the same guarantee.

---

## Embedding spaces

An embedding space is a `(provider type, model, dimension)` triple. Vectors are
only comparable within one.

This exists because of a failure with no error message. Change
`OLLAMA_EMBEDDING_MODEL` to a different model of the *same* dimension and every
insert succeeds, every query runs, and retrieval quality collapses. Nothing
raises. Nothing logs. Answers just get worse.

So: every chunk carries `embedding_space_id`, every search filters on the current
space, and a boot guard compares three things —

1. `EMBEDDING_DIM` and the `vector(N)` column built from it at migration time;
2. the provider's declared dimension;
3. what the model on your server actually returns

— and **refuses to start** if they disagree. When the model server is unreachable
the third check is skipped with a warning; an unreachable GPU box degrades chat
but should not stop the API serving document management and search.

### Changing the embedding model

Not an env edit. The procedure:

1. Add the new space and a second vector column
   (`ALTER TABLE chunks ADD COLUMN embedding_v2 vector(1024)`).
2. Dual-write: ingest writes both while you backfill existing chunks.
3. Backfill in batches, re-embedding from `document_versions.normalized_text` —
   persisted during CLEANING precisely so this never re-parses or re-OCRs.
4. `CREATE INDEX CONCURRENTLY` the new HNSW index, **outside** an Alembic
   transaction (`op.get_bind().execution_options(isolation_level="AUTOCOMMIT")`).
5. Flip `is_current` to the new space, in one transaction.
6. Drop the old column and index once you are satisfied.

Steps 1–4 are online. Only step 5 changes what search reads, and it is atomic.

---

## OCR

The pipeline depends on `OCRProvider` and never on Tesseract:

```python
class OCRProvider(ABC):
    async def extract_text(
        self, images: Sequence[tuple[int, bytes]], *, languages: Sequence[str] | None = None
    ) -> OCRResult: ...
```

`images` is `(page_number, png_bytes)` pairs so a partial run — only the pages
that need OCR — keeps real page numbers for citations.

`OCR__PROVIDER=tesseract` today; adding Google Document AI or AWS Textract means
one new file and one config value, with no change to the pipeline. Tesseract is
installed in the **worker image only**: parsing untrusted uploads does not belong
in the process serving requests, and the API image stays ~150MB smaller for it.

OCR is never run speculatively — see [architecture](architecture.md) and
`app/services/ingestion/ocr_gate.py` for the assessment that decides.

---

## Testing against providers

`FakeLLMProvider` and `FakeEmbeddingProvider` implement the full interfaces —
tools, streaming, structured output. The fake embedder hashes tokens into a
fixed-dimension space, so vectors are stable across runs and processes, which is
what lets integration tests assert on retrieval *ordering* without a model server
anywhere.

No test in the suite requires one.
