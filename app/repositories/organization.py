"""Organization and location queries."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.core.tenancy import TenantContext
from app.models.organization import Location, Organization
from app.repositories.base import assert_tenant, require


async def get_organization(session: AsyncSession, organization_id: UUID) -> Organization:
    org = await session.get(Organization, organization_id)
    return require(org, what="Organization", identifier=organization_id)


async def get_organization_by_slug(session: AsyncSession, slug: str) -> Organization | None:
    result = await session.execute(select(Organization).where(Organization.slug == slug))
    return result.scalar_one_or_none()


async def create_organization(
    session: AsyncSession, *, name: str, slug: str, settings: dict | None = None
) -> Organization:
    org = Organization(name=name, slug=slug, settings=settings or {})
    session.add(org)
    await session.flush()
    return org


async def list_locations(
    session: AsyncSession, tenant: TenantContext, *, include_inactive: bool = False
) -> Sequence[Location]:
    stmt = select(Location).where(Location.organization_id == tenant.organization_id)
    if not include_inactive:
        stmt = stmt.where(Location.is_active.is_(True))
    result = await session.execute(stmt.order_by(Location.name))
    return result.scalars().all()


async def get_location(session: AsyncSession, tenant: TenantContext, location_id: UUID) -> Location:
    location = await session.get(Location, location_id)
    location = require(location, what="Location", identifier=location_id)
    assert_tenant(location, tenant)
    return location


async def get_location_by_slug(
    session: AsyncSession, tenant: TenantContext, slug: str
) -> Location | None:
    result = await session.execute(
        select(Location).where(
            Location.organization_id == tenant.organization_id,
            Location.slug == slug,
        )
    )
    return result.scalar_one_or_none()


async def create_location(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    name: str,
    slug: str,
    timezone: str = "UTC",
    settings: dict | None = None,
) -> Location:
    location = Location(
        organization_id=tenant.organization_id,
        name=name,
        slug=slug,
        timezone=timezone,
        settings=settings or {},
    )
    session.add(location)
    await session.flush()
    return location


UPDATABLE_LOCATION_FIELDS = frozenset({"name", "timezone", "settings", "is_active"})


async def count_users_at_location(
    session: AsyncSession, tenant: TenantContext, location_id: UUID
) -> int:
    from app.models.user import User

    result = await session.execute(
        select(func.count(User.id)).where(
            User.organization_id == tenant.organization_id,
            User.location_id == location_id,
            User.is_active.is_(True),
        )
    )
    return int(result.scalar_one())


async def update_location(
    session: AsyncSession, tenant: TenantContext, location_id: UUID, changes: dict[str, object]
) -> Location:
    """Edit a branch.

    ``slug`` is not updatable: it appears in stored object keys, so changing it
    would orphan every file already written under the old one.

    Deactivating a branch that still has people pinned to it is refused.
    ``list_locations`` hides inactive branches, so those users would be left
    assigned to something nobody can see or select -- visible only as knowledge
    that quietly stops being reachable.
    """
    unknown = set(changes) - UPDATABLE_LOCATION_FIELDS
    if unknown:
        raise ValueError(f"Not updatable: {sorted(unknown)}")

    location = await get_location(session, tenant, location_id)

    if changes.get("is_active") is False and location.is_active:
        pinned = await count_users_at_location(session, tenant, location_id)
        if pinned:
            raise ConflictError(
                f"{pinned} active user(s) are assigned to this branch. "
                f"Move them elsewhere before deactivating it.",
                details={"pinned_users": pinned},
            )

    for field_name, value in changes.items():
        setattr(location, field_name, value)
    await session.flush()
    return location
