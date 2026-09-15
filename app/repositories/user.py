"""User queries, including the two-step login lookup.

Login is the one operation that must find a user before any organization is
known. It is handled by resolving the organization from ``user_directory``
first, then reading the real user row inside a correctly-scoped session -- so
password hashes and roles are never readable without tenant context.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import Role
from app.core.security import normalize_email
from app.core.tenancy import TenantContext
from app.models.user import User, UserDirectory
from app.repositories.base import assert_tenant, require


async def resolve_directory(session: AsyncSession, email: str) -> UserDirectory | None:
    """Map an email to its organization. Runs without tenant scope by design."""
    result = await session.execute(
        select(UserDirectory).where(UserDirectory.email == normalize_email(email))
    )
    return result.scalar_one_or_none()


async def get_user(session: AsyncSession, tenant: TenantContext, user_id: UUID) -> User:
    user = await session.get(User, user_id)
    user = require(user, what="User", identifier=user_id)
    assert_tenant(user, tenant)
    return user


async def get_user_by_email(
    session: AsyncSession, tenant: TenantContext, email: str
) -> User | None:
    result = await session.execute(
        select(User).where(
            User.organization_id == tenant.organization_id,
            User.email == normalize_email(email),
        )
    )
    return result.scalar_one_or_none()


def _user_filters(
    tenant: TenantContext,
    *,
    role: Role | None,
    location_id: UUID | None,
    is_active: bool | None,
    query: str | None,
) -> list[ColumnElement[bool]]:
    """Predicates shared by the list and count queries.

    Built once so a filtered page and its total can never disagree -- the bug
    where a table says "42 results" and then shows a differently-filtered 12.
    """
    filters: list[ColumnElement[bool]] = [User.organization_id == tenant.organization_id]
    if role is not None:
        filters.append(User.role == role)
    if location_id is not None:
        filters.append(User.location_id == location_id)
    if is_active is not None:
        filters.append(User.is_active.is_(is_active))
    if query:
        like = f"%{query.strip().lower()}%"
        filters.append(or_(User.email.ilike(like), User.full_name.ilike(like)))
    return filters


async def list_users(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    role: Role | None = None,
    location_id: UUID | None = None,
    is_active: bool | None = None,
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> Sequence[User]:
    result = await session.execute(
        select(User)
        .where(
            *_user_filters(
                tenant, role=role, location_id=location_id, is_active=is_active, query=query
            )
        )
        .order_by(User.email)
        .limit(limit)
        .offset(offset)
    )
    return result.scalars().all()


async def count_users(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    role: Role | None = None,
    location_id: UUID | None = None,
    is_active: bool | None = None,
    query: str | None = None,
) -> int:
    result = await session.execute(
        select(func.count(User.id)).where(
            *_user_filters(
                tenant, role=role, location_id=location_id, is_active=is_active, query=query
            )
        )
    )
    return int(result.scalar_one())


async def create_user(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    email: str,
    password_hash: str,
    role: Role = Role.USER,
    location_id: UUID | None = None,
    full_name: str | None = None,
) -> User:
    """Create a user and its directory entry together.

    The two must stay in step: a user without a directory row can never log in,
    and a directory row without a user is a dangling lookup. Creating them in
    one transaction is what keeps that true.
    """
    normalized = normalize_email(email)
    user = User(
        organization_id=tenant.organization_id,
        location_id=location_id,
        email=normalized,
        full_name=full_name,
        password_hash=password_hash,
        role=role,
    )
    session.add(user)
    await session.flush()

    session.add(
        UserDirectory(
            email=normalized,
            organization_id=tenant.organization_id,
            user_id=user.id,
            is_active=True,
        )
    )
    await session.flush()
    return user


async def record_login(session: AsyncSession, user: User) -> None:
    user.last_login_at = datetime.now(UTC)
    await session.flush()


async def invalidate_tokens(session: AsyncSession, user: User) -> None:
    """Revoke every outstanding token for this user."""
    user.token_version += 1
    await session.flush()


#: Fields a caller may change through an update. Anything outside this set is a
#: programming error, not a bad request -- `organization_id` in particular is
#: never updatable, because moving a user between tenants is not an edit.
UPDATABLE_USER_FIELDS = frozenset({"full_name", "role", "location_id"})


async def update_user(session: AsyncSession, user: User, changes: dict[str, object]) -> User:
    """Apply a partial update.

    Takes an explicit dict rather than keyword arguments with defaults so that
    "set full_name to null" and "leave full_name alone" stay distinguishable --
    the caller builds it from ``model_dump(exclude_unset=True)``.
    """
    unknown = set(changes) - UPDATABLE_USER_FIELDS
    if unknown:
        raise ValueError(f"Not updatable: {sorted(unknown)}")
    for field, value in changes.items():
        setattr(user, field, value)
    await session.flush()
    return user


async def set_password(session: AsyncSession, user: User, password_hash: str) -> User:
    """Replace the password and revoke outstanding tokens in one step.

    The two belong together: changing a password precisely because it may be
    compromised, while leaving sessions opened with it alive, is not a password
    change.
    """
    user.password_hash = password_hash
    user.token_version += 1
    await session.flush()
    return user


async def set_active(session: AsyncSession, user: User, *, active: bool) -> User:
    """Enable or disable a user, keeping the login directory in step.

    Login checks ``is_active`` on both this row and the directory row, so either
    one alone would be enough to block sign-in. Both are set anyway: leaving the
    directory disagreeing with the user row means the next person to read it
    gets the wrong answer.
    """
    user.is_active = active
    directory = await session.get(UserDirectory, user.email)
    if directory is not None:
        directory.is_active = active
    if not active:
        await invalidate_tokens(session, user)
    await session.flush()
    return user


async def lock_active_admins(session: AsyncSession, tenant: TenantContext) -> list[UUID]:
    """Lock every active admin row in the organization and return their ids.

    Used to make "is this the last admin?" a decision that cannot be raced. Two
    concurrent requests each demoting a different one of the final two admins
    would otherwise both observe two admins and both succeed, leaving the
    organization with none and no way back in except a platform operator.
    """
    result = await session.execute(
        select(User.id)
        .where(
            User.organization_id == tenant.organization_id,
            User.role == Role.ADMIN,
            User.is_active.is_(True),
        )
        .with_for_update()
    )
    return list(result.scalars().all())
