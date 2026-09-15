"""Hybrid search endpoint.

Exposes retrieval directly, without an LLM. Useful for debugging what the agent
actually sees, and for a client that wants to render sources itself.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import CurrentPrincipal, Trace, Uow, get_retriever
from app.providers.search.base import SearchHit
from app.schemas.chat import SearchHitOut, SearchRequest, SearchResponse
from app.services.retrieval.service import Retriever

router = APIRouter(prefix="/search", tags=["search"])


@router.post("", response_model=SearchResponse)
async def search(
    payload: SearchRequest,
    principal: CurrentPrincipal,
    uow: Uow,
    trace: Trace,
    retriever: Retriever = Depends(get_retriever),
) -> SearchResponse:
    """Search the caller's knowledge base.

    With ``hierarchical`` (the default) the result is the merged, override-
    resolved set the agent would receive: location-specific passages ranked
    first, and organization passages they supersede removed. Turning it off
    returns the raw ranking, which is what you want when diagnosing why a
    particular passage did or did not surface.
    """
    tenant = principal.tenant.narrowed_to(payload.location_id)

    if payload.hierarchical:
        outcome = await retriever.retrieve(
            uow,
            tenant,
            queries=[payload.query],
            top_k=payload.top_k,
            document_types=payload.document_types,
            languages=payload.languages,
            trace=trace,
        )
        return SearchResponse(
            query=payload.query,
            hits=[_to_out(hit) for hit in outcome.hits],
            total=len(outcome.hits),
            took_ms=outcome.took_ms,
            suppressed_by_override=outcome.merged.suppressed_count,
            overridden_topics=sorted(outcome.merged.overrides),
            degraded=outcome.degraded,
        )

    result = await retriever.search_one_scope(
        uow, tenant, query=payload.query, top_k=payload.top_k, trace=trace
    )
    return SearchResponse(
        query=payload.query,
        hits=[_to_out(hit) for hit in result.hits],
        total=len(result.hits),
        took_ms=result.took_ms,
        degraded=result.degraded,
    )


def _to_out(hit: SearchHit) -> SearchHitOut:
    return SearchHitOut(
        chunk_id=hit.chunk_id,
        document_id=hit.document_id,
        document_version=hit.document_version,
        source=hit.source_name,
        scope=hit.scope.value,
        section=hit.breadcrumb or hit.heading,
        page_from=hit.page_from,
        page_to=hit.page_to,
        content=hit.content,
        score=round(hit.score, 6),
        vector_score=round(hit.vector_score, 6) if hit.vector_score is not None else None,
        keyword_score=round(hit.keyword_score, 6) if hit.keyword_score is not None else None,
        matched=list(hit.matched_arms),
    )
