"""Database engine, sessions and tenant scoping.

This is the most correctness-critical module in the system. Two rules hold
everywhere, and both are enforced here rather than by convention:

1. **Tenant context is set inside the transaction.** PostgreSQL's
   ``SET LOCAL`` (and ``set_config(..., is_local => true)``) only takes effect
   within an open transaction, and is reverted when it ends. Setting it outside
   one is a silent no-op that would leave the previous tenant's value alive on a
   pooled connection -- a genuine cross-tenant read. So every session here opens
   an explicit transaction *first* and sets the GUC as its first statement.

2. **Transactions are short.** A transaction is never held across an LLM call,
   an embedding call, or a tool call. Because tenant context lives in the
   transaction, the tempting implementation is one long request-scoped
   transaction; that exhausts the pool under modest concurrency. :class:`UnitOfWork`
   hands out a fresh short transaction per interaction instead, and
   ``idle_in_transaction_session_timeout`` makes a violation fail loudly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.tenancy import TenantContext

log = get_logger(__name__)

#: Name of the PostgreSQL setting RLS policies read.
ORG_GUC = "app.current_org_id"

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def _install_vector_codec(engine: AsyncEngine) -> None:
    """Register pgvector's asyncpg codec on every new connection.

    Without this, vectors round-trip as strings. The extension may not exist yet
    on a brand-new database (the first migration creates it), so a failure here
    is logged and ignored rather than preventing startup.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:  # pragma: no cover
        try:
            from pgvector.asyncpg import register_vector

            dbapi_connection.run_async(register_vector)
        except Exception as exc:  # noqa: BLE001 - extension may not exist yet
            log.debug("pgvector_codec_not_registered", error=str(exc))


def create_engine(settings: Settings) -> AsyncEngine:
    engine = create_async_engine(
        settings.database_url,
        echo=settings.database_echo,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_pre_ping=True,
        # Roll back on return so a connection can never carry a half-open
        # transaction -- and therefore a stale tenant GUC -- back to the pool.
        pool_reset_on_return="rollback",
        connect_args={"server_settings": {"application_name": settings.app_name}},
    )
    _install_vector_codec(engine)
    return engine


def init_engine(settings: Settings) -> AsyncEngine:
    """Create the process-wide engine and sessionmaker. Idempotent."""
    global _engine, _sessionmaker
    if _engine is None:
        _engine = create_engine(settings)
        _sessionmaker = async_sessionmaker(
            _engine,
            expire_on_commit=False,  # mandatory for async: no lazy refresh after commit
            autoflush=False,
        )
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("Database engine not initialized; call init_engine() first")
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError("Database engine not initialized; call init_engine() first")
    return _sessionmaker


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


# ---------------------------------------------------------------------------
# Tenant-scoped sessions
# ---------------------------------------------------------------------------


async def _apply_session_guards(
    session: AsyncSession,
    organization_id: UUID | None,
    settings: Settings,
) -> None:
    """Set tenant context and per-transaction timeouts.

    Must run as the first statements inside the transaction. ``set_config`` is
    used rather than ``SET LOCAL`` because it takes a bind parameter, which
    keeps the org id out of the SQL string entirely.
    """
    await session.execute(
        text("SELECT set_config('statement_timeout', :v, true)"),
        {"v": str(settings.statement_timeout_ms)},
    )
    await session.execute(
        text("SELECT set_config('idle_in_transaction_session_timeout', :v, true)"),
        {"v": str(settings.idle_in_transaction_timeout_ms)},
    )
    # An absent org id is set to the empty string, which policies treat as
    # "no tenant" and deny. Default-deny is the only safe failure mode.
    await session.execute(
        text(f"SELECT set_config('{ORG_GUC}', :org, true)"),
        {"org": str(organization_id) if organization_id else ""},
    )


@asynccontextmanager
async def tenant_session(
    organization_id: UUID | None,
    settings: Settings,
    *,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> AsyncIterator[AsyncSession]:
    """The only way to obtain a database session.

    API requests and background workers both come through here, so there is no
    second, unaudited path to the data.
    """
    maker = sessionmaker or get_sessionmaker()
    async with maker() as session, session.begin():
        await _apply_session_guards(session, organization_id, settings)
        yield session


@asynccontextmanager
async def unscoped_session(
    settings: Settings,
    *,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> AsyncIterator[AsyncSession]:
    """A session with no tenant context.

    RLS therefore denies every tenant table. This exists for health checks,
    migrations and the deliberately-global ``user_directory`` lookup used to
    resolve an organization at login -- and for nothing else.
    """
    maker = sessionmaker or get_sessionmaker()
    async with maker() as session, session.begin():
        await _apply_session_guards(session, None, settings)
        yield session


class UnitOfWork:
    """Factory for short, tenant-scoped transactions.

    Services take a ``UnitOfWork`` rather than an ``AsyncSession`` precisely so
    they cannot hold a transaction open across a slow provider call::

        async with uow.begin() as session:
            chunks = await repo.fetch(session, ...)
        answer = await llm.generate(...)      # no transaction held here
        async with uow.begin() as session:
            await repo.record(session, ...)
    """

    __slots__ = ("_maker", "_settings", "tenant")

    def __init__(
        self,
        tenant: TenantContext | None,
        settings: Settings,
        *,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.tenant = tenant
        self._settings = settings
        self._maker = sessionmaker or get_sessionmaker()

    @property
    def organization_id(self) -> UUID | None:
        return self.tenant.organization_id if self.tenant else None

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[AsyncSession]:
        async with tenant_session(
            self.organization_id, self._settings, sessionmaker=self._maker
        ) as session:
            yield session

    def scoped_to(self, tenant: TenantContext) -> UnitOfWork:
        return UnitOfWork(tenant, self._settings, sessionmaker=self._maker)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


async def check_application_role(settings: Settings) -> dict[str, Any]:
    """Verify the connection role cannot bypass Row Level Security.

    This is the check that catches the worst possible misconfiguration. A
    superuser -- or any role with BYPASSRLS -- ignores every policy on every
    table, silently. No error is raised, no query fails, and every tenant can
    read every other tenant's documents. It is invisible in testing because
    everything appears to work.

    The Compose stack's ``POSTGRES_USER`` is a superuser and owns the schema, so
    the failure mode is one careless ``DATABASE_URL`` away. The application must
    connect as the separate, unprivileged ``app_rw`` role.
    """
    try:
        async with unscoped_session(settings) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles"
                        " WHERE rolname = current_user"
                    )
                )
            ).one()
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return {"ok": False, "error": str(exc)[:200]}

    role, is_superuser, bypasses_rls = row
    unsafe = bool(is_superuser or bypasses_rls)
    return {
        "ok": not unsafe,
        "role": role,
        "superuser": bool(is_superuser),
        "bypasses_rls": bool(bypasses_rls),
        **(
            {
                "error": (
                    f"Role '{role}' bypasses Row Level Security. Tenant isolation "
                    f"is NOT enforced. Connect as an unprivileged role such as "
                    f"app_rw (see docker/postgres/init.sql)."
                )
            }
            if unsafe
            else {}
        ),
    }


async def check_database(settings: Settings) -> dict[str, Any]:
    """Liveness probe for the database and the pgvector extension."""
    try:
        async with unscoped_session(settings) as session:
            await session.execute(text("SELECT 1"))
            row = await session.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            )
            version = row.scalar_one_or_none()
        return {"ok": True, "pgvector": version}
    except Exception as exc:  # noqa: BLE001 - health must never raise
        return {"ok": False, "error": str(exc)}
