"""Shared test fixtures.

Unit tests run with no external services. Integration and e2e tests need
PostgreSQL and Redis and are skipped -- not failed -- when those are absent, so
``pytest`` is useful on a laptop with nothing running.

The model server is never required. Every test uses the deterministic fake LLM
and embedding providers, which implement the same interfaces as the real ones,
so the code under test takes the production path.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import (
    EmbeddingProviderConfig,
    LLMProviderConfig,
    ModelConfig,
    QueueSettings,
    Settings,
    StorageSettings,
)
from app.core.db import UnitOfWork
from app.core.enums import ModelTier, Role
from app.core.logging import configure_logging
from app.core.tenancy import (
    Principal,
    TenantContext,
    scopes_for,
    system_principal,
)
from app.core.tracing import TraceContext
from app.providers.embeddings.fake import FakeEmbeddingProvider
from app.providers.llm.fake import FakeLLMProvider
from app.providers.queue.memory import MemoryQueue
from app.providers.registry import ProviderBundle
from app.providers.search.base import build_search_provider
from app.providers.storage.memory import MemoryStorageProvider
from app.providers.vector_store.base import build_vector_store
from app.services.documents.service import DocumentService
from app.services.ingestion.pipeline import IngestionPipeline
from app.services.retrieval.service import Retriever

configure_logging("WARNING", "console")

# Integration tests use BOTH database roles, because the split between them is
# load-bearing rather than cosmetic.
#
#   app_rw  -- what the application connects as. Not a superuser, NOBYPASSRLS,
#              owns nothing. Row Level Security actually applies to it.
#   app     -- the owner. Runs migrations and provisions tenants. In the Compose
#              image this is also a superuser, and superusers bypass RLS
#              entirely regardless of FORCE -- which is exactly why the
#              application must never use it.
#
# Testing isolation through the owner role would pass while proving nothing.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://app_rw:app_rw@localhost:5432/agentdb"
)
TEST_OWNER_DATABASE_URL = os.environ.get(
    "TEST_OWNER_DATABASE_URL", "postgresql+asyncpg://app:app@localhost:5432/agentdb"
)
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/1")


# ---------------------------------------------------------------------------
# Settings and identities
# ---------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings wired to fakes, with storage in a temp directory."""
    return Settings(
        app_env="test",
        database_url=TEST_DATABASE_URL,
        redis_url=TEST_REDIS_URL,
        embedding_dim=768,
        storage=StorageSettings(provider="memory", local_path=tmp_path / "storage"),
        queue=QueueSettings(provider="memory"),
    )


@pytest.fixture
def organization_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def location_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def tenant(organization_id: uuid.UUID, location_id: uuid.UUID) -> TenantContext:
    return TenantContext(organization_id=organization_id, location_id=location_id)


@pytest.fixture
def admin(organization_id: uuid.UUID) -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        organization_id=organization_id,
        role=Role.ADMIN,
        email="admin@test.example",
        scopes=scopes_for(Role.ADMIN),
    )


@pytest.fixture
def user(organization_id: uuid.UUID, location_id: uuid.UUID) -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        organization_id=organization_id,
        location_id=location_id,
        role=Role.USER,
        email="user@test.example",
        scopes=scopes_for(Role.USER),
    )


@pytest.fixture
def trace() -> TraceContext:
    return TraceContext.new()


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_llm() -> FakeLLMProvider:
    return FakeLLMProvider(
        LLMProviderConfig(
            name="fake",
            type="fake",
            models=[
                ModelConfig(
                    name="fake-fast",
                    tier=ModelTier.FAST,
                    supports_tools=True,
                    supports_json_schema=True,
                    context_window=32_000,
                ),
                ModelConfig(
                    name="fake-quality",
                    tier=ModelTier.QUALITY,
                    supports_tools=True,
                    supports_json_schema=True,
                    context_window=128_000,
                    cost_per_1m_input=3.0,
                    cost_per_1m_output=15.0,
                ),
            ],
        )
    )


@pytest.fixture
def fake_embeddings() -> FakeEmbeddingProvider:
    return FakeEmbeddingProvider(
        EmbeddingProviderConfig(name="fake", type="fake", model="fake-embed", dimension=768)
    )


@pytest.fixture
def providers(
    settings: Settings,
    fake_llm: FakeLLMProvider,
    fake_embeddings: FakeEmbeddingProvider,
) -> ProviderBundle:
    return ProviderBundle(
        llm={"fake": fake_llm},
        embeddings=fake_embeddings,
        storage=MemoryStorageProvider(),
        queue=MemoryQueue(),
        search=build_search_provider(settings),
        vector_store=build_vector_store(settings),
    )


# ---------------------------------------------------------------------------
# Database (integration only)
# ---------------------------------------------------------------------------


