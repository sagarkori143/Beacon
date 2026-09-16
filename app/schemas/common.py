"""Shared response shapes."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    """One page of a list, with enough context to render a pager.

    ``total`` is what lets a table say "42 results" rather than leaving the
    client to infer "maybe more" from a full page.
    """

    items: list[T]
    total: int | None = None
    limit: int = 50
    offset: int = 0

    # A plain @property is invisible to Pydantic v2, so this would never reach
    # the client -- the caller would have to recompute it from the other three.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_more(self) -> bool:
        return self.total is not None and self.offset + len(self.items) < self.total


class HealthStatus(BaseModel):
    status: str = Field(description="'ok' or 'degraded'")
    version: str = "0.1.0"
    checks: dict[str, Any] = Field(default_factory=dict)


class OrganizationOut(ORMModel):
    id: UUID
    name: str
    slug: str
    is_active: bool
    created_at: datetime


class LocationOut(ORMModel):
    id: UUID
    organization_id: UUID
    name: str
    slug: str
    timezone: str
    is_active: bool
    settings: dict[str, Any] = Field(default_factory=dict)


class LocationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9-]+$")
    timezone: str = Field(default="UTC", max_length=64)
    settings: dict[str, Any] = Field(default_factory=dict)


class UserOut(ORMModel):
    id: UUID
    email: str
    full_name: str | None
    role: str
    location_id: UUID | None
    is_active: bool


class MeOut(BaseModel):
    user: UserOut
    organization: OrganizationOut
    location: LocationOut | None = None
    scopes: list[str] = Field(default_factory=list)


class LocationUpdate(BaseModel):
    """A partial edit to a branch.

    `slug` is absent on purpose: it appears in stored object keys, so changing
    it would orphan every file already written under the old one.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    timezone: str | None = Field(default=None, max_length=64)
    settings: dict[str, Any] | None = None
    is_active: bool | None = None
