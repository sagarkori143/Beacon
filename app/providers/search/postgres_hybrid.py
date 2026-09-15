"""Hybrid search over PostgreSQL: pgvector ANN + full-text search, in one query.

Both arms run as CTEs in a single statement, so there is one round trip and both
see exactly the same tenant predicates. Fusion happens in SQL too -- pulling two
candidate sets into Python to merge them would double the data transferred and
make the ranking impossible to EXPLAIN.

The subtle problem this file exists to solve is **filtered-ANN recall**. An HNSW
index traverses the graph globally and the tenant predicate is applied to what
comes back, so a small tenant inside a large corpus can get two results for a
query that would return fifty unfiltered. Four things mitigate it, all here:

1. The HNSW index is partial on ``is_active``, so the graph contains only live
   chunks and none of the traversal is wasted on superseded versions.
2. ``hnsw.iterative_scan`` lets pgvector keep scanning until it has enough rows
   that survive the filter (pgvector >= 0.8; detected, not assumed).
3. ``ef_search`` is scaled to the requested candidate count, and the query is
   retried once at a much wider setting when the vector arm under-returns.
4. The lexical arm is a floor: GIN pre-filters correctly, so keyword matches are
   never lost to this effect.

A retry is recorded on the result and counted as a metric rather than being
silently absorbed, because silently degraded recall is exactly the kind of
problem that goes unnoticed for months.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.tracing import TraceContext
from app.providers.base import ProviderHealth
from app.providers.search.base import (
    ScopeMode,
    SearchFilters,
    SearchHit,
    SearchProvider,
    SearchResult,
    register_search_provider,
)
from app.providers.search.fusion import build_fusion

log = get_logger(__name__)

#: Language code -> PostgreSQL text-search configuration.
TS_CONFIG_BY_LANGUAGE: dict[str, str] = {
    "en": "english",
    "es": "spanish",
    "fr": "french",
    "de": "german",
    "it": "italian",
    "pt": "portuguese",
    "nl": "dutch",
    "ru": "russian",
    "sv": "swedish",
    "no": "norwegian",
    "da": "danish",
    "fi": "finnish",
    "tr": "turkish",
}

#: Languages PostgreSQL has no stemmer for. 'simple' still indexes them; it just
#: does not stem, which for CJK is the correct behaviour anyway.
_FALLBACK_TS_CONFIG = "simple"

#: Turns the parsed query's conjunctions into disjunctions.
#:
#: ``websearch_to_tsquery`` joins terms with AND, so "breakfast dining room
#: hours" only matches a chunk containing all four stems. Real questions rarely
#: do, and the lexical arm then returns nothing -- leaving "hybrid" search
#: quietly running on the vector arm alone, which is the exact failure this
#: architecture exists to avoid. Relaxing to OR lets ``ts_rank_cd`` do the
#: discriminating, which is what it is for.
#:
#: Phrase operators (``<->``) survive untouched, so quoted phrases still behave.
#: A query containing negation is left alone: relaxing ``a & !b`` to ``a | !b``
#: would match nearly everything.
_STRICT_TSQUERY = """
    SELECT websearch_to_tsquery(CAST(:ts_config AS regconfig), :q) AS query
"""

_RELAX_TSQUERY = """
    SELECT CASE
             WHEN strpos(raw::text, '!') > 0 THEN raw
             ELSE replace(raw::text, ' & ', ' | ')::tsquery
           END AS query
    FROM (SELECT websearch_to_tsquery(CAST(:ts_config AS regconfig), :q) AS raw) parsed
"""

_SELECT_COLUMNS = """
    c.id                      AS chunk_id,
    c.document_id             AS document_id,
    c.document_version_id     AS document_version_id,
    c.document_version        AS document_version,
    c.organization_id         AS organization_id,
    c.location_id             AS location_id,
    c.content                 AS content,
    c.heading                 AS heading,
    c.section_path            AS section_path,
    c.section_key             AS section_key,
    c.topic_key               AS topic_key,
    c.source_name             AS source_name,
    c.source_type             AS source_type,
    c.page_from               AS page_from,
    c.page_to                 AS page_to,
    c.language                AS language,
    c.token_count             AS token_count,
    c.content_hash            AS content_hash
