"""Tenant scope and the authenticated principal.

The single rule this module exists to enforce: **tenant scope comes from the
authenticated identity, never from request parameters.** A caller may narrow
within the scope their token grants; they may never widen it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from app.core.enums import Role
from app.core.errors import TenantScopeError


@dataclass(frozen=True, slots=True)
class TenantContext:
    """The org (and optionally location) a unit of work is confined to.

    ``location_id is None`` means "organization-wide": for an admin that is full
    org scope; for a stored record it means the knowledge is shared by every
    location rather than belonging to one.
    """

    organization_id: UUID
    location_id: UUID | None = None

    def narrowed_to(self, location_id: UUID | None) -> TenantContext:
        """Return a context scoped to ``location_id``.

        A principal already pinned to one location cannot move to another, and
        nobody can change organization. Any attempt is a hard error rather than
        a silent fallback, because a silent fallback is a cross-tenant leak.
        """
        if location_id is None:
            return self
        if self.location_id is not None and self.location_id != location_id:
            raise TenantScopeError(
                "Token is scoped to a different location",
                details={"requested_location_id": str(location_id)},
            )
        return TenantContext(organization_id=self.organization_id, location_id=location_id)

    def assert_owns(self, organization_id: UUID) -> None:
        if organization_id != self.organization_id:
            raise TenantScopeError("Record belongs to a different organization")


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated user behind a request (or the worker behind a job)."""

    user_id: UUID
    organization_id: UUID
    role: Role
    location_id: UUID | None = None
    email: str | None = None
    scopes: frozenset[str] = field(default_factory=frozenset)

    @property
    def tenant(self) -> TenantContext:
        return TenantContext(
            organization_id=self.organization_id,
            location_id=self.location_id,
        )

    @property
    def is_admin(self) -> bool:
        return self.role is Role.ADMIN

    def require_admin(self) -> None:
        if not self.is_admin:
            raise TenantScopeError("Administrator role required")

    def has_scope(self, scope: str) -> bool:
        return self.is_admin or scope in self.scopes


@dataclass(frozen=True, slots=True)
class PlatformPrincipal:
    """An operator of the deployment, belonging to no organization.

    Deliberately a *separate type* from :class:`Principal` rather than another
    role on it. Tenant endpoints depend on ``Principal``, so a platform token
    cannot satisfy them by accident -- the type system refuses it before any
    permission check runs.

    To act inside an organization a platform owner names it explicitly, and the
    database session is scoped to that one organization. No request ever sees
    two tenants' rows.
    """

    user_id: UUID
    email: str

    def scope_to(self, organization_id: UUID) -> TenantContext:
        """The tenant this operator is acting on behalf of, for one request."""
        return TenantContext(organization_id=organization_id)


#: Scopes granted implicitly by role. Kept here so tools and endpoints agree.
ROLE_SCOPES: dict[Role, frozenset[str]] = {
    Role.USER: frozenset({"knowledge:read", "tools:basic"}),
    Role.ADMIN: frozenset(
        {
            "knowledge:read",
            "knowledge:write",
            "documents:write",
            "ingestion:manage",
            "tools:basic",
            "tools:admin",
            "org:manage",
        }
    ),
}


def scopes_for(role: Role) -> frozenset[str]:
    return ROLE_SCOPES.get(role, frozenset())


#: Stands in for "nobody in particular" on the public site. A fixed value rather
#: than a fresh uuid4 per request, so it is recognisable in logs and audit rows
#: as anonymous traffic instead of looking like thousands of distinct users.
PUBLIC_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


def public_principal(organization_id: UUID) -> Principal:
    """The identity behind a question asked by a visitor who is not signed in.

    Deliberately an ordinary :class:`Principal`, so every tenant check downstream
    applies to it unchanged -- the only thing unusual about it is how it was
    obtained. It carries no location, which
    ``Retriever._levels`` already reads as organization-wide knowledge only,
    with no path to any one branch's private material.

    It is granted the plain-user scopes and nothing more: read knowledge, use
    the basic tools. Anything that writes is out of reach by construction rather
    than by a check somewhere remembering to say no.
    """
    return Principal(
        user_id=PUBLIC_USER_ID,
        organization_id=organization_id,
        role=Role.USER,
        location_id=None,
        email=None,
        scopes=ROLE_SCOPES[Role.USER],
    )


def system_principal(organization_id: UUID, user_id: UUID) -> Principal:
    """Principal used by background workers acting on behalf of an organization.

    Workers still go through the same tenant machinery as HTTP requests: there is
    no unscoped path to the database.
    """
    return Principal(
        user_id=user_id,
        organization_id=organization_id,
        role=Role.ADMIN,
        scopes=ROLE_SCOPES[Role.ADMIN],
    )
