"""Organization and location queries."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
