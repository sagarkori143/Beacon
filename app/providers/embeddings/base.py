"""The embedding provider abstraction.

Same registry pattern as the LLM layer: ``type`` in the manifest maps to a class,
so adding a vendor is one file plus one config entry.

Two things every implementation must respect:

* ``dimension`` is declared, not discovered at random. It is compared against the
  vector column and the current embedding space at boot, and a mismatch stops
  the process rather than corrupting the index.
* Documents and queries go through *different* methods. Several modern models
  (nomic-embed-text among them) require an asymmetric prefix, and getting that
  backwards degrades retrieval in a way that is very hard to notice.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, TypeVar

from app.core.config import EmbeddingProviderConfig
from app.core.errors import ConfigurationError
from app.core.logging import get_logger
from app.core.tracing import TraceContext
from app.providers.base import ProviderHealth

if TYPE_CHECKING:
    pass

log = get_logger(__name__)


class EmbeddingProvider(ABC):
    #: The manifest entry's name ("default", "backup", ...). Identifies *this
    #: configured instance*.
    name: str
    #: The registry type ("ollama", "openai", ...). Set automatically by
    #: @register_embedding_provider.
    #:
    #: Embedding-space identity uses this rather than `name`, because renaming a
    #: manifest entry must not invalidate every vector already indexed -- the
    #: space is a property of the model, not of what an operator called the
    #: config block.
    provider_type: str = "unknown"
    model: str
    dimension: int
    normalize: bool

    @abstractmethod
    async def embed_documents(
        self, texts: Sequence[str], *, trace: TraceContext | None = None
    ) -> list[list[float]]:
        """Embed chunk text for indexing. Order of results matches input."""

    @abstractmethod
    async def embed_query(self, text: str, *, trace: TraceContext | None = None) -> list[float]:
        """Embed a search query."""

    @abstractmethod
    async def health(self) -> ProviderHealth: ...

    @abstractmethod
    async def aclose(self) -> None: ...

    # -- shared helpers ------------------------------------------------------

    def _postprocess(self, vector: Sequence[float]) -> list[float]:
        """Validate the dimension and L2-normalize.

        Normalizing on write means cosine distance in pgvector reduces to a dot
        product, and it makes the dedup threshold in the context builder a plain
        cosine similarity comparison.
        """
        if len(vector) != self.dimension:
            raise ConfigurationError(
                f"Embedding provider '{self.name}' returned {len(vector)} dimensions "
                f"but is configured for {self.dimension}. The model on the server "
                f"({self.model}) does not match the manifest.",
                details={"expected": self.dimension, "actual": len(vector)},
            )
        if not self.normalize:
            return list(vector)
        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0.0:
            return list(vector)
        return [v / norm for v in vector]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

TEmbedding = TypeVar("TEmbedding", bound="type[EmbeddingProvider]")

_EMBEDDING_TYPES: dict[str, type[EmbeddingProvider]] = {}


def register_embedding_provider(type_name: str) -> Callable[[TEmbedding], TEmbedding]:
    def decorator(cls: TEmbedding) -> TEmbedding:
        if type_name in _EMBEDDING_TYPES and _EMBEDDING_TYPES[type_name] is not cls:
            raise ConfigurationError(f"Duplicate embedding provider type '{type_name}'")
        _EMBEDDING_TYPES[type_name] = cls
        # Stamp the registry key onto the class so embedding-space identity and
        # the manifest cannot disagree.
        cls.provider_type = type_name  # type: ignore[attr-defined]
        return cls

    return decorator


_loaded = False


def _load_builtin() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    from app.providers.embeddings import fake, ollama, openai_compatible  # noqa: F401


def available_embedding_types() -> list[str]:
    _load_builtin()
    return sorted(_EMBEDDING_TYPES)


def build_embedding_provider(config: EmbeddingProviderConfig) -> EmbeddingProvider:
    _load_builtin()
    cls = _EMBEDDING_TYPES.get(config.type)
    if cls is None:
        raise ConfigurationError(
            f"Unknown embedding provider type '{config.type}' for '{config.name}'. "
            f"Known types: {', '.join(sorted(_EMBEDDING_TYPES))}"
        )
    return cls(config)  # type: ignore[call-arg]
