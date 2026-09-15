"""Repository conventions.

Repositories are the only place that knows SQL or SQLAlchemy. Services take a
:class:`~app.core.db.UnitOfWork` and hand a session to repository functions;
they never build queries themselves.

Every function takes the session explicitly rather than holding one, which is
what keeps transactions short: the caller decides the boundary, and a repository
can never accidentally keep one open across a provider call.
"""

from __future__ import annotations

from typing import TypeVar
from uuid import UUID

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, TenantScopeError
from app.core.tenancy import TenantContext
from app.models.base import Base

T = TypeVar("T", bound=Base)


def scoped(stmt: Select, model: type[T], tenant: TenantContext) -> Select:
    """Add the organization predicate to a query.

    Row Level Security already enforces this at the database, but the predicate
    is applied here too. Defence in depth is the lesser reason; the real one is
    that an explicit predicate lets the planner use the tenant index instead of
    filtering after the fact.
    """
    return stmt.where(model.organization_id == tenant.organization_id)  # type: ignore[attr-defined]


def require(entity: T | None, *, what: str, identifier: object = None) -> T:
    if entity is None:
        suffix = f" ({identifier})" if identifier is not None else ""
        raise NotFoundError(f"{what} not found{suffix}")
    return entity


def assert_tenant(entity: object, tenant: TenantContext) -> None:
    """Confirm a loaded row really belongs to the caller's organization.

    Should be impossible given RLS plus the query predicate. It is checked
    anyway on anything reached by primary key, because "impossible" is exactly
    the assumption a cross-tenant leak is built on.
    """
    owner: UUID | None = getattr(entity, "organization_id", None)
    if owner is not None and owner != tenant.organization_id:
        raise TenantScopeError("Record belongs to a different organization")


async def flush(session: AsyncSession) -> None:
    """Flush pending changes so server defaults and identities are populated."""
    await session.flush()
