"""FastAPI application.

Startup builds every provider from configuration and runs the boot guards. The
guiding rule for what stops startup and what merely warns:

* A **misconfiguration that would silently corrupt data** stops the process. The
  embedding-space guard is the one that matters -- starting with a mismatched
  model writes incomparable vectors and degrades retrieval with no error at all.
* An **unreachable dependency degrades** instead. A model server on someone's
  GPU box will be down sometimes; that must not take the API down with it.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import install_error_handlers
from app.api.middleware.request_id import RequestContextMiddleware
from app.api.router import build_api_router, health_router
from app.core.config import Settings, get_settings
from app.core.db import (
    check_application_role,
    dispose_engine,
    init_engine,
    unscoped_session,
)
from app.core.errors import ConfigurationError, EmbeddingSpaceMismatch
from app.core.logging import configure_logging, get_logger
from app.core.redis import close_redis, init_redis
from app.providers.registry import build_providers
from app.repositories.embedding_space import verify_embedding_space
from app.services.agent.router import ModelRouter
from app.services.agent.runtime import AgentRuntime
from app.services.auth.service import AuthService
from app.services.documents.service import DocumentService
from app.services.platform.service import PlatformService
from app.services.retrieval.service import Retriever
from app.tools.registry import build_default_registry

log = get_logger(__name__)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    init_engine(settings)
    redis = init_redis(settings)
    # No OCR in the API: uploads are parsed by the worker, and the API image
    # has no OCR engine installed.
    providers = build_providers(settings, redis=redis, include_ocr=False)

    await _verify_application_role(settings)
    await _verify_embedding_space(settings, providers)

    tools = build_default_registry(max_result_tokens=settings.context.max_tool_result_tokens)
    router = ModelRouter(providers, settings)
    retriever = Retriever(
        settings=settings,
        search=providers.require_search(),
        embeddings=providers.require_embeddings(),
    )

    app.state.settings = settings
    app.state.redis = redis
    app.state.providers = providers
    app.state.tools = tools
    app.state.model_router = router
    app.state.retriever = retriever
    app.state.auth_service = AuthService(settings)
    app.state.document_service = DocumentService(settings, providers)
    app.state.platform_service = PlatformService(settings)
    app.state.agent = AgentRuntime(
        settings=settings,
        providers=providers,
        router=router,
        retriever=retriever,
        tools=tools,
        redis=redis,
    )

    with contextlib.suppress(Exception):
        # Warm the queue's consumer group so the first upload does not race its
        # creation. Failure here is not fatal: the worker creates it too.
        await providers.require_queue().setup()

    log.info(
        "application_started",
        env=settings.app_env,
        providers=sorted(providers.llm),
        tools=len(tools),
    )

    try:
        yield
    finally:
        await providers.aclose()
        await close_redis()
        await dispose_engine()
        log.info("application_stopped")


async def _verify_application_role(settings: Settings) -> None:
    """Refuse to serve production traffic with RLS disabled.

    In production this is fatal, because the alternative is running with every
    tenant boundary silently open. Outside production it is a loud warning:
    someone running migrations or a script against a scratch database with the
    owner role should be told, not blocked.
    """
    result = await check_application_role(settings)

    if result.get("ok"):
        log.info("application_role_verified", role=result.get("role"))
        return

    if "error" in result and result.get("bypasses_rls") is None:
        log.warning("application_role_unverified", error=result["error"])
        return

    message = result.get("error", "application role check failed")
    if settings.is_production:
        raise ConfigurationError(message)
    log.error("application_role_unsafe", role=result.get("role"), detail=message)


async def _verify_embedding_space(settings: Settings, providers: object) -> None:
    """Refuse to start on an embedding-space mismatch.

    This is the one startup check that hard-fails. Every other failure mode is
    loud; this one is silent -- vectors from the wrong model are the right shape,
    insert cleanly, and simply return worse answers forever.
    """
    embeddings = getattr(providers, "embeddings", None)
    if embeddings is None:
        log.warning("embedding_space_unverified", reason="no embedding provider configured")
        return

    try:
        async with unscoped_session(settings) as session:
            space = await verify_embedding_space(
                session, embeddings, configured_dimension=settings.embedding_dim
            )
        log.info("embedding_space_verified", space=space.describe())
    except EmbeddingSpaceMismatch:
        raise
    except Exception as exc:  # noqa: BLE001 - a database not yet migrated is fine
        log.warning(
            "embedding_space_check_skipped",
            error=str(exc)[:200],
            hint="run migrations; the check runs again on next start",
        )


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    app = FastAPI(
        title="Enterprise AI Agent & Runtime",
        version="0.1.0",
        description=(
            "Multi-tenant RAG and agent backend. Organization-wide knowledge with "
            "location-specific overrides, hybrid retrieval, document versioning, "
            "and pluggable LLM/embedding/OCR/storage/queue providers."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.security.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Trace-ID"],
    )

    install_error_handlers(app)
    app.include_router(health_router)
    app.include_router(build_api_router(settings.api_prefix))

    return app


app = create_app()
