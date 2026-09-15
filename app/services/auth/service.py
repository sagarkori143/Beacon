"""Authentication.

Login is the one operation that has to find a user before any organization is
known, and it is handled in two steps so that gap stays as small as possible:
the global directory maps an email to an organization, then the real user row --
password hash, role, location -- is read inside a properly scoped session.

The alternative, a policy exception on the ``users`` table itself, would expose
every user's hash and role to an unscoped query. See ``docs/tenant-isolation.md``.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from uuid import UUID

from app.core.config import Settings
from app.core.db import UnitOfWork, tenant_session, unscoped_session
from app.core.enums import AuditAction, Role
from app.core.errors import AuthenticationError, ConflictError
from app.core.logging import get_logger
from app.core.security import (
    TokenPair,
    create_token_pair,
    decode_token,
    hash_password,
    needs_rehash,
    normalize_email,
    verify_password,
)
from app.core.tenancy import Principal, TenantContext
from app.repositories import audit as audit_repo
from app.repositories import user as user_repo

log = get_logger(__name__)

#: Verified against on a failed directory lookup so that a request for an
#: unknown email costs the same time as one for a known email. Without it,
#: response timing enumerates valid accounts.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(32))


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    principal: Principal
    tokens: TokenPair
    organization_name: str
    location_name: str | None


class AuthService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def login(
        self,
        email: str,
        password: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
        request_id: str | None = None,
    ) -> AuthenticatedUser:
        normalized = normalize_email(email)

        async with unscoped_session(self.settings) as session:
            directory = await user_repo.resolve_directory(session, normalized)

        if directory is None or not directory.is_active:
            # Same work as the success path, so timing reveals nothing.
            verify_password(password, _DUMMY_HASH)
            log.info("login_failed", reason="unknown_email")
            raise AuthenticationError("Invalid email or password")

        tenant = TenantContext(organization_id=directory.organization_id)

        async with tenant_session(tenant.organization_id, self.settings) as session:
            user = await user_repo.get_user_by_email(session, tenant, normalized)

            if (
                user is None
                or not user.is_active
                or not verify_password(password, user.password_hash)
            ):
                await audit_repo.record(
                    session,
                    organization_id=tenant.organization_id,
                    action=AuditAction.LOGIN_FAILURE,
                    outcome="FAILURE",
                    actor_user_id=user.id if user else None,
                    ip_address=ip_address,
                    user_agent=user_agent,
                    request_id=request_id,
                )
                log.info("login_failed", reason="bad_credentials")
                raise AuthenticationError("Invalid email or password")

            # Transparently upgrade a hash produced with weaker parameters.
            if needs_rehash(user.password_hash):
                user.password_hash = hash_password(password)

            await user_repo.record_login(session, user)

            from app.repositories.organization import get_location, get_organization

            organization = await get_organization(session, tenant.organization_id)
            location = (
                await get_location(session, tenant, user.location_id) if user.location_id else None
            )

            tokens = create_token_pair(
                settings=self.settings.security,
                user_id=user.id,
                organization_id=user.organization_id,
                role=user.role,
                location_id=user.location_id,
                token_version=user.token_version,
                email=user.email,
            )
            principal = Principal(
                user_id=user.id,
                organization_id=user.organization_id,
                role=user.role,
                location_id=user.location_id,
                email=user.email,
                scopes=_scopes(user.role),
            )

            await audit_repo.record(
                session,
                organization_id=tenant.organization_id,
                action=AuditAction.LOGIN_SUCCESS,
                actor_user_id=user.id,
                location_id=user.location_id,
                ip_address=ip_address,
                user_agent=user_agent,
                request_id=request_id,
            )
            result = AuthenticatedUser(
                principal=principal,
                tokens=tokens,
                organization_name=organization.name,
                location_name=location.name if location else None,
            )

        log.info("login_succeeded", user_id=str(principal.user_id))
        return result

    async def refresh(self, refresh_token: str) -> TokenPair:
        """Exchange a refresh token for a new pair.

        The user row is re-read rather than trusting the token's claims: a role
        change, a deactivation or a forced logout must take effect at the next
        refresh, not at the refresh token's natural expiry two weeks later.
        """
        claims = decode_token(refresh_token, self.settings.security, expect="refresh")

        async with tenant_session(claims.organization_id, self.settings) as session:
            user = await user_repo.get_user(
                session, TenantContext(organization_id=claims.organization_id), claims.subject
            )
            if not user.is_active:
                raise AuthenticationError("Account is disabled")
            if user.token_version != claims.token_version:
                raise AuthenticationError("Token has been revoked")

            return create_token_pair(
                settings=self.settings.security,
                user_id=user.id,
                organization_id=user.organization_id,
                role=user.role,
                location_id=user.location_id,
                token_version=user.token_version,
                email=user.email,
            )

    async def authenticate(self, token: str) -> Principal:
        """Resolve a bearer token to a principal.

        Only the signature and the claims are checked here -- no database round
        trip per request. Revocation works through ``token_version``, which is
        verified on refresh; access tokens are short-lived for exactly that
        reason.
        """
        claims = decode_token(token, self.settings.security, expect="access")
        return claims.to_principal()

    async def create_user(
        self,
        uow: UnitOfWork,
        tenant: TenantContext,
        *,
        email: str,
        password: str,
        role: Role = Role.USER,
        location_id: UUID | None = None,
        full_name: str | None = None,
    ) -> Principal:
        # The login directory is global, so an address is unique across the
        # whole deployment. Catching a duplicate here turns what would surface
        # as an unhandled primary-key violation -- a 500 -- into a plain 409.
        normalized = normalize_email(email)
        async with unscoped_session(self.settings) as session:
            if await user_repo.resolve_directory(session, normalized) is not None:
                raise ConflictError(f"A user with email '{normalized}' already exists")

        async with uow.begin() as session:
            user = await user_repo.create_user(
                session,
                tenant,
                email=normalized,
                password_hash=hash_password(password),
                role=role,
                location_id=location_id,
                full_name=full_name,
            )
            return Principal(
                user_id=user.id,
                organization_id=user.organization_id,
                role=user.role,
                location_id=user.location_id,
                email=user.email,
                scopes=_scopes(user.role),
            )

    async def change_password(
        self,
        uow: UnitOfWork,
        principal: Principal,
        *,
        current_password: str,
        new_password: str,
    ) -> TokenPair:
        """Change your own password, proving you know the current one.

        Returns a fresh token pair. Changing a password revokes every
        outstanding token for the account -- including the one that made this
        request -- so without handing back new tokens the caller would be signed
        out by their own successful action.
        """
        async with uow.begin() as session:
            user = await user_repo.get_user(session, principal.tenant, principal.user_id)

            if not verify_password(current_password, user.password_hash):
                await audit_repo.record(
                    session,
                    organization_id=principal.organization_id,
                    action=AuditAction.USER_PASSWORD_CHANGE,
                    outcome="FAILURE",
                    actor_user_id=user.id,
                    resource_type="user",
                    resource_id=user.id,
                )
                log.info("password_change_failed", user_id=str(user.id))
                raise AuthenticationError("Current password is incorrect")

            await user_repo.set_password(session, user, hash_password(new_password))
            await audit_repo.record(
                session,
                organization_id=principal.organization_id,
                action=AuditAction.USER_PASSWORD_CHANGE,
                actor_user_id=user.id,
                resource_type="user",
                resource_id=user.id,
            )
            tokens = create_token_pair(
                settings=self.settings.security,
                user_id=user.id,
                organization_id=user.organization_id,
                role=user.role,
                location_id=user.location_id,
                token_version=user.token_version,
                email=user.email,
            )

        log.info("password_changed", user_id=str(principal.user_id))
        return tokens


def _scopes(role: Role) -> frozenset[str]:
    from app.core.tenancy import scopes_for

    return scopes_for(role)
