"""Platform operations: provisioning tenants and the users inside them.

Every method here names the organization it is acting on, and opens a session
scoped to exactly that one. A platform operator can enumerate tenants and create
things inside a named tenant; no operation reads across two of them.

What is deliberately absent: any way to read a tenant's documents, chunks or
conversations. To do that, an operator creates themselves a user account in that
organization -- which is auditable, and leaves the isolation invariant intact.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, text

from app.core.config import Settings
from app.core.db import ORG_GUC, platform_session, unscoped_session
from app.core.enums import AuditAction, Role
from app.core.errors import AuthenticationError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.security import (
    TokenPair,
    create_platform_token_pair,
    decode_platform_token,
    hash_password,
    needs_rehash,
    normalize_email,
    verify_password,
)
from app.core.tenancy import PlatformPrincipal, TenantContext
from app.models.organization import Location, Organization
from app.models.platform import PlatformUser
from app.models.user import User
from app.repositories import audit as audit_repo
from app.repositories import organization as org_repo
from app.repositories import user as user_repo
from app.services.documents.service import slugify

log = get_logger(__name__)

#: Same constant-time shape as tenant login: an unknown email must cost the same
#: as a known one, or response timing enumerates operator accounts.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(32))


@dataclass(frozen=True, slots=True)
class AuthenticatedOperator:
    principal: PlatformPrincipal
    tokens: TokenPair


@dataclass(frozen=True, slots=True)
class ProvisionedOrganization:
    organization: Organization
    admin: User | None
    admin_password: str | None


class PlatformService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # -- authentication ------------------------------------------------------

    async def login(self, email: str, password: str) -> AuthenticatedOperator:
        normalized = normalize_email(email)

        async with unscoped_session(self.settings) as session:
            result = await session.execute(
                select(PlatformUser).where(PlatformUser.email == normalized)
            )
            operator = result.scalar_one_or_none()

            if operator is None or not operator.is_active:
                verify_password(password, _DUMMY_HASH)
                log.info("platform_login_failed", reason="unknown_account")
                raise AuthenticationError("Invalid email or password")

            if not verify_password(password, operator.password_hash):
                log.info("platform_login_failed", reason="bad_credentials")
                raise AuthenticationError("Invalid email or password")

            if needs_rehash(operator.password_hash):
                operator.password_hash = hash_password(password)

            from datetime import UTC, datetime

            operator.last_login_at = datetime.now(UTC)
            tokens = create_platform_token_pair(
                settings=self.settings.security,
                user_id=operator.id,
                email=operator.email,
                token_version=operator.token_version,
            )
            principal = PlatformPrincipal(user_id=operator.id, email=operator.email)

        log.info("platform_login_succeeded", user_id=str(principal.user_id))
        return AuthenticatedOperator(principal=principal, tokens=tokens)

    async def authenticate(self, token: str) -> PlatformPrincipal:
        return decode_platform_token(token, self.settings.security).to_principal()

    async def refresh(self, refresh_token: str) -> AuthenticatedOperator:
        """Exchange a refresh token for a new pair.

        Takes no access token: the whole point of a refresh is that the access
        token may already have expired. The account row is re-read instead, so a
        revocation takes effect here rather than at the refresh token's natural
        expiry.
        """
        claims = decode_platform_token(refresh_token, self.settings.security, expect="refresh")

        async with unscoped_session(self.settings) as session:
            operator = await session.get(PlatformUser, claims.subject)
            if operator is None or not operator.is_active:
                raise AuthenticationError("Account is disabled")
            if operator.token_version != claims.token_version:
                raise AuthenticationError("Token has been revoked")

            tokens = create_platform_token_pair(
                settings=self.settings.security,
                user_id=operator.id,
                email=operator.email,
                token_version=operator.token_version,
            )
            return AuthenticatedOperator(
                principal=PlatformPrincipal(user_id=operator.id, email=operator.email),
                tokens=tokens,
            )

    # -- organizations -------------------------------------------------------

    async def list_organizations(self) -> Sequence[Organization]:
        """Every tenant. Readable only with the platform flag set."""
        async with platform_session(None, self.settings) as session:
            result = await session.execute(select(Organization).order_by(Organization.name))
            return result.scalars().all()

    async def get_organization(self, organization_id: UUID) -> Organization:
        async with platform_session(organization_id, self.settings) as session:
            organization = await session.get(Organization, organization_id)
            if organization is None:
                raise NotFoundError(f"Organization not found ({organization_id})")
            return organization

    async def create_organization(
        self,
        operator: PlatformPrincipal,
        *,
        name: str,
        slug: str | None = None,
        admin_email: str | None = None,
        admin_password: str | None = None,
        admin_full_name: str | None = None,
        settings_payload: dict | None = None,
    ) -> ProvisionedOrganization:
        """Create a tenant, optionally with its first administrator.

        Creating the admin in the same call is the common case: an organization
        with no administrator cannot be managed, so the operator would have to
        make a second call immediately anyway.
        """
        resolved_slug = slugify(slug or name, max_len=100)

        async with platform_session(None, self.settings) as session:
            existing = await org_repo.get_organization_by_slug(session, resolved_slug)
            if existing is not None:
                raise ConflictError(f"An organization with slug '{resolved_slug}' already exists")

            organization = await org_repo.create_organization(
                session,
                name=name,
                slug=resolved_slug,
                settings=settings_payload or {},
            )
            organization_id = organization.id

            # audit_log is a tenant table: its policy checks organization_id
            # against app.current_org_id, which this session opened empty
            # because the organization did not exist yet. Adopt the new tenant
            # for the rest of the transaction so the record is admitted -- and
            # so it lands in that tenant's own audit trail.
            await session.execute(
                text(f"SELECT set_config('{ORG_GUC}', :org, true)"),
                {"org": str(organization_id)},
            )
            await audit_repo.record(
                session,
                organization_id=organization_id,
                action=AuditAction.ORGANIZATION_CREATE,
                resource_type="organization",
                resource_id=organization_id,
                message=f"created by platform operator {operator.email}",
            )

        log.info(
            "organization_provisioned",
            organization_id=str(organization_id),
            slug=resolved_slug,
            by=str(operator.user_id),
        )

        admin: User | None = None
        generated: str | None = None
        if admin_email:
            # A generated password is returned once and never stored in clear
            # text; the operator hands it over out of band.
            generated = admin_password or secrets.token_urlsafe(18)
            admin = await self.create_user(
                operator,
                organization_id=organization_id,
                email=admin_email,
                password=generated,
                role=Role.ADMIN,
                full_name=admin_full_name,
            )

        organization = await self.get_organization(organization_id)
        return ProvisionedOrganization(
            organization=organization,
            admin=admin,
            admin_password=generated if admin_password is None else None,
        )

    # -- locations -----------------------------------------------------------

    async def create_location(
        self,
        operator: PlatformPrincipal,
        *,
        organization_id: UUID,
        name: str,
        slug: str | None = None,
        timezone: str = "UTC",
        settings_payload: dict | None = None,
    ) -> Location:
        tenant = operator.scope_to(organization_id)
        resolved_slug = slugify(slug or name, max_len=100)

        async with platform_session(organization_id, self.settings) as session:
            await self._require_organization(session, organization_id)

            if await org_repo.get_location_by_slug(session, tenant, resolved_slug):
                raise ConflictError(
                    f"A location with slug '{resolved_slug}' already exists in this organization"
                )
            location = await org_repo.create_location(
                session,
                tenant,
                name=name,
                slug=resolved_slug,
                timezone=timezone,
                settings=settings_payload or {},
            )
            await audit_repo.record(
                session,
                organization_id=organization_id,
                action=AuditAction.LOCATION_CREATE,
                resource_type="location",
                resource_id=location.id,
                message=f"location created by platform operator {operator.email}",
            )
            return location

    async def list_locations(self, organization_id: UUID) -> Sequence[Location]:
        async with platform_session(organization_id, self.settings) as session:
            await self._require_organization(session, organization_id)
            return await org_repo.list_locations(
                session, TenantContext(organization_id=organization_id), include_inactive=True
            )

    # -- users ---------------------------------------------------------------

    async def create_user(
        self,
        operator: PlatformPrincipal,
        *,
        organization_id: UUID,
        email: str,
        password: str,
        role: Role,
        location_id: UUID | None = None,
        full_name: str | None = None,
    ) -> User:
        """Create a user inside a named organization.

        The session is scoped to that organization, so the row lands under the
        right tenant by construction -- the operator cannot write into a
        different one even by supplying the wrong id, because RLS would reject
        the insert.
        """
        tenant = TenantContext(organization_id=organization_id)
        normalized = normalize_email(email)

        # The directory is global, so a duplicate address must be caught before
        # the tenant-scoped insert rather than surfacing as a constraint error.
        async with unscoped_session(self.settings) as session:
            if await user_repo.resolve_directory(session, normalized) is not None:
                raise ConflictError(f"A user with email '{normalized}' already exists")

        async with platform_session(organization_id, self.settings) as session:
            await self._require_organization(session, organization_id)

            if location_id is not None:
                await org_repo.get_location(session, tenant, location_id)

            user = await user_repo.create_user(
                session,
                tenant,
                email=normalized,
                password_hash=hash_password(password),
                role=role,
                location_id=location_id,
                full_name=full_name,
            )
            await audit_repo.record(
                session,
                organization_id=organization_id,
                action=AuditAction.USER_CREATE,
                resource_type="user",
                resource_id=user.id,
                message=f"{role.value} created by platform operator {operator.email}",
            )

        log.info(
            "user_provisioned",
            organization_id=str(organization_id),
            role=role.value,
            by=str(operator.user_id),
        )
        return user

    async def list_users(self, organization_id: UUID) -> Sequence[User]:
        async with platform_session(organization_id, self.settings) as session:
            await self._require_organization(session, organization_id)
            return await user_repo.list_users(
                session, TenantContext(organization_id=organization_id)
            )

    async def set_user_active(
        self, operator: PlatformPrincipal, *, organization_id: UUID, user_id: UUID, active: bool
    ) -> User:
        """Enable or disable a tenant user, revoking their tokens when disabling."""
        tenant = TenantContext(organization_id=organization_id)

        async with platform_session(organization_id, self.settings) as session:
            user = await user_repo.get_user(session, tenant, user_id)
            user.is_active = active
            if not active:
                # Disabling must take effect now, not at token expiry.
                await user_repo.invalidate_tokens(session, user)
            await session.flush()

            await audit_repo.record(
                session,
                organization_id=organization_id,
                action=AuditAction.USER_ENABLE if active else AuditAction.USER_DISABLE,
                resource_type="user",
                resource_id=user.id,
                message=f"by platform operator {operator.email}",
            )
            return user

    # -- helpers -------------------------------------------------------------

    @staticmethod
    async def _require_organization(session: object, organization_id: UUID) -> Organization:
        organization = await session.get(Organization, organization_id)  # type: ignore[attr-defined]
        if organization is None:
            raise NotFoundError(f"Organization not found ({organization_id})")
        return organization

    async def create_operator(
        self, *, email: str, password: str, full_name: str | None = None
    ) -> PlatformUser:
        """Create another platform operator.

        The *first* one cannot be created this way -- see scripts/create_owner.py
        for the bootstrap, which is deliberately a local command rather than an
        endpoint.
        """
        normalized = normalize_email(email)

        async with unscoped_session(self.settings) as session:
            existing = await session.execute(
                select(PlatformUser).where(PlatformUser.email == normalized)
            )
            if existing.scalar_one_or_none() is not None:
                raise ConflictError(f"A platform operator with email '{normalized}' already exists")

            operator = PlatformUser(
                email=normalized,
                full_name=full_name,
                password_hash=hash_password(password),
                is_active=True,
            )
            session.add(operator)
            await session.flush()
            log.info("platform_operator_created", email=normalized)
            return operator
