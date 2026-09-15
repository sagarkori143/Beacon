"""Organization and location endpoints.

There is no "list organizations": a caller's organization comes from their
token, and the only one they can ever see is their own.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status

from app.api.deps import CurrentAdmin, CurrentPrincipal, Uow
from app.repositories import organization as org_repo
from app.schemas.common import LocationCreate, LocationOut, OrganizationOut

router = APIRouter(tags=["organizations"])


@router.get("/organizations/me", response_model=OrganizationOut)
async def my_organization(principal: CurrentPrincipal, uow: Uow) -> OrganizationOut:
    async with uow.begin() as session:
        organization = await org_repo.get_organization(session, principal.organization_id)
        return OrganizationOut.model_validate(organization)


@router.get("/locations", response_model=list[LocationOut])
async def list_locations(principal: CurrentPrincipal, uow: Uow) -> list[LocationOut]:
    """Locations in the caller's organization.

    A user pinned to one location sees only that one -- the list is a navigation
    aid, and showing them the rest of the estate is not their business.
    """
    async with uow.begin() as session:
        locations = await org_repo.list_locations(session, principal.tenant)
        if principal.location_id is not None:
            locations = [loc for loc in locations if loc.id == principal.location_id]
        return [LocationOut.model_validate(loc) for loc in locations]


@router.get("/locations/{location_id}", response_model=LocationOut)
async def get_location(location_id: UUID, principal: CurrentPrincipal, uow: Uow) -> LocationOut:
    principal.tenant.narrowed_to(location_id)
    async with uow.begin() as session:
        location = await org_repo.get_location(session, principal.tenant, location_id)
        return LocationOut.model_validate(location)


@router.post("/locations", response_model=LocationOut, status_code=status.HTTP_201_CREATED)
async def create_location(payload: LocationCreate, admin: CurrentAdmin, uow: Uow) -> LocationOut:
    async with uow.begin() as session:
        location = await org_repo.create_location(
            session,
            admin.tenant,
            name=payload.name,
            slug=payload.slug,
            timezone=payload.timezone,
            settings=payload.settings,
        )
        return LocationOut.model_validate(location)
