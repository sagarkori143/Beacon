"""The retriever: query in, ranked tenant-scoped passages out.

Orchestrates embedding, the hybrid search arms at each level of the hierarchy,
fusion across query rewrites, and the hierarchical merge. Everything above this
(the agent, the /search endpoint, the knowledge_search tool) uses one call.

Database work is deliberately short-lived: the embedding call happens *before*
any transaction opens, and the transaction closes before the caller does
anything slow with the results.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

from app.core.config import Settings
from app.core.db import UnitOfWork
from app.core.enums import KnowledgeScope
from app.core.logging import get_logger
from app.core.tenancy import TenantContext
from app.core.tracing import TraceContext
from app.providers.embeddings.base import EmbeddingProvider
from app.providers.search.base import (
    ScopeMode,
    SearchFilters,
    SearchHit,
    SearchProvider,
    SearchResult,
)
from app.providers.search.fusion import build_fusion
from app.services.retrieval.hierarchical import MergedResults, ScopedResults, merge_scoped

log = get_logger(__name__)


@dataclass(slots=True)
class RetrievalOutcome:
    hits: list[SearchHit]
    merged: MergedResults
    queries: tuple[str, ...]
    degraded: bool = False
    took_ms: float = 0.0
    per_scope: dict[str, int] = field(default_factory=dict)

    def trace(self) -> dict[str, object]:
        return {
            "queries": list(self.queries),
            "hits": len(self.hits),
            "degraded": self.degraded,
            "took_ms": round(self.took_ms, 1),
            "per_scope": self.per_scope,
            **self.merged.trace(),
        }


class Retriever:
    def __init__(
        self,
        *,
        settings: Settings,
        search: SearchProvider,
        embeddings: EmbeddingProvider,
    ) -> None:
        self.settings = settings
        self.retrieval = settings.retrieval
        self.search = search
        self.embeddings = embeddings
        self.fusion = build_fusion(settings.retrieval)

    async def retrieve(
        self,
        uow: UnitOfWork,
        tenant: TenantContext,
        *,
        queries: Sequence[str],
        top_k: int | None = None,
        document_types: Sequence[str] = (),
        languages: Sequence[str] = (),
        document_ids: Sequence[UUID] = (),
        trace: TraceContext | None = None,
    ) -> RetrievalOutcome:
        """Retrieve for one or more phrasings of the same question."""
        started = time.perf_counter()
        top_k = top_k or self.retrieval.top_k
        unique_queries = tuple(dict.fromkeys(q.strip() for q in queries if q.strip()))
        if not unique_queries:
            return RetrievalOutcome(hits=[], merged=MergedResults(hits=[]), queries=())

        # Embed first, outside any transaction: this is a network call to the
        # model server and must never be made with a database session held.
        embeddings = await self._embed(unique_queries, trace)

        base_filters = SearchFilters(
            organization_id=tenant.organization_id,
            location_id=tenant.location_id,
            languages=tuple(languages),
            document_types=tuple(document_types),
            document_ids=tuple(document_ids),
        )
        levels = self._levels(tenant)

        per_scope_results: list[tuple[KnowledgeScope, list[SearchHit]]] = []
        degraded = False

        async with uow.begin() as session:
            for scope in levels:
                filters = base_filters.for_scope(scope)
                ranked_lists: list[list[SearchHit]] = []
                for query, embedding in zip(unique_queries, embeddings, strict=True):
                    result: SearchResult = await self.search.search(
                        session,
                        query=query,
                        embedding=embedding,
                        filters=filters,
                        # Over-fetch per level: the merge discards overridden
                        # passages, and under-fetching here would leave the
                        # final set short after suppression.
                        top_k=top_k * 2,
                        trace=trace,
                    )
                    degraded = degraded or result.degraded
                    ranked_lists.append(result.hits)

                fused = (
                    self.fusion.combine_lists(ranked_lists)
                    if len(ranked_lists) > 1
                    else (ranked_lists[0] if ranked_lists else [])
                )
                per_scope_results.append((scope, fused))

        merged = merge_scoped(
            [ScopedResults(scope=scope, hits=hits) for scope, hits in per_scope_results],
            self.retrieval,
            top_k=top_k,
        )

        outcome = RetrievalOutcome(
            hits=merged.hits,
            merged=merged,
            queries=unique_queries,
            degraded=degraded,
            took_ms=(time.perf_counter() - started) * 1000.0,
            per_scope={scope.value: len(hits) for scope, hits in per_scope_results},
        )
        log.info("retrieval_complete", **outcome.trace())
        return outcome

    async def search_one_scope(
        self,
        uow: UnitOfWork,
        tenant: TenantContext,
        *,
        query: str,
        scope_mode: ScopeMode = ScopeMode.LOCATION_AND_ORG,
        top_k: int | None = None,
        include_inactive: bool = False,
        document_version_id: UUID | None = None,
        trace: TraceContext | None = None,
    ) -> SearchResult:
        """A single flat search with no hierarchical merge.

        Used by the /search endpoint and by the post-ingestion smoke test, which
        must query a version that is not active yet.
        """
        embedding = await self.embeddings.embed_query(query, trace=trace)
        filters = SearchFilters(
            organization_id=tenant.organization_id,
            location_id=tenant.location_id,
            scope_mode=scope_mode,
            include_inactive=include_inactive,
            document_version_id=document_version_id,
        )
        async with uow.begin() as session:
            return await self.search.search(
                session,
                query=query,
                embedding=embedding,
                filters=filters,
                top_k=top_k or self.retrieval.top_k,
                trace=trace,
            )

    # -- internals -----------------------------------------------------------

    def _levels(self, tenant: TenantContext) -> list[KnowledgeScope]:
        """Which levels to query, most specific first.

        A caller with no location sees organization knowledge only -- there is no
        "search every location" fallback, because that is how one location's
        private information reaches another's guest.
        """
        if tenant.location_id is None:
            return [KnowledgeScope.ORGANIZATION]
        return [KnowledgeScope.LOCATION, KnowledgeScope.ORGANIZATION]

    async def _embed(self, queries: Sequence[str], trace: TraceContext | None) -> list[list[float]]:
        if trace is not None:
            with trace.span("embed.query", count=len(queries), model=self.embeddings.model):
                return await self._embed_all(queries, trace)
        return await self._embed_all(queries, trace)

    async def _embed_all(
        self, queries: Sequence[str], trace: TraceContext | None
    ) -> list[list[float]]:
        if len(queries) == 1:
            return [await self.embeddings.embed_query(queries[0], trace=trace)]
        # embed_documents batches; queries need the query-side prefix, so they
        # go one at a time but concurrently within the provider's own limits.
        import asyncio

        return list(
            await asyncio.gather(*(self.embeddings.embed_query(q, trace=trace) for q in queries))
        )
