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

from sqlalchemy import select
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


async def list_users(session: AsyncSession, tenant: TenantContext) -> Sequence[User]:
    result = await session.execute(
        select(User).where(User.organization_id == tenant.organization_id).order_by(User.email)
    )
    return result.scalars().all()


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
