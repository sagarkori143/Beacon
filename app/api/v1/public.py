"""The public site: anyone may ask any listed organization a question.

Nobody signs in here. That is a real departure from the rest of the API, where
tenant scope comes from a token and never from a request parameter, so it is
worth being exact about what does and does not change.

**What changes** is authentication: the organization comes from the URL slug.

**What does not change** is scoping. One request still touches exactly one
organization. The slug is resolved to a single organization, the session is
opened scoped to that one, and the principal carries no location -- which
``Retriever._levels`` already reads as organization-wide knowledge only, with no
path into any one branch's private material. Nothing here can see two tenants.

**What this does expose** is every listed organization's active knowledge, to
anyone who can reach the site. That is the intended product behaviour, and
``Organization.is_public`` is the switch that governs it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.api.deps import AppSettings, Trace, get_agent, get_app_settings, get_redis_client
from app.api.v1.chat_support import SSE_HEADERS, build_agent_request, to_chat_response
from app.core.config import Settings
from app.core.db import UnitOfWork, unscoped_session
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.core.tenancy import TenantContext, public_principal
from app.models.organization import Organization
from app.schemas.chat import ChatRequest, ChatResponse
from app.schemas.public import PublicOrganizationOut
from app.services.agent.guardrails import check_public_rate_limit
from app.services.agent.memory import ConversationMemory
from app.services.agent.runtime import AgentResult, AgentRuntime

log = get_logger(__name__)

router = APIRouter(prefix="/public", tags=["public"])


def _client_address(request: Request) -> str:
    """Who to rate-limit, behind a proxy or not.

    ``X-Forwarded-For`` is trusted only for its first entry, and only because
    this sits behind a proxy the operator controls. It is a rate-limit bucket,
    not an authorisation decision -- the worst a spoofed value achieves is
    getting someone else's bucket.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else "unknown"


async def _resolve_public_organization(slug: str, settings: Settings) -> Organization:
    """Look up a listed organization by slug.

    Runs without tenant context on purpose: the caller has no organization yet,
    and this is how they get one. ``organizations`` is the tenant-root table and
    its policy admits a session scoped to its own row or a platform operator, so
    this read is done before any tenant session exists -- the query itself is
    restricted to the columns a visitor may see.

    An organization that is not public is a 404 rather than a 403: confirming
    that a private tenant exists is itself something a visitor should not learn.
    """
    async with unscoped_session(settings) as session:
        result = await session.execute(
            select(
                Organization.id,
                Organization.name,
                Organization.slug,
            ).where(
                Organization.slug == slug,
                Organization.is_active.is_(True),
                Organization.is_public.is_(True),
            )
        )
        row = result.one_or_none()

    if row is None:
        raise NotFoundError(f"No organization is published at '{slug}'")
    return row  # type: ignore[return-value]


@router.get("/organizations", response_model=list[PublicOrganizationOut])
async def list_public_organizations(
    settings: AppSettings,
) -> list[PublicOrganizationOut]:
    """Every organization a visitor may ask about.

    Names and slugs only. Nothing here reveals what any of them know.
    """
    async with unscoped_session(settings) as session:
        result = await session.execute(
            select(Organization.id, Organization.name, Organization.slug)
            .where(Organization.is_active.is_(True), Organization.is_public.is_(True))
            .order_by(Organization.name)
        )
        rows = result.all()

    return [PublicOrganizationOut(id=r.id, name=r.name, slug=r.slug) for r in rows]


@router.get("/organizations/{slug}", response_model=PublicOrganizationOut)
async def get_public_organization(slug: str, settings: AppSettings) -> PublicOrganizationOut:
    organization = await _resolve_public_organization(slug, settings)
    return PublicOrganizationOut(id=organization.id, name=organization.name, slug=organization.slug)


@router.post("/organizations/{slug}/chat", response_model=ChatResponse)
async def public_chat(
    slug: str,
    payload: ChatRequest,
    request: Request,
    trace: Trace,
    settings: AppSettings,
    agent: AgentRuntime = Depends(get_agent),
    redis=Depends(get_redis_client),
) -> ChatResponse:
    """Ask a listed organization a question and wait for the answer."""
    agent_request, memory, conversation_id, principal = await _prepare(
        slug, payload, request, settings, redis, stream=False
    )
    trace.organization_id = principal.organization_id

    result = await agent.run(agent_request, trace=trace)
    await _remember(memory, principal, conversation_id, payload.message, result)

    # include_trace is ignored on purpose: routing decisions, provider names and
    # retrieval internals are operational detail, not public information.
    return to_chat_response(result, conversation_id, include_trace=False)


@router.post("/organizations/{slug}/chat/stream")
async def public_chat_stream(
    slug: str,
    payload: ChatRequest,
    request: Request,
    trace: Trace,
    settings: AppSettings,
    agent: AgentRuntime = Depends(get_agent),
    redis=Depends(get_redis_client),
) -> StreamingResponse:
    """The same answer, streamed as it is produced."""
    agent_request, memory, conversation_id, principal = await _prepare(
        slug, payload, request, settings, redis, stream=True
    )
    trace.organization_id = principal.organization_id
    result = AgentResult(answer="")

    async def event_stream() -> AsyncIterator[str]:
        try:
            async for event in agent.run_stream(agent_request, trace=trace, sink=result):
                if await request.is_disconnected():
                    log.info("public_chat_disconnected", trace_id=trace.trace_id)
                    return
                if event.type == "done":
                    event.data["conversation_id"] = str(conversation_id)
                yield event.to_sse()
        finally:
            if result.answer:
                await _remember(memory, principal, conversation_id, payload.message, result)

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=SSE_HEADERS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _prepare(
    slug: str,
    payload: ChatRequest,
    request: Request,
    settings: Settings,
    redis,
    *,
    stream: bool,
):
    """Resolve the organization, admit the caller, and build the agent request."""
    organization = await _resolve_public_organization(slug, settings)

    await check_public_rate_limit(
        redis,
        organization_id=organization.id,
        client=_client_address(request),
        limit_per_minute=settings.agent.public_rate_limit_per_minute,
    )

    principal = public_principal(organization.id)
    uow = UnitOfWork(TenantContext(organization_id=organization.id), settings)

    agent_request, memory, conversation_id = await build_agent_request(
        payload, principal, uow, settings, redis, stream=stream
    )
    return agent_request, memory, conversation_id, principal


async def _remember(
    memory: ConversationMemory,
    principal,
    conversation_id: uuid.UUID,
    question: str,
    result: AgentResult,
) -> None:
    """Keep the turn in Redis only, so follow-up questions have context.

    Deliberately no ``conversations`` row. Those carry a ``user_id`` pointing at
    a real person, and inventing one for an anonymous visitor would mean either
    a nullable foreign key or fake user rows. Storing anonymous transcripts
    indefinitely is also a liability nobody asked for -- Redis expires them.
    """
    await memory.append(principal.organization_id, conversation_id, role="user", content=question)
    await memory.append(
        principal.organization_id, conversation_id, role="assistant", content=result.answer
    )


__all__ = ["get_app_settings", "router"]