async def _database_available(url: str) -> bool:
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url, pool_pre_ping=True)
    try:
        async with engine.connect():
            return True
    except Exception:  # noqa: BLE001 - absence is the answer, not an error
        return False
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def db_engine(settings: Settings) -> AsyncIterator[object]:
    """Engine against the integration database, or skip the test."""
    from app.core.db import create_engine

    if not await _database_available(settings.database_url):
        pytest.skip("PostgreSQL not reachable. Start it with: docker compose up -d postgres redis")

    engine = create_engine(settings)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def uow(settings: Settings, db_engine: object, tenant: TenantContext):
    """A UnitOfWork bound to the integration database."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)  # type: ignore[arg-type]
    return UnitOfWork(tenant, settings, sessionmaker=maker)


@pytest.fixture
async def app_engine(settings: Settings, db_engine: object) -> AsyncIterator[None]:
    """Stand up the process-wide engine and sessionmaker.

    Most services take a UnitOfWork, so a test can hand them an engine directly.
    A few deliberately do not -- `AuthService` resolves the login directory
    without tenant scope, and `PlatformService` acts outside any single tenant --
    and those reach for the module-level sessionmaker. Tests that exercise them
    have to stand it up, pointed at the unprivileged `app_rw` role so RLS still
    applies.
    """
    from app.core.db import dispose_engine, init_engine

    await dispose_engine()
    init_engine(settings)
    try:
        yield None
    finally:
        await dispose_engine()


@pytest.fixture
async def owner_engine() -> AsyncIterator[object]:
    """Engine as the owner role, for provisioning tenants in fixtures.

    Creating an organization is deliberately outside what the application role
    can do: the policy on `organizations` restricts a session to its own row, so
    there is no tenant context under which a new tenant can be inserted.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    if not await _database_available(TEST_OWNER_DATABASE_URL):
        pytest.skip("PostgreSQL not reachable (owner role)")

    engine = create_async_engine(TEST_OWNER_DATABASE_URL)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def fake_embedding_space(owner_engine: object, fake_embeddings) -> AsyncIterator[None]:
    """Make the fake provider's embedding space the current one, then put it back.

    The pipeline refuses to write vectors from a provider that is not the
    current space -- that is the point of the mechanism, and it is what stops
    one model's vectors being labelled as another's. A test database whose
    current space was set by a real run of the application therefore has to be
    pointed at the provider the tests actually use.

    The restore afterwards is not tidiness. `embedding_spaces` is global, not
    tenant-scoped, so a test run against a shared development database leaves
    `fake` current for everyone -- and the application's boot guard then
    *correctly* refuses to start, because the configured provider no longer
    matches the indexed vectors. Working as designed, but a confusing way to
    find out. So the previous current space is restored even when a test fails.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(owner_engine, expire_on_commit=False, autoflush=False)  # type: ignore[arg-type]
    async with maker() as session, session.begin():
        previous = (
            await session.execute(text("SELECT id FROM embedding_spaces WHERE is_current LIMIT 1"))
        ).scalar_one_or_none()
        await session.execute(
            text("UPDATE embedding_spaces SET is_current = false WHERE is_current")
        )
        await session.execute(
            text(
                "INSERT INTO embedding_spaces"
                " (id, provider_type, model, dimension, normalized, is_current,"
                "  created_at, updated_at)"
                " VALUES (gen_random_uuid(), :p, :m, :d, true, true, now(), now())"
                " ON CONFLICT (provider_type, model, dimension)"
                " DO UPDATE SET is_current = true"
            ),
            {
                "p": fake_embeddings.provider_type,
                "m": fake_embeddings.model,
                "d": fake_embeddings.dimension,
            },
        )

    try:
        yield None
    finally:
        if previous is not None:
            async with maker() as session, session.begin():
                await session.execute(
                    text("UPDATE embedding_spaces SET is_current = false WHERE is_current")
                )
                await session.execute(
                    text("UPDATE embedding_spaces SET is_current = true WHERE id = :id"),
                    {"id": previous},
                )


@pytest.fixture
async def seeded_org(owner_engine: object, fake_embedding_space: None) -> AsyncIterator[dict]:
    """Create a throwaway organization with two locations; drop it afterwards.

    Each test gets its own organization, so tests cannot see each other's rows
    even when they run in parallel -- which is also a small ongoing proof that
    the tenant filtering works.
    """
    from sqlalchemy import text

    maker = async_sessionmaker(owner_engine, expire_on_commit=False, autoflush=False)  # type: ignore[arg-type]
    slug = f"test-{uuid.uuid4().hex[:10]}"

    async with maker() as session, session.begin():
        org_id = (
            await session.execute(
                text(
                    "INSERT INTO organizations (id, name, slug, is_active, settings,"
                    " created_at, updated_at)"
                    " VALUES (gen_random_uuid(), :name, :slug, true, '{}'::jsonb,"
                    " now(), now()) RETURNING id"
                ),
                {"name": f"Test {slug}", "slug": slug},
            )
        ).scalar_one()

        # is_local=true. Using false here would set the value for the whole
        # connection, which then goes back to the pool still carrying it -- the
        # exact cross-tenant leak app.core.db is built to prevent.
        await session.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(org_id)},
        )
        location_ids = {}
        for name in ("alpha", "beta"):
            location_ids[name] = (
                await session.execute(
                    text(
                        "INSERT INTO locations (id, organization_id, name, slug,"
                        " timezone, is_active, settings, created_at, updated_at)"
                        " VALUES (gen_random_uuid(), :org, :name, :slug, 'UTC', true,"
                        " '{}'::jsonb, now(), now()) RETURNING id"
                    ),
                    {"org": org_id, "name": name.title(), "slug": name},
                )
            ).scalar_one()

        # A real admin row: documents.created_by is a foreign key, so a
        # synthetic user id would fail on the first upload.
        admin_id = (
            await session.execute(
                text(
                    "INSERT INTO users (id, organization_id, location_id, email,"
                    " full_name, password_hash, role, is_active, token_version,"
                    " created_at, updated_at)"
                    " VALUES (gen_random_uuid(), :org, NULL, :email, 'Test Admin',"
                    " 'x', 'ADMIN', true, 0, now(), now()) RETURNING id"
                ),
                {"org": org_id, "email": f"admin@{slug}.example"},
            )
        ).scalar_one()

    yield {
        "organization_id": org_id,
        "locations": location_ids,
        "admin_id": admin_id,
        "slug": slug,
    }

    async with maker() as session, session.begin():
        await session.execute(text("DELETE FROM organizations WHERE id = :id"), {"id": org_id})


# ---------------------------------------------------------------------------
# Ingestion helpers (integration and e2e)
# ---------------------------------------------------------------------------
#
# The helper below runs the real pipeline -- the same code the worker runs --
# against the real schema, with fake model providers. Nothing about retrieval,
# versioning or activation is simulated.


@dataclass(slots=True)
class Ingested:
    """What an ingestion helper call produced."""

    document_id: uuid.UUID
    version_id: uuid.UUID
    version_number: int
    job_id: uuid.UUID


@pytest.fixture
def org_tenant(seeded_org: dict) -> TenantContext:
    return TenantContext(organization_id=seeded_org["organization_id"])


@pytest.fixture
def org_uow(settings: Settings, db_engine: object, org_tenant: TenantContext) -> UnitOfWork:
    maker = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)  # type: ignore[arg-type]
    return UnitOfWork(org_tenant, settings, sessionmaker=maker)


@pytest.fixture
def retriever(settings: Settings, providers: ProviderBundle) -> Retriever:
    return Retriever(
        settings=settings,
        search=providers.require_search(),
        embeddings=providers.require_embeddings(),
    )


@pytest.fixture
def ingest(
    settings: Settings,
    providers: ProviderBundle,
    org_uow: UnitOfWork,
    org_tenant: TenantContext,
    seeded_org: dict,
) -> Callable[..., Awaitable[Ingested]]:
    """Upload a document and run the full pipeline synchronously.

    Returns the ids so a test can then assert on versions, chunks or retrieval.
    """
    service = DocumentService(settings, providers)
    pipeline = IngestionPipeline(settings=settings, providers=providers)
    admin = system_principal(seeded_org["organization_id"], seeded_org["admin_id"])

    async def _ingest(
        body: str,
        *,
        title: str,
        location: str | None = None,
        document_type: str = "policy",
        run_pipeline: bool = True,
    ) -> Ingested:
        location_id = seeded_org["locations"].get(location) if location else None
        result = await service.upload(
            org_uow,
            admin,
            data=body.encode("utf-8"),
            filename=f"{title}.md",
            content_type="text/markdown",
            title=title,
            location_id=location_id,
            document_type=document_type,
            trace=TraceContext.new(organization_id=seeded_org["organization_id"]),
        )
        if run_pipeline:
            await pipeline.run(
                org_uow,
                result.job_id,
                tenant=org_tenant,
                trace=TraceContext.new(),
                worker_id="test",
            )
        return Ingested(
            document_id=result.document.id,
            version_id=result.version.id,
            version_number=result.version.version_number,
            job_id=result.job_id,
        )

    return _ingest


@pytest.fixture
def pipeline(settings: Settings, providers: ProviderBundle) -> IngestionPipeline:
    return IngestionPipeline(settings=settings, providers=providers)


@pytest.fixture
def sample_markdown() -> str:
    """A document with several clearly separate sections."""
    return """# Operations Handbook

## Breakfast Service

Breakfast is served from 7:00 AM to 10:00 AM in the main dining room every day.
Children under six eat free.

## Pet Policy

Pets are not permitted, except assistance dogs which are welcome everywhere.

## Smoking Policy

All rooms are non-smoking. A cleaning fee of 50,000 JPY applies to smoking in a
guest room.

## Parking

Valet parking is 4,000 JPY per night. Vehicle height is limited to 2.1 metres.
"""