"""


@register_search_provider("postgres_hybrid")
class PostgresHybridSearch(SearchProvider):
    name = "postgres_hybrid"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.retrieval = settings.retrieval
        self.fusion = build_fusion(settings.retrieval)
        #: Populated on first use; None means "not yet checked".
        self._supports_iterative_scan: bool | None = None

    # -- public API ----------------------------------------------------------

    async def search(
        self,
        session: AsyncSession,
        *,
        query: str,
        embedding: Sequence[float] | None,
        filters: SearchFilters,
        top_k: int,
        trace: TraceContext | None = None,
    ) -> SearchResult:
        started = time.perf_counter()
        candidate_k = max(top_k * 4, self.retrieval.candidate_k)

        result = await self._run(
            session,
            query=query,
            embedding=embedding,
            filters=filters,
            top_k=top_k,
            candidate_k=candidate_k,
            ef_search=max(self.retrieval.ef_search, candidate_k * 2),
        )

        # Fallback for pgvector < 0.8, which has no iterative scan: retry with a
        # much wider probe when the ANN arm lost most of its candidates to the
        # tenant filter.
        #
        # On 0.8+ this is redundant -- `hnsw.iterative_scan` already keeps
        # scanning until enough rows survive the filter -- so the extra query is
        # skipped rather than run on every request.
        needs_manual_retry = (
            embedding is not None
            and self._supports_iterative_scan is False
            and result.vector_candidates < candidate_k // 2
            and result.vector_candidates < top_k
        )
        if needs_manual_retry:
            wider = max(self.retrieval.ef_search, candidate_k * 2) * (
                self.retrieval.ef_search_retry_multiplier
            )
            retried = await self._run(
                session,
                query=query,
                embedding=embedding,
                filters=filters,
                top_k=top_k,
                candidate_k=candidate_k,
                ef_search=wider,
            )
            # Only a retry that actually recovers rows means the first pass lost
            # recall. Returning the same count means the corpus simply has fewer
            # matching chunks -- complete recall, not degraded. Flagging that as
            # degraded would make the signal meaningless for small tenants,
            # which is most of them.
            if retried.vector_candidates > result.vector_candidates:
                log.info(
                    "search_recall_recovered",
                    organization_id=str(filters.organization_id),
                    first_pass=result.vector_candidates,
                    after_retry=retried.vector_candidates,
                    ef_search=wider,
                )
                retried.degraded = True
                result = retried

        result.took_ms = (time.perf_counter() - started) * 1000.0
        if trace is not None:
            with trace.span(
                "search.hybrid",
                strategy=self.fusion.name,
                hits=len(result.hits),
                vector_candidates=result.vector_candidates,
                keyword_candidates=result.keyword_candidates,
                degraded=result.degraded,
            ):
                pass
        return result

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            name=self.name,
            ok=True,
            extra={
                "fusion": self.fusion.name,
                "iterative_scan": self._supports_iterative_scan,
            },
        )

    # -- execution -----------------------------------------------------------

    async def _run(
        self,
        session: AsyncSession,
        *,
        query: str,
        embedding: Sequence[float] | None,
        filters: SearchFilters,
        top_k: int,
        candidate_k: int,
        ef_search: int,
    ) -> SearchResult:
        await self._apply_index_settings(session, ef_search)

        predicates, params = self._build_predicates(filters)
        ts_config = self._ts_config(filters)

        params.update(
            {
                "q": query,
                "ts_config": ts_config,
                "candidate_k": candidate_k,
                "top_k": top_k,
                **self.fusion.bind_params(),
            }
        )

        has_vector = embedding is not None
        if has_vector:
            # Bound as text and cast in SQL: pinning the parameter type to
            # text means the query does not depend on pgvector's asyncpg
            # codec being registered on this particular connection.
            params["qvec"] = "[" + ",".join(f"{v:.8g}" for v in embedding) + "]"

        sql = self._build_sql(
            predicates=predicates,
            has_vector=has_vector,
            needs_document_join=filters.needs_document_join,
            normalized=self.fusion.needs_normalized_scores,
            relax_lexical=self.retrieval.lexical_relax_to_or,
        )

        statement = text(sql)
        if filters.exclude_chunk_ids:
            statement = statement.bindparams(bindparam("excluded_ids", expanding=True))
        if filters.document_ids:
            statement = statement.bindparams(bindparam("document_ids", expanding=True))
        if filters.languages:
            statement = statement.bindparams(bindparam("languages", expanding=True))
        if filters.document_types:
            statement = statement.bindparams(bindparam("document_types", expanding=True))

        rows = (await session.execute(statement, params)).mappings().all()

        hits = [self._hit_from_row(row) for row in rows]
        return SearchResult(
            hits=hits,
            query=query,
            vector_candidates=int(rows[0]["vector_candidates"]) if rows else 0,
            keyword_candidates=int(rows[0]["keyword_candidates"]) if rows else 0,
            meta={"ef_search": ef_search, "ts_config": ts_config},
        )

    async def _apply_index_settings(self, session: AsyncSession, ef_search: int) -> None:
        """Tune the ANN probe for this transaction only."""
        if self._supports_iterative_scan is None:
            version = (
                await session.execute(
                    text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                )
            ).scalar_one_or_none()
            self._supports_iterative_scan = _version_at_least(version, (0, 8))
            log.info(
                "pgvector_capabilities",
                version=version,
                iterative_scan=self._supports_iterative_scan,
            )

        await session.execute(
            text("SELECT set_config('hnsw.ef_search', :v, true)"), {"v": str(ef_search)}
        )
        if self._supports_iterative_scan:
            # 'relaxed_order' keeps scanning past the first ef_search candidates
            # when the filter rejects them, at the cost of approximate ordering
            # within the batch -- which fusion re-orders anyway.
            await session.execute(
                text("SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true)")
            )

    # -- SQL construction ----------------------------------------------------

    def _build_predicates(self, filters: SearchFilters) -> tuple[list[str], dict[str, Any]]:
        """Tenant and metadata predicates, applied identically to both arms.

        Organization scope is unconditional and first: it is the predicate that
        makes cross-tenant retrieval impossible, and it is never derived from
        anything a client sent.
        """
        predicates = ["c.organization_id = :organization_id"]
        params: dict[str, Any] = {"organization_id": filters.organization_id}

        if not filters.include_inactive:
            predicates.append("c.is_active")

        match filters.scope_mode:
            case ScopeMode.LOCATION_AND_ORG:
                if filters.location_id is not None:
                    predicates.append("(c.location_id = :location_id OR c.location_id IS NULL)")
                    params["location_id"] = filters.location_id
                else:
                    predicates.append("c.location_id IS NULL")
            case ScopeMode.LOCATION_ONLY:
                if filters.location_id is None:
                    # No location means no location-scoped knowledge exists for
                    # this caller; make that explicit instead of silently
                    # widening to the whole organization.
                    predicates.append("FALSE")
                else:
                    predicates.append("c.location_id = :location_id")
                    params["location_id"] = filters.location_id
            case ScopeMode.ORG_ONLY:
                predicates.append("c.location_id IS NULL")
            case ScopeMode.ALL_LOCATIONS:
                pass

        if filters.document_version_id is not None:
            predicates.append("c.document_version_id = :document_version_id")
            params["document_version_id"] = filters.document_version_id

        if filters.languages:
            predicates.append("c.language IN :languages")
            params["languages"] = list(filters.languages)

        if filters.document_ids:
            predicates.append("c.document_id IN :document_ids")
            params["document_ids"] = list(filters.document_ids)

        if filters.document_types:
            predicates.append("d.document_type IN :document_types")
            params["document_types"] = list(filters.document_types)

        if filters.exclude_chunk_ids:
            predicates.append("c.id NOT IN :excluded_ids")
            params["excluded_ids"] = list(filters.exclude_chunk_ids)

        return predicates, params

    # ruff: noqa: S608 - the only interpolated fragments are predicate strings
    # this class builds itself from a fixed vocabulary. Every caller-supplied
    # value, including the tenant id and the query text, is a bound parameter.
    def _build_sql(
        self,
        *,
        predicates: list[str],
        has_vector: bool,
        needs_document_join: bool,
        normalized: bool,
        relax_lexical: bool = True,
    ) -> str:
        where = " AND ".join(predicates)
        join = (
            "JOIN documents d ON d.id = c.document_id AND d.is_deleted = FALSE"
            if needs_document_join
            else ""
        )

        norm_expr = (
            "COALESCE((score - min(score) OVER ()) "
            "/ NULLIF(max(score) OVER () - min(score) OVER (), 0), 1.0)"
        )

        if has_vector:
            vector_cte = f"""
            vec_raw AS (
                SELECT c.id AS chunk_id,
                       1 - (c.embedding <=> CAST(CAST(:qvec AS text) AS vector)) AS score
                FROM chunks c
                {join}
                WHERE {where}
                  AND c.embedding IS NOT NULL
                ORDER BY c.embedding <=> CAST(CAST(:qvec AS text) AS vector)
                LIMIT :candidate_k
            ),
            vec AS (
                SELECT chunk_id,
                       score,
                       row_number() OVER (ORDER BY score DESC) AS rank
                       {f", {norm_expr} AS norm" if normalized else ""}
                FROM vec_raw
            )"""
        else:
            vector_cte = """
            vec AS (
                SELECT NULL::uuid AS chunk_id, NULL::float AS score, NULL::bigint AS rank
                       {norm}
                WHERE FALSE
            )""".replace("{norm}", ", NULL::float AS norm" if normalized else "")

        keyword_cte = f"""
            tsq AS ({_RELAX_TSQUERY if relax_lexical else _STRICT_TSQUERY}),
            kw_raw AS (
                SELECT c.id AS chunk_id,
                       ts_rank_cd(c.search_vector, tsq.query, 32) AS score
                FROM chunks c
                {join}
                CROSS JOIN tsq
                WHERE {where}
                  AND c.search_vector @@ tsq.query
                ORDER BY score DESC
                LIMIT :candidate_k
            ),
            kw AS (
                SELECT chunk_id,
                       score,
                       row_number() OVER (ORDER BY score DESC) AS rank
                       {f", {norm_expr} AS norm" if normalized else ""}
                FROM kw_raw
            )"""

        norm_columns = ", v.norm AS vector_norm, k.norm AS keyword_norm" if normalized else ""

        return f"""
        WITH {vector_cte},
        {keyword_cte},
        fused AS (
            SELECT chunk_id,
                   v.score AS vector_score,
                   v.rank  AS vector_rank,
                   k.score AS keyword_score,
                   k.rank  AS keyword_rank
                   {norm_columns}
            FROM vec v
            FULL OUTER JOIN kw k USING (chunk_id)
        )
        SELECT
            {_SELECT_COLUMNS},
            f.vector_score,
            f.vector_rank,
            f.keyword_score,
            f.keyword_rank,
            {self.fusion.sql_expression()} AS fused_score,
            count(*) FILTER (WHERE f.vector_rank IS NOT NULL) OVER ()  AS vector_candidates,
            count(*) FILTER (WHERE f.keyword_rank IS NOT NULL) OVER () AS keyword_candidates
        FROM fused f
        JOIN chunks c ON c.id = f.chunk_id
        ORDER BY fused_score DESC, c.document_version DESC, c.ordinal ASC
        LIMIT :top_k
        """

    def _ts_config(self, filters: SearchFilters) -> str:
        """Pick the text-search configuration for the query.

        Uses the requested language when exactly one is filtered; otherwise the
        configured default. Stemming with the wrong language is worse than not
        stemming, so an ambiguous case falls back rather than guessing.
        """
        if len(filters.languages) == 1:
            return TS_CONFIG_BY_LANGUAGE.get(filters.languages[0], _FALLBACK_TS_CONFIG)
        return self.retrieval.default_text_search_config

    @staticmethod
    def _hit_from_row(row: Any) -> SearchHit:
        section_path = row["section_path"] or []
        return SearchHit(
            chunk_id=row["chunk_id"],
            document_id=row["document_id"],
            document_version_id=row["document_version_id"],
            document_version=row["document_version"],
            organization_id=row["organization_id"],
            location_id=row["location_id"],
            content=row["content"],
            heading=row["heading"],
            section_path=tuple(section_path),
            section_key=row["section_key"],
            topic_key=row["topic_key"],
            source_name=row["source_name"],
            source_type=str(row["source_type"]),
            page_from=row["page_from"],
            page_to=row["page_to"],
            language=row["language"],
            token_count=row["token_count"] or 0,
            content_hash=row["content_hash"],
            score=float(row["fused_score"] or 0.0),
            vector_score=(float(row["vector_score"]) if row["vector_score"] is not None else None),
            keyword_score=(
                float(row["keyword_score"]) if row["keyword_score"] is not None else None
            ),
            vector_rank=int(row["vector_rank"]) if row["vector_rank"] is not None else None,
            keyword_rank=int(row["keyword_rank"]) if row["keyword_rank"] is not None else None,
        )


def _version_at_least(version: str | None, minimum: tuple[int, ...]) -> bool:
    if not version:
        return False
    try:
        parts = tuple(int(p) for p in version.split(".")[: len(minimum)])
    except ValueError:
        return False
    return parts >= minimum


def ts_config_for(language: str, default: str = "english") -> str:
    """Public helper: the indexing stage must use the same mapping as search."""
    return TS_CONFIG_BY_LANGUAGE.get(
        language, default if language.startswith("en") else _FALLBACK_TS_CONFIG
    )
