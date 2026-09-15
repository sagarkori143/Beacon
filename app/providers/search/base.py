"""Search provider abstraction.

One interface, ``search(query, filters, top_k)``, regardless of whether the
implementation is PostgreSQL with pgvector or a dedicated vector database later.

The filter object is the load-bearing part. Tenant isolation is expressed here
and applied in SQL by the implementation -- it is never left to the LLM, never
applied after the fact in Python, and never optional.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypeVar
from uuid import UUID

from app.core.config import Settings
from app.core.enums import KnowledgeScope
from app.core.errors import ConfigurationError
from app.core.tracing import TraceContext
from app.providers.base import ProviderHealth


class ScopeMode(StrEnum):
    """Which slice of the tenant hierarchy a search may see.

    ``LOCATION_AND_ORG`` is the normal case for an end user: their location's
    knowledge plus the organization-wide knowledge shared by every location. The
    other modes exist so the hierarchical retriever can fetch each level
    separately and merge them with precedence.
    """

    LOCATION_AND_ORG = "location_and_org"
    LOCATION_ONLY = "location_only"
    ORG_ONLY = "org_only"
    ALL_LOCATIONS = "all_locations"


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """Everything that constrains a search. ``organization_id`` is mandatory."""

    organization_id: UUID
    location_id: UUID | None = None
    scope_mode: ScopeMode = ScopeMode.LOCATION_AND_ORG
    languages: tuple[str, ...] = ()
    document_types: tuple[str, ...] = ()
    document_ids: tuple[UUID, ...] = ()
    exclude_chunk_ids: tuple[UUID, ...] = ()
    #: Restrict to one version. Used by the post-ingestion smoke test, which must
    #: query a version that is not active yet.
    document_version_id: UUID | None = None
    #: Include chunks whose version is not active. Only the validation gate does.
    include_inactive: bool = False

    def for_scope(self, scope: KnowledgeScope) -> SearchFilters:
        """Narrow to exactly one level of the hierarchy."""
        mode = ScopeMode.LOCATION_ONLY if scope is KnowledgeScope.LOCATION else ScopeMode.ORG_ONLY
        return SearchFilters(
            organization_id=self.organization_id,
            location_id=self.location_id,
            scope_mode=mode,
            languages=self.languages,
            document_types=self.document_types,
            document_ids=self.document_ids,
            exclude_chunk_ids=self.exclude_chunk_ids,
            document_version_id=self.document_version_id,
            include_inactive=self.include_inactive,
        )

    @property
    def needs_document_join(self) -> bool:
        return bool(self.document_types)


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One retrieved chunk with its provenance and its scores.

    Both arms' raw scores and ranks are kept, not just the fused number, because
    "why did this rank here?" is the question you always end up asking, and
    because the trace shows it.
    """

    chunk_id: UUID
    document_id: UUID
    document_version_id: UUID
    document_version: int
    organization_id: UUID
    location_id: UUID | None

    content: str
    heading: str | None
    section_path: tuple[str, ...]
    section_key: str | None
    topic_key: str | None
    source_name: str
    source_type: str
    page_from: int | None
    page_to: int | None
    language: str
    token_count: int
    content_hash: str

    score: float = 0.0
    vector_score: float | None = None
    keyword_score: float | None = None
    vector_rank: int | None = None
    keyword_rank: int | None = None
    #: Applied by the hierarchical merge, not by the SQL.
    boost: float = 1.0

    @property
    def scope(self) -> KnowledgeScope:
        return KnowledgeScope.ORGANIZATION if self.location_id is None else KnowledgeScope.LOCATION

    @property
    def matched_arms(self) -> tuple[str, ...]:
        arms = []
        if self.vector_rank is not None:
            arms.append("vector")
        if self.keyword_rank is not None:
            arms.append("keyword")
        return tuple(arms)

    def with_score(self, score: float, *, boost: float | None = None) -> SearchHit:
        return SearchHit(
            **{
                **{f: getattr(self, f) for f in self.__slots__},
                "score": score,
                "boost": self.boost if boost is None else boost,
            }
        )

    @property
    def breadcrumb(self) -> str:
        return " > ".join(self.section_path)


@dataclass(slots=True)
class SearchResult:
    hits: list[SearchHit]
    query: str
    #: True when the vector arm under-returned and the wider retry was used.
    degraded: bool = False
    vector_candidates: int = 0
    keyword_candidates: int = 0
    took_ms: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.hits)

    def __iter__(self) -> Any:
        return iter(self.hits)


class SearchProvider(ABC):
    name: str

    @abstractmethod
    async def search(
        self,
        session: Any,
        *,
        query: str,
        embedding: Sequence[float] | None,
        filters: SearchFilters,
        top_k: int,
        trace: TraceContext | None = None,
    ) -> SearchResult:
        """Retrieve the ``top_k`` best chunks for ``query`` within ``filters``."""

    @abstractmethod
    async def health(self) -> ProviderHealth: ...

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

TSearch = TypeVar("TSearch", bound="type[SearchProvider]")

_SEARCH_TYPES: dict[str, type[SearchProvider]] = {}


def register_search_provider(type_name: str) -> Callable[[TSearch], TSearch]:
    def decorator(cls: TSearch) -> TSearch:
        _SEARCH_TYPES[type_name] = cls
        return cls

    return decorator


_loaded = False


def _load_builtin() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    from app.providers.search import postgres_hybrid  # noqa: F401


def build_search_provider(settings: Settings, provider: str = "postgres_hybrid") -> SearchProvider:
    _load_builtin()
    cls = _SEARCH_TYPES.get(provider)
    if cls is None:
        raise ConfigurationError(
            f"Unknown search provider '{provider}'. Known: {', '.join(sorted(_SEARCH_TYPES))}"
        )
    return cls(settings)  # type: ignore[call-arg]
