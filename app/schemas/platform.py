"""Platform-operator request/response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from app.core.enums import Role
from app.schemas.common import ORMModel


class PlatformLoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class PlatformTokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    email: str
    #: Always "platform". Present so a client can tell the two credential kinds
    #: apart without decoding the token.
    principal_type: str = "platform"


class OperatorOut(ORMModel):
    id: UUID
    email: str
    full_name: str | None
    is_active: bool
    last_login_at: datetime | None


class OperatorCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=256)
    full_name: str | None = Field(default=None, max_length=200)


class OrganizationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    #: Derived from the name when omitted.
    slug: str | None = Field(default=None, max_length=100, pattern=r"^[a-z0-9-]+$")
    settings: dict[str, Any] = Field(default_factory=dict)

    # An organization with no administrator cannot be managed, so the first one
    # is created in the same call. Omit the password to have one generated and
    # returned once.
    admin_email: EmailStr | None = None
    admin_password: str | None = Field(default=None, min_length=12, max_length=256)
    admin_full_name: str | None = Field(default=None, max_length=200)


class OrganizationSummary(ORMModel):
    id: UUID
    name: str
    slug: str
    is_active: bool
    created_at: datetime


class ProvisionedOrganizationOut(BaseModel):
    organization: OrganizationSummary
    admin_email: str | None = None
    #: Returned exactly once, only when the server generated it. Hand it over
    #: out of band; it is not stored in clear text and cannot be retrieved again.
    admin_password: str | None = None


class TenantUserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=256)
    role: Role = Field(
        default=Role.USER,
        description="ADMIN manages the organization; USER is an end customer.",
    )
    location_id: UUID | None = Field(
        default=None,
        description="Pin the user to one location. Omit for organization-wide access.",
    )
    full_name: str | None = Field(default=None, max_length=200)


class TenantUserOut(ORMModel):
    id: UUID
    organization_id: UUID
    email: str
    full_name: str | None
    role: Role
    location_id: UUID | None
    is_active: bool
    created_at: datetime


class PlatformLocationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str | None = Field(default=None, max_length=100, pattern=r"^[a-z0-9-]+$")
    timezone: str = Field(default="UTC", max_length=64)
    settings: dict[str, Any] = Field(default_factory=dict)
