"""Shared response shapes."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int | None = None
    limit: int = 50
    offset: int = 0

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
