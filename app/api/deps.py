"""FastAPI dependencies.

The important one is :func:`current_principal`. Tenant scope is derived from the
bearer token and nothing else -- no request body, query parameter or header can
influence which organization a request operates on. Endpoints may narrow within
that scope; nothing can widen it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request
from redis.asyncio import Redis

from app.core.config import Settings, get_settings
from app.core.db import UnitOfWork
from app.core.errors import AuthenticationError
from app.core.tenancy import Principal
from app.core.tracing import TraceContext
from app.providers.registry import ProviderBundle
from app.services.agent.router import ModelRouter
from app.services.agent.runtime import AgentRuntime
from app.services.auth.service import AuthService
from app.services.documents.service import DocumentService
from app.services.retrieval.service import Retriever
from app.tools.registry import ToolRegistry


def get_app_settings() -> Settings:
    return get_settings()


def get_providers(request: Request) -> ProviderBundle:
    return request.app.state.providers


def get_redis_client(request: Request) -> Redis:
    return request.app.state.redis


def get_router(request: Request) -> ModelRouter:
    return request.app.state.model_router


def get_retriever(request: Request) -> Retriever:
    return request.app.state.retriever


def get_tools(request: Request) -> ToolRegistry:
    return request.app.state.tools


def get_agent(request: Request) -> AgentRuntime:
    return request.app.state.agent


def get_auth_service(request: Request) -> AuthService:
    return request.app.state.auth_service


def get_document_service(request: Request) -> DocumentService:
    return request.app.state.document_service


def get_trace(request: Request) -> TraceContext:
    """The trace created by the request-id middleware."""
    return request.state.trace


async def current_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    auth: AuthService = Depends(get_auth_service),
) -> Principal:
    """Resolve the bearer token to a principal, or reject the request."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthenticationError("Missing bearer token")

    principal = await auth.authenticate(authorization[7:].strip())

    # Bind the tenant onto the trace so every log line for this request carries
    # it without each handler having to remember.
    trace: TraceContext = request.state.trace
    trace.organization_id = principal.organization_id
    trace.user_id = principal.user_id
    return principal


async def current_admin(
    principal: Principal = Depends(current_principal),
) -> Principal:
    principal.require_admin()
    return principal


def get_uow(
    principal: Principal = Depends(current_principal),
    settings: Settings = Depends(get_app_settings),
) -> UnitOfWork:
    """A UnitOfWork already scoped to the caller's organization.

    Handlers receive this rather than a session, so no endpoint can hold a
    transaction open across a model call. Sessions are opened per interaction
    inside ``async with uow.begin()``.
    """
    return UnitOfWork(principal.tenant, settings)


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]
CurrentAdmin = Annotated[Principal, Depends(current_admin)]
Uow = Annotated[UnitOfWork, Depends(get_uow)]
AppSettings = Annotated[Settings, Depends(get_app_settings)]
Providers = Annotated[ProviderBundle, Depends(get_providers)]
Trace = Annotated[TraceContext, Depends(get_trace)]
