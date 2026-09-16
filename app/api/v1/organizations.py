"""Organization and location endpoints.

There is no "list organizations": a caller's organization comes from their
token, and the only one they can ever see is their own.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status

from app.api.deps import CurrentAdmin, CurrentPrincipal, Uow
from app.repositories import organization as org_repo
from app.schemas.common import (
    LocationCreate,
    LocationOut,
    LocationUpdate,
    OrganizationOut,
    Page,
)

router = APIRouter(tags=["organizations"])


@router.get("/organizations/me", response_model=OrganizationOut)
async def my_organization(principal: CurrentPrincipal, uow: Uow) -> OrganizationOut:
    async with uow.begin() as session:
        organization = await org_repo.get_organization(session, principal.organization_id)
        return OrganizationOut.model_validate(organization)


@router.get("/locations", response_model=Page[LocationOut])
async def list_locations(
    principal: CurrentPrincipal,
    uow: Uow,
    include_inactive: bool = False,
) -> Page[LocationOut]:
    """The branches this caller can see.

    A user pinned to one branch sees only that one -- not as a filter they could
    turn off, but because that is the whole of their scope.
    """
    from app.repositories.organization import list_locations as repo_list

    async with uow.begin() as session:
        locations = await repo_list(session, principal.tenant, include_inactive=include_inactive)

    if principal.location_id is not None:
        locations = [loc for loc in locations if loc.id == principal.location_id]

    items = [LocationOut.model_validate(loc) for loc in locations]
    return Page[LocationOut](items=items, total=len(items), limit=len(items) or 1, offset=0)


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


@router.patch("/locations/{location_id}", response_model=LocationOut)
async def update_location(
    location_id: UUID, payload: LocationUpdate, admin: CurrentAdmin, uow: Uow
) -> LocationOut:
    """Rename a branch, move its timezone, or take it out of service.

    Deactivating is refused while people are still assigned to it: inactive
    branches disappear from the branch list, so those users would be pinned to
    something nobody can see, and their knowledge would quietly stop being
    reachable.
    """
    from app.core.enums import AuditAction
    from app.repositories import audit as audit_repo
    from app.repositories.organization import update_location as repo_update

    changes = payload.model_dump(exclude_unset=True)

    async with uow.begin() as session:
        location = await repo_update(session, admin.tenant, location_id, changes)
        if changes:
            await audit_repo.record(
                session,
                organization_id=admin.organization_id,
                action=AuditAction.LOCATION_UPDATE,
                actor_user_id=admin.user_id,
                location_id=location.id,
                resource_type="location",
                resource_id=location.id,
                message=f"{', '.join(sorted(changes))} changed",
            )
        return LocationOut.model_validate(location)
