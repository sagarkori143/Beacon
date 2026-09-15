"""Users, plus the one deliberately-global lookup table in the system."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import Role
from app.models.base import Base, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.models.organization import Organization


class User(UUIDMixin, TimestampMixin, Base):
    """A person in exactly one organization, optionally pinned to one location.

    A user pinned to a location can never see another location's private
    knowledge; a user with ``location_id IS NULL`` sees organization-wide
    knowledge and, if an admin, may query any location within the org.
    """

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("organization_id", "email"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("locations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    email: Mapped[str] = mapped_column(String(320), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[Role] = mapped_column(
        Enum(Role, name="user_role", native_enum=False, length=20),
        nullable=False,
        default=Role.USER,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Incremented to invalidate every outstanding token for this user (password
    # change, forced logout). Checked on every request against the `tv` claim.
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    organization: Mapped[Organization] = relationship(back_populates="users", lazy="raise")


class UserDirectory(Base):
    """Global email -> organization mapping used only to resolve login.

    This is the single intentional exception to tenant scoping, and it exists to
    avoid a far worse alternative. Login must find a user before any organization
    is known, so *something* has to be readable without tenant context. Rather
    than punching a hole in the ``users`` RLS policy -- which would expose
    password hashes, roles and names -- this table is kept deliberately tiny:
    an email, the organization it belongs to, and the user id. The real user row
    is then read under proper tenant scope.

    See ``docs/tenant-isolation.md``.
    """

    __tablename__ = "user_directory"

    email: Mapped[str] = mapped_column(String(320), primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
