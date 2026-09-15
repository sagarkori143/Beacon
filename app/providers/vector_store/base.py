"""Vector store abstraction for chunk writes and visibility.

Reads go through :mod:`app.providers.search`; this interface owns the write side:
inserting chunk rows with their embeddings and lexical vectors, flipping their
visibility during version activation, and removing them.

It exists as a separate seam from search because that is where a dedicated
vector database would slot in: Qdrant or Milvus would implement this and the
lexical arm would stay in PostgreSQL, which is exactly the migration the
architecture is meant to allow.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar
from uuid import UUID

from app.core.config import Settings
from app.core.enums import SourceType
from app.core.errors import ConfigurationError
from app.providers.base import ProviderHealth


@dataclass(slots=True)
class ChunkRecord:
    """A chunk ready to be written. Mirrors the columns of ``chunks``."""

    id: UUID
    organization_id: UUID
    location_id: UUID | None
    document_id: UUID
    document_version_id: UUID
    document_version: int
    ordinal: int
    content: str
    content_hash: str
    token_count: int
    language: str
    source_type: SourceType
    source_name: str
    embedding_space_id: UUID
    embedding: list[float]
    heading: str | None = None
    section_path: list[str] = field(default_factory=list)
    section_key: str | None = None
    topic_key: str | None = None
    kind: str = "text"
    page_from: int | None = None
    page_to: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class VectorStore(ABC):
    name: str

    @abstractmethod
    async def upsert(self, session: Any, records: Sequence[ChunkRecord]) -> int:
        """Write chunks. They are written **inactive**: invisible to search until
        the version that owns them is activated."""

    @abstractmethod
    async def delete_by_version(self, session: Any, document_version_id: UUID) -> int: ...

    @abstractmethod
    async def set_active(self, session: Any, document_version_id: UUID, *, active: bool) -> int:
        """Flip visibility for every chunk of a version, in one statement."""

    @abstractmethod
    async def count_for_version(
        self, session: Any, document_version_id: UUID, *, only_embedded: bool = False
    ) -> int: ...

    @abstractmethod
    async def health(self) -> ProviderHealth: ...

    async def aclose(self) -> None:
        return None


TVectorStore = TypeVar("TVectorStore", bound="type[VectorStore]")

_VECTOR_STORE_TYPES: dict[str, type[VectorStore]] = {}


def register_vector_store(type_name: str) -> Callable[[TVectorStore], TVectorStore]:
    def decorator(cls: TVectorStore) -> TVectorStore:
        _VECTOR_STORE_TYPES[type_name] = cls
        return cls

    return decorator


_loaded = False


def _load_builtin() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    from app.providers.vector_store import pgvector  # noqa: F401


def build_vector_store(settings: Settings, provider: str = "pgvector") -> VectorStore:
    _load_builtin()
    cls = _VECTOR_STORE_TYPES.get(provider)
    if cls is None:
        raise ConfigurationError(
            f"Unknown vector store '{provider}'. Known: {', '.join(sorted(_VECTOR_STORE_TYPES))}"
        )
    return cls(settings)  # type: ignore[call-arg]
