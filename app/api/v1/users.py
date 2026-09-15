"""Organization user management, for an organization's own administrator.

Everything here is scoped to the caller's organization by their token. No path,
query or body field names an organization -- that is what makes "an admin can
only ever touch their own people" true by construction rather than by review.

The rules these endpoints enforce (last-admin protection, no self-harm, no
assigning a user outside your own branch) live in
:mod:`app.services.users.management`, shared with the platform-operator path so
the two cannot drift apart.
"""

from __future__ import annotations

import secrets
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from app.api.deps import CurrentAdmin, Uow
from app.core.enums import Role
from app.schemas.auth import (
    PasswordResetRequest,
    PasswordResetResponse,
    UserUpdate,
)
from app.schemas.common import Page, UserOut
from app.services.users import management

router = APIRouter(prefix="/users", tags=["users"])


def _actor(admin: CurrentAdmin) -> management.Actor:
    return management.Actor(
        label=admin.email or str(admin.user_id),
        user_id=admin.user_id,
        location_id=admin.location_id,
    )


@router.get("", response_model=Page[UserOut])
async def list_users(
    admin: CurrentAdmin,
    uow: Uow,
    role: Role | None = None,
    location_id: UUID | None = None,
    is_active: bool | None = None,
    q: Annotated[str | None, Query(max_length=200, description="Match email or name")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[UserOut]:
    """The people in the caller's organization.

    An administrator pinned to a location sees only that location's users, the
    same way ``GET /locations`` already narrows for them.
    """
    from app.repositories.user import count_users
    from app.repositories.user import list_users as repo_list

    scope = location_id if location_id is not None else admin.location_id
    filters = {"role": role, "location_id": scope, "is_active": is_active, "query": q}

    async with uow.begin() as session:
        users = await repo_list(session, admin.tenant, limit=limit, offset=offset, **filters)
        total = await count_users(session, admin.tenant, **filters)

    return Page[UserOut](
        items=[UserOut.model_validate(u) for u in users],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{user_id}", response_model=UserOut)
async def get_user(user_id: UUID, admin: CurrentAdmin, uow: Uow) -> UserOut:
    from app.repositories.user import get_user as repo_get

    async with uow.begin() as session:
        return UserOut.model_validate(await repo_get(session, admin.tenant, user_id))


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(user_id: UUID, payload: UserUpdate, admin: CurrentAdmin, uow: Uow) -> UserOut:
    """Change a user's name, role or branch.

    Changing the role or the branch **signs the user out**: what they are
    allowed to do has changed, and access tokens are not re-checked against the
    database on every request. Renaming them does not.
    """
    user = await management.update_user(
        uow,
        admin.tenant,
        user_id=user_id,
        changes=payload.model_dump(exclude_unset=True),
        actor=_actor(admin),
    )
    return UserOut.model_validate(user)


@router.post("/{user_id}/disable", response_model=UserOut)
async def disable_user(user_id: UUID, admin: CurrentAdmin, uow: Uow) -> UserOut:
    """Disable a user and revoke their outstanding tokens.

    Refused for your own account, and for the last active administrator.
    """
    user = await management.set_user_active(
        uow, admin.tenant, user_id=user_id, active=False, actor=_actor(admin)
    )
    return UserOut.model_validate(user)


@router.post("/{user_id}/enable", response_model=UserOut)
async def enable_user(user_id: UUID, admin: CurrentAdmin, uow: Uow) -> UserOut:
    user = await management.set_user_active(
        uow, admin.tenant, user_id=user_id, active=True, actor=_actor(admin)
    )
    return UserOut.model_validate(user)


@router.post(
    "/{user_id}/reset-password",
    response_model=PasswordResetResponse,
    status_code=status.HTTP_200_OK,
)
async def reset_password(
    user_id: UUID, payload: PasswordResetRequest, admin: CurrentAdmin, uow: Uow
) -> PasswordResetResponse:
    """Set a user's password for them, ending their sessions.

    Omit ``password`` and one is generated and returned **once** -- it is stored
    only as a hash, so it cannot be retrieved again. Supply your own and nothing
    is echoed back, since repeating a password you already know only adds
    another place for it to leak.
    """
    generated = payload.password or secrets.token_urlsafe(18)
    user = await management.reset_password(
        uow, admin.tenant, user_id=user_id, password=generated, actor=_actor(admin)
    )
    return PasswordResetResponse(
        user=UserOut.model_validate(user),
        password=generated if payload.password is None else None,
    )
