"""Ollama embedding provider.

Like the LLM provider, this talks to a model server that is normally on another
machine, so batching, bounded concurrency, timeouts and retries are not optional
niceties -- an ingestion run may issue thousands of these calls across a network.

``nomic-embed-text`` and several other current models are asymmetric: they expect
``search_document:`` on indexed text and ``search_query:`` on queries. Getting
that backwards quietly costs a noticeable amount of retrieval quality, so the
prefixes are applied here rather than left to callers.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence

from app.core.config import EmbeddingProviderConfig
from app.core.errors import ProviderError, ProviderTimeout
from app.core.logging import get_logger
from app.core.tracing import TraceContext
from app.providers.base import HTTPProviderBase, ProviderHealth
from app.providers.embeddings.base import EmbeddingProvider, register_embedding_provider

log = get_logger(__name__)

#: Models known to require task prefixes, and the prefixes they expect.
_ASYMMETRIC_PREFIXES: dict[str, tuple[str, str]] = {
    "nomic-embed-text": ("search_document: ", "search_query: "),
    "mxbai-embed-large": ("", "Represent this sentence for searching relevant passages: "),
    "snowflake-arctic-embed": ("", "Represent this sentence for searching relevant passages: "),
}


@register_embedding_provider("ollama")
class OllamaEmbeddingProvider(HTTPProviderBase, EmbeddingProvider):
    def __init__(self, config: EmbeddingProviderConfig) -> None:
        headers: dict[str, str] = {}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"

        HTTPProviderBase.__init__(
            self,
            name=config.name,
            base_url=config.base_url or "http://localhost:11434",
            timeout_s=config.timeout_s,
            connect_timeout_s=config.connect_timeout_s,
            max_concurrency=config.max_concurrency,
            max_retries=config.max_retries,
            headers=headers,
        )
        self.config = config
        self.model = config.model
        self.dimension = config.dimension
        self.normalize = config.normalize
        self.batch_size = config.batch_size

        base_model = config.model.split(":")[0]
        override = config.options.get("prefixes")
        if isinstance(override, dict):
            self._doc_prefix = str(override.get("document", ""))
            self._query_prefix = str(override.get("query", ""))
        else:
            self._doc_prefix, self._query_prefix = _ASYMMETRIC_PREFIXES.get(base_model, ("", ""))

    # -- embedding -----------------------------------------------------------

    async def embed_documents(
        self, texts: Sequence[str], *, trace: TraceContext | None = None
    ) -> list[list[float]]:
        if not texts:
            return []

        batches = [
            [self._doc_prefix + t for t in texts[i : i + self.batch_size]]
            for i in range(0, len(texts), self.batch_size)
        ]
        # Bounded concurrency matters here: Ollama serializes model execution, so
        # firing every batch at once queues them all and blows the timeout.
        results = await asyncio.gather(*(self._embed_batch(b, trace) for b in batches))
        return [vector for batch in results for vector in batch]

    async def embed_query(self, text: str, *, trace: TraceContext | None = None) -> list[float]:
        vectors = await self._embed_batch([self._query_prefix + text], trace)
        return vectors[0]

    async def _embed_batch(
        self, inputs: list[str], trace: TraceContext | None
    ) -> list[list[float]]:
        payload = {"model": self.model, "input": inputs}
        body = await self._call(
            lambda: self._post_json("/api/embed", payload),
            trace=trace,
            operation="embed",
        )
        raw = body.get("embeddings")
        if not isinstance(raw, list) or len(raw) != len(inputs):
            raise ProviderError(
                f"{self.name}: expected {len(inputs)} embeddings, got "
                f"{len(raw) if isinstance(raw, list) else 'none'}",
                provider=self.name,
            )
        return [self._postprocess(vector) for vector in raw]

    # -- health --------------------------------------------------------------

    async def health(self) -> ProviderHealth:
        """Embed a probe string and confirm the dimension the server returns.

        This is the check that catches someone changing the model on the GPU box
        without updating the manifest -- the failure that would otherwise show up
        as silently worse answers.
        """
        started = time.perf_counter()
        try:
            body = await self._post_json(
                "/api/embed", {"model": self.model, "input": ["healthcheck"]}
            )
        except ProviderTimeout:
            return ProviderHealth(self.name, ok=False, detail="timeout")
        except Exception as exc:  # noqa: BLE001 - health must never raise
            return ProviderHealth(self.name, ok=False, detail=str(exc)[:200])

        vectors = body.get("embeddings") or []
        actual = len(vectors[0]) if vectors else 0
        ok = actual == self.dimension
        return ProviderHealth(
            name=self.name,
            ok=ok,
            detail=(
                None
                if ok
                else f"model '{self.model}' returns {actual} dimensions, configured {self.dimension}"
            ),
            latency_ms=(time.perf_counter() - started) * 1000.0,
            extra={"model": self.model, "dimension": actual},
        )

    async def aclose(self) -> None:
        await HTTPProviderBase.aclose(self)
