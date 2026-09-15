"""Changing a user, with the rules that stop an organization breaking itself.

Two different callers need this logic and must not drift apart: an organization
admin acting on their own tenant, and a platform operator acting on any tenant.
So it lives here once, parameterised by who is asking, rather than twice.

Every operation runs inside a single ``uow.begin()`` block. That is not tidiness
-- each guard below is a read followed by a write, and a guard that reads in one
transaction and writes in another is not a guard, it is a race.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.core.db import UnitOfWork
from app.core.enums import AuditAction, Role
from app.core.errors import ConflictError, TenantScopeError
from app.core.logging import get_logger
from app.core.security import hash_password
from app.core.tenancy import TenantContext
from app.models.user import User
from app.repositories import audit as audit_repo
from app.repositories import user as user_repo
from app.repositories.organization import get_location

log = get_logger(__name__)

#: Changing either of these changes what the user is allowed to do, so their
#: existing tokens must stop working. A display-name edit must not sign anyone
#: out, which is why this is a set rather than "any change at all".
_PRIVILEGE_FIELDS = frozenset({"role", "location_id"})


@dataclass(frozen=True, slots=True)
class Actor:
    """Who is making the change, in the only terms the rules care about.

    A platform operator has no user row in the organization they are acting on,
    so ``user_id`` and ``location_id`` are ``None`` for them -- which correctly
    makes the self-harm and pinned-admin rules inapplicable rather than needing
    a separate code path.
    """

    label: str
    user_id: UUID | None = None
    location_id: UUID | None = None


async def _forbid_removing_last_admin(session, tenant: TenantContext, user: User) -> None:
    """Refuse to leave an organization with no way back in.

    An organization whose last administrator is disabled or demoted cannot
    create users, upload knowledge, or undo the change. Recovery needs a
    platform operator -- which for a customer means a support ticket.

    The count is taken with ``FOR UPDATE`` because this is a read-then-write:
    under READ COMMITTED, two transactions each demoting one of the final two
    admins would both see two admins and both succeed.
    """
    if user.role is not Role.ADMIN or not user.is_active:
        return

    remaining = await user_repo.lock_active_admins(session, tenant)
    if set(remaining) <= {user.id}:
        raise ConflictError(
            "An organization must keep at least one active administrator. "
            "Appoint another one first.",
            details={"active_admin_count": len(remaining)},
        )


def _check_location_assignment(actor: Actor, requested: UUID | None) -> None:
    """An admin pinned to one branch may not move anyone outside it.

    Deliberately not implemented with ``TenantContext.narrowed_to``: that
    returns ``self`` unchanged when handed ``None`` (app/core/tenancy.py), so a
    pinned admin could unpin a user to organization-wide scope -- widening, not
    narrowing -- and pass the check.
    """
    if actor.location_id is None:
        return
    if requested != actor.location_id:
        raise TenantScopeError(
            "You can only assign users to your own location.",
            details={"requested_location_id": str(requested) if requested else None},
        )


async def set_user_active(
    uow: UnitOfWork,
    tenant: TenantContext,
    *,
    user_id: UUID,
    active: bool,
    actor: Actor,
    protect_last_admin: bool = True,
) -> User:
    """Enable or disable a user, revoking their tokens on disable.

    ``protect_last_admin`` is deliberately switchable, and the platform-operator
    path turns it off. An organization admin must never be able to lock their
    own organization out, because only a platform operator could let them back
    in. The operator *is* that recovery path, and suspending a whole customer by
    disabling their last admin is a real thing to want -- so the rule that
    protects tenants from themselves would only get in the operator's way.
    Do not "fix" this inconsistency.
    """
    async with uow.begin() as session:
        user = await user_repo.get_user(session, tenant, user_id)

        if not active:
            if actor.user_id is not None and actor.user_id == user.id:
                # A clearer message than the last-admin conflict, and it holds
                # even when other admins exist.
                raise ConflictError("You cannot disable your own account.")
            if protect_last_admin:
                await _forbid_removing_last_admin(session, tenant, user)

        await user_repo.set_active(session, user, active=active)
        await audit_repo.record(
            session,
            organization_id=tenant.organization_id,
            action=AuditAction.USER_ENABLE if active else AuditAction.USER_DISABLE,
            actor_user_id=actor.user_id,
            resource_type="user",
            resource_id=user.id,
            message=f"by {actor.label}",
        )
        log.info("user_active_changed", user_id=str(user.id), active=active)
        return user


async def update_user(
    uow: UnitOfWork,
    tenant: TenantContext,
    *,
    user_id: UUID,
    changes: dict[str, object],
    actor: Actor,
) -> User:
    """Apply a partial update.

    ``changes`` carries only the fields the caller actually sent, so clearing a
    field stays distinguishable from leaving it alone.
    """
    async with uow.begin() as session:
        user = await user_repo.get_user(session, tenant, user_id)
        if not changes:
            return user

        if "location_id" in changes:
            requested = changes["location_id"]
            _check_location_assignment(actor, requested)  # type: ignore[arg-type]
            if requested is not None:
                # Proves the branch belongs to this organization -- which is
                # what stops an admin parking a user in another tenant's branch.
                await get_location(session, tenant, requested)  # type: ignore[arg-type]

        new_role = changes.get("role")
        if new_role is not None and new_role is not Role.ADMIN:
            if actor.user_id is not None and actor.user_id == user.id:
                raise ConflictError("You cannot remove your own administrator role.")
            await _forbid_removing_last_admin(session, tenant, user)

        await user_repo.update_user(session, user, changes)

        # A role or location change alters what this user may do, but
        # `AuthService.authenticate` deliberately does not re-read the database
        # on each request -- so without this bump a demoted admin would keep
        # administrator power until their access token expired.
        privileged = bool(_PRIVILEGE_FIELDS & set(changes))
        if privileged:
            await user_repo.invalidate_tokens(session, user)

        await audit_repo.record(
            session,
            organization_id=tenant.organization_id,
            action=AuditAction.USER_UPDATE,
            actor_user_id=actor.user_id,
            resource_type="user",
            resource_id=user.id,
            message=f"{', '.join(sorted(changes))} changed by {actor.label}",
        )
        log.info(
            "user_updated",
            user_id=str(user.id),
            fields=sorted(changes),
            sessions_revoked=privileged,
        )
        return user


async def reset_password(
    uow: UnitOfWork,
    tenant: TenantContext,
    *,
    user_id: UUID,
    password: str,
    actor: Actor,
) -> User:
    """Set a user's password on their behalf, ending their sessions."""
    async with uow.begin() as session:
        user = await user_repo.get_user(session, tenant, user_id)
        await user_repo.set_password(session, user, hash_password(password))
        await audit_repo.record(
            session,
            organization_id=tenant.organization_id,
            action=AuditAction.USER_PASSWORD_RESET,
            actor_user_id=actor.user_id,
            resource_type="user",
            resource_id=user.id,
            message=f"reset by {actor.label}",
        )
        log.info("user_password_reset", user_id=str(user.id))
        return user
