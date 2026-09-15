"""Organization and Location: the two levels of the tenant hierarchy."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.models.user import User


class Organization(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Per-tenant overrides: model pins, allowed providers, feature flags, quotas.
    # Kept as JSONB so adding an org-level knob does not require a migration.
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB().with_variant(JSON, "sqlite"), nullable=False, default=dict
    )

    locations: Mapped[list[Location]] = relationship(
        back_populates="organization",
        cascade="all, delete-orphan",
        lazy="raise",
    )
    users: Mapped[list[User]] = relationship(
        back_populates="organization",
        cascade="all, delete-orphan",
        lazy="raise",
    )

    @property
    def model_pins(self) -> dict[str, str]:
        """Task -> ``provider/model`` pins. Highest-priority routing rule."""
        pins = self.settings.get("model_pins", {})
        return pins if isinstance(pins, dict) else {}

    @property
    def allowed_providers(self) -> list[str] | None:
        """Allow-list of provider names, or ``None`` for "any registered"."""
        allowed = self.settings.get("allowed_providers")
        return allowed if isinstance(allowed, list) else None


class Location(UUIDMixin, TimestampMixin, Base):
    """A site within an organization (a hotel property, branch, store, ...).

    Location-scoped knowledge overrides organization-wide knowledge at answer
    time without the organization content ever being duplicated per location.
    """

    __tablename__ = "locations"
    __table_args__ = (UniqueConstraint("organization_id", "slug"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Structured facts the agent can answer from without retrieval: address,
    # phone, front-desk hours, amenities.
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB().with_variant(JSON, "sqlite"), nullable=False, default=dict
    )

    organization: Mapped[Organization] = relationship(back_populates="locations", lazy="raise")
