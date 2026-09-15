"""Deterministic in-process embedding provider.

Vectors are derived from a hash of the text, so they are stable across runs and
across processes -- which is what lets integration tests assert on retrieval
*ordering* without a model server anywhere in the picture.

The mapping is not semantic, but it is consistent: identical text always yields
an identical vector, and a shared token between two texts nudges them toward
each other, which is enough to exercise ranking, fusion and deduplication.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence

from app.core.config import EmbeddingProviderConfig
from app.core.tracing import TraceContext
from app.providers.base import ProviderHealth
from app.providers.embeddings.base import EmbeddingProvider, register_embedding_provider

_TOKEN_RE = re.compile(r"[a-z0-9]+")


@register_embedding_provider("fake")
class FakeEmbeddingProvider(EmbeddingProvider):
    def __init__(self, config: EmbeddingProviderConfig | None = None) -> None:
        config = config or EmbeddingProviderConfig(
            name="fake", type="fake", model="fake-embed", dimension=768
        )
        self.config = config
        self.name = config.name
        self.model = config.model
        self.dimension = config.dimension
        self.normalize = True
        self.embed_calls = 0

    def _vector(self, text: str) -> list[float]:
        """Bag-of-tokens projection into a fixed-dimension space.

        Each token is hashed to a coordinate and a sign, which gives texts that
        share vocabulary a higher dot product -- a crude but stable stand-in for
        semantic similarity.
        """
        vector = [0.0] * self.dimension
        tokens = _TOKEN_RE.findall(text.lower()) or ["\x00"]
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]

    async def embed_documents(
        self, texts: Sequence[str], *, trace: TraceContext | None = None
    ) -> list[list[float]]:
        self.embed_calls += 1
        return [self._vector(t) for t in texts]

    async def embed_query(self, text: str, *, trace: TraceContext | None = None) -> list[float]:
        self.embed_calls += 1
        return self._vector(text)

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            name=self.name, ok=True, extra={"model": self.model, "dimension": self.dimension}
        )

    async def aclose(self) -> None:
        return None
