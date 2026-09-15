"""Authentication request/response models."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from app.core.enums import Role


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    #: Echoed so a client can render "signed in to Sagar Hotels, Ginza" without
    #: a second round trip.
    organization: str | None = None
    location: str | None = None
    role: Role | None = None


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=256)
    full_name: str | None = Field(default=None, max_length=200)
    role: Role = Role.USER
    #: Organization always comes from the caller's token, never from here.
    location_id: UUID | None = None
