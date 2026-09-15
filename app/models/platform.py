"""Platform-level accounts.

A platform owner operates the deployment: they provision organizations and the
users inside them. They do not belong to any organization, which is why this is
a separate table rather than a nullable ``organization_id`` on ``users``.

Keeping them apart matters for more than tidiness. ``users`` is a tenant table
with Row Level Security forced on it, and every row must have an owning
organization for the policy to mean anything. A nullable column there would
punch a permanent hole in the policy for the sake of a handful of rows.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class PlatformUser(UUIDMixin, TimestampMixin, Base):
    """An operator of the whole deployment, above any single tenant.

    Deliberately *not* able to read tenant knowledge. A platform owner can create
    an organization and its users; to read a tenant's documents they must hold a
    user account in that tenant. That keeps the isolation invariant intact -- no
    credential in the system can see two organizations' data in one request.
    """

    __tablename__ = "platform_users"

    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    full_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Incremented to revoke every outstanding token for this account.
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
