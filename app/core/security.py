"""Password hashing and JWT issuance/verification.

Tokens carry the tenant. That is the whole point: every downstream decision --
which rows RLS exposes, which location a query may see, whether an upload is
allowed -- derives from claims signed here, never from a request body.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import SecuritySettings
from app.core.enums import Role
from app.core.errors import AuthenticationError
from app.core.logging import get_logger
from app.core.tenancy import Principal, scopes_for

log = get_logger(__name__)

# Argon2id with library defaults, which track current guidance. Chosen over
# bcrypt for its memory-hardness and because it has no 72-byte input limit.
_hasher = PasswordHasher()

TokenType = Literal["access", "refresh"]


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return True


def needs_rehash(password_hash: str) -> bool:
    """True when the stored hash used weaker parameters than we now use."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = 0


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """Decoded, validated JWT payload."""

    subject: UUID
    organization_id: UUID
    role: Role
    token_type: TokenType
    location_id: UUID | None
    token_version: int
    jti: str
    email: str | None = None
    expires_at: datetime | None = None

    def to_principal(self) -> Principal:
        return Principal(
            user_id=self.subject,
            organization_id=self.organization_id,
            role=self.role,
            location_id=self.location_id,
            email=self.email,
            scopes=scopes_for(self.role),
        )


def create_token(
    *,
    settings: SecuritySettings,
    user_id: UUID,
    organization_id: UUID,
    role: Role,
    location_id: UUID | None,
    token_version: int,
    email: str | None = None,
    token_type: TokenType = "access",
) -> tuple[str, int]:
    """Issue a signed token. Returns the token and its lifetime in seconds."""
    ttl = settings.access_token_ttl_s if token_type == "access" else settings.refresh_token_ttl_s
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "org": str(organization_id),
        "loc": str(location_id) if location_id else None,
        "role": role.value,
        # Bumping the user's token_version invalidates every outstanding token
        # without needing a denylist.
        "tv": token_version,
        "typ": token_type,
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl)).timestamp()),
    }
    if email:
        payload["email"] = email

    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, ttl


def create_token_pair(
    *,
    settings: SecuritySettings,
    user_id: UUID,
    organization_id: UUID,
    role: Role,
    location_id: UUID | None,
    token_version: int,
    email: str | None = None,
) -> TokenPair:
    access, ttl = create_token(
        settings=settings,
        user_id=user_id,
        organization_id=organization_id,
        role=role,
        location_id=location_id,
        token_version=token_version,
        email=email,
        token_type="access",
    )
    refresh, _ = create_token(
        settings=settings,
        user_id=user_id,
        organization_id=organization_id,
        role=role,
        location_id=location_id,
        token_version=token_version,
        email=email,
        token_type="refresh",
    )
    return TokenPair(access_token=access, refresh_token=refresh, expires_in=ttl)


def decode_token(
    token: str,
    settings: SecuritySettings,
    *,
    expect: TokenType = "access",
) -> TokenClaims:
    """Verify and decode a token.

    Rejects a refresh token presented as an access token. Without that check,
    a long-lived refresh token would work as a bearer credential and silently
    defeat the short access-token lifetime.
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub", "org", "typ"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("Invalid authentication token") from exc

    if payload.get("typ") != expect:
        raise AuthenticationError(f"Expected a {expect} token")

    try:
        role = Role(payload["role"])
        location = payload.get("loc")
        return TokenClaims(
            subject=UUID(payload["sub"]),
            organization_id=UUID(payload["org"]),
            role=role,
            token_type=expect,
            location_id=UUID(location) if location else None,
            token_version=int(payload.get("tv", 0)),
            jti=str(payload.get("jti", "")),
            email=payload.get("email"),
            expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        )
    except (KeyError, ValueError) as exc:
        raise AuthenticationError("Malformed authentication token") from exc


def normalize_email(email: str) -> str:
    """Casefold for lookups.

    The directory and the user table both store the normalized form, so
    ``Alice@Example.com`` and ``alice@example.com`` cannot become two accounts.
    """
    return email.strip().casefold()
