"""OpenAI-compatible embedding provider.

Covers OpenAI itself plus every gateway that implements ``/embeddings``:
Together, Voyage (via its compatible endpoint), Jina, DeepInfra, vLLM's
embedding server, LM Studio. ``type: openai`` is registered as an alias with the
canonical base URL prefilled.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Any

from app.core.config import EmbeddingProviderConfig
from app.core.errors import ConfigurationError, ProviderError, ProviderTimeout
from app.core.logging import get_logger
from app.core.tracing import TraceContext
from app.providers.base import HTTPProviderBase, ProviderHealth
from app.providers.embeddings.base import EmbeddingProvider, register_embedding_provider

log = get_logger(__name__)


@register_embedding_provider("openai_compatible")
class OpenAICompatibleEmbeddingProvider(HTTPProviderBase, EmbeddingProvider):
    default_base_url = ""

    def __init__(self, config: EmbeddingProviderConfig) -> None:
        base_url = config.base_url or self.default_base_url
        if not base_url:
            raise ConfigurationError(f"Embedding provider '{config.name}' requires base_url")

        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"

        HTTPProviderBase.__init__(
            self,
            name=config.name,
            base_url=base_url,
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

    async def embed_documents(
        self, texts: Sequence[str], *, trace: TraceContext | None = None
    ) -> list[list[float]]:
        if not texts:
            return []
        batches = [
            list(texts[i : i + self.batch_size]) for i in range(0, len(texts), self.batch_size)
        ]
        results = await asyncio.gather(*(self._embed_batch(b, trace, "document") for b in batches))
        return [vector for batch in results for vector in batch]

    async def embed_query(self, text: str, *, trace: TraceContext | None = None) -> list[float]:
        vectors = await self._embed_batch([text], trace, "query")
        return vectors[0]

    async def _embed_batch(
        self, inputs: list[str], trace: TraceContext | None, kind: str
    ) -> list[list[float]]:
        payload: dict[str, Any] = {"model": self.model, "input": inputs}
        # Matryoshka models (text-embedding-3-*) accept an explicit output size;
        # sending it keeps the provider and the vector column in agreement.
        if self.config.options.get("send_dimensions", True):
            payload["dimensions"] = self.dimension
        # Voyage and Cohere-style endpoints distinguish document from query.
        if input_type := self.config.options.get("input_type"):
            payload["input_type"] = f"{input_type}_{kind}" if input_type is True else input_type

        body = await self._call(
            lambda: self._post_json("/embeddings", payload), trace=trace, operation="embed"
        )
        data = body.get("data")
        if not isinstance(data, list) or len(data) != len(inputs):
            raise ProviderError(
                f"{self.name}: expected {len(inputs)} embeddings, got "
                f"{len(data) if isinstance(data, list) else 'none'}",
                provider=self.name,
            )
        # The API does not guarantee ordering; each item carries its index.
        ordered = sorted(data, key=lambda d: d.get("index", 0))
        return [self._postprocess(item["embedding"]) for item in ordered]

    async def health(self) -> ProviderHealth:
        started = time.perf_counter()
        try:
            vectors = await self._embed_batch(["healthcheck"], None, "query")
        except ProviderTimeout:
            return ProviderHealth(self.name, ok=False, detail="timeout")
        except Exception as exc:  # noqa: BLE001 - health must never raise
            return ProviderHealth(self.name, ok=False, detail=str(exc)[:200])
        return ProviderHealth(
            name=self.name,
            ok=True,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            extra={"model": self.model, "dimension": len(vectors[0])},
        )

    async def aclose(self) -> None:
        await HTTPProviderBase.aclose(self)


@register_embedding_provider("openai")
class OpenAIEmbeddingProvider(OpenAICompatibleEmbeddingProvider):
    default_base_url = "https://api.openai.com/v1"


@register_embedding_provider("voyage")
class VoyageEmbeddingProvider(OpenAICompatibleEmbeddingProvider):
    default_base_url = "https://api.voyageai.com/v1"

    def __init__(self, config: EmbeddingProviderConfig) -> None:
        # Voyage rejects `dimensions` and requires `input_type`.
        options = {**config.options}
        options.setdefault("send_dimensions", False)
        options.setdefault("input_type", True)
        super().__init__(config.model_copy(update={"options": options}))
