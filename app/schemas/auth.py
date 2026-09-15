"""Authentication request/response models."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from app.core.enums import Role
from app.schemas.common import UserOut


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


class UserUpdate(BaseModel):
    """A partial update. Absent means "leave alone"; null means "clear".

    Those two must stay distinguishable -- "unpin this user from their branch"
    and "do not touch their branch" are different requests that would otherwise
    look identical. The endpoint uses ``model_dump(exclude_unset=True)`` to keep
    them apart, which is why no field here has a non-null default.
    """

    full_name: str | None = Field(default=None, max_length=200)
    role: Role | None = None
    location_id: UUID | None = None

    # Deliberately absent: `email` (changing it means a second write to the
    # un-scoped login directory), `is_active` (that is what disable/enable is
    # for) and `organization_id` (moving a user between tenants is not an edit).


class PasswordResetRequest(BaseModel):
    """Reset another user's password. Omit to have one generated."""

    password: str | None = Field(default=None, min_length=12, max_length=256)


class PasswordResetResponse(BaseModel):
    user: UserOut
    #: Returned exactly once, and only when the server generated it. Stored as a
    #: hash, so it cannot be retrieved again.
    password: str | None = None


class PasswordChangeRequest(BaseModel):
    """Change your own password. The current one is required as proof."""

    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)
