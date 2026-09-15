"""Platform-operator endpoints: provisioning tenants and their users.

These sit under ``/platform`` and accept **only** a platform credential. A
tenant token is refused here, and a platform token is refused on every tenant
endpoint -- the two are different credential types, not two roles on one.

What an operator can do: create organizations, locations and users, and enable
or disable users. What they deliberately cannot do: read any tenant's documents,
chunks or conversations. For that they create themselves a user in that
organization, which is auditable and keeps the isolation invariant -- no
credential in the system can see two organizations' data in one request.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, status

from app.api.deps import get_platform_service
from app.core.enums import Role
from app.core.errors import AuthenticationError
from app.core.tenancy import PlatformPrincipal
from app.schemas.auth import RefreshRequest
from app.schemas.common import LocationOut
from app.schemas.platform import (
    OperatorCreate,
    OperatorOut,
    OrganizationCreate,
    OrganizationSummary,
    PlatformLocationCreate,
    PlatformLoginRequest,
    PlatformTokenResponse,
    ProvisionedOrganizationOut,
    TenantUserCreate,
    TenantUserOut,
)
from app.services.platform.service import PlatformService

router = APIRouter(prefix="/platform", tags=["platform"])


async def current_operator(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    service: PlatformService = Depends(get_platform_service),
) -> PlatformPrincipal:
    """Resolve a platform bearer token, or reject the request."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthenticationError("Missing bearer token")

    operator = await service.authenticate(authorization[7:].strip())
    request.state.trace.user_id = operator.user_id
    return operator


CurrentOperator = Annotated[PlatformPrincipal, Depends(current_operator)]
Service = Annotated[PlatformService, Depends(get_platform_service)]


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


@router.post("/auth/login", response_model=PlatformTokenResponse)
async def login(payload: PlatformLoginRequest, service: Service) -> PlatformTokenResponse:
    """Sign in as a platform operator.

    The first operator is created with ``scripts/create_owner.py`` -- a local
    command, not an endpoint, because an endpoint that mints the first
    all-powerful account is an open door until someone remembers to close it.
    """
    result = await service.login(payload.email, payload.password)
    return PlatformTokenResponse(
        access_token=result.tokens.access_token,
        refresh_token=result.tokens.refresh_token,
        expires_in=result.tokens.expires_in,
        email=result.principal.email,
    )


@router.post("/auth/refresh", response_model=PlatformTokenResponse)
async def refresh(payload: RefreshRequest, service: Service) -> PlatformTokenResponse:
    """Exchange a refresh token for a new pair.

    Deliberately takes no access token -- requiring one would defeat the purpose,
    since refresh exists precisely for when the access token has expired. The
    operator row is re-read instead, so a disabled account is refused here.
    """
    result = await service.refresh(payload.refresh_token)
    return PlatformTokenResponse(
        access_token=result.tokens.access_token,
        refresh_token=result.tokens.refresh_token,
        expires_in=result.tokens.expires_in,
        email=result.principal.email,
    )


@router.get("/auth/me", response_model=dict)
async def me(operator: CurrentOperator) -> dict:
    return {
        "user_id": str(operator.user_id),
        "email": operator.email,
        "principal_type": "platform",
        "can": [
            "create organizations",
            "create locations in any organization",
            "create users in any organization",
            "enable/disable users",
        ],
        "cannot": [
            "read any tenant's documents, search results or conversations",
        ],
    }


@router.post("/operators", response_model=OperatorOut, status_code=status.HTTP_201_CREATED)
async def create_operator(
    payload: OperatorCreate, operator: CurrentOperator, service: Service
) -> OperatorOut:
    """Add another platform operator."""
    created = await service.create_operator(
        email=payload.email, password=payload.password, full_name=payload.full_name
    )
    return OperatorOut.model_validate(created)


# ---------------------------------------------------------------------------
# Organizations
# ---------------------------------------------------------------------------


@router.get("/organizations", response_model=list[OrganizationSummary])
async def list_organizations(
    operator: CurrentOperator, service: Service
) -> list[OrganizationSummary]:
    """Every tenant on the deployment."""
    organizations = await service.list_organizations()
    return [OrganizationSummary.model_validate(o) for o in organizations]


@router.post(
    "/organizations",
    response_model=ProvisionedOrganizationOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_organization(
    payload: OrganizationCreate, operator: CurrentOperator, service: Service
) -> ProvisionedOrganizationOut:
    """Create a tenant, optionally with its first administrator.

    Omit ``admin_password`` and one is generated and returned **once**. It is
    stored only as a hash, so it cannot be retrieved again.
    """
    result = await service.create_organization(
        operator,
        name=payload.name,
        slug=payload.slug,
        admin_email=payload.admin_email,
        admin_password=payload.admin_password,
        admin_full_name=payload.admin_full_name,
        settings_payload=payload.settings,
    )
    return ProvisionedOrganizationOut(
        organization=OrganizationSummary.model_validate(result.organization),
        admin_email=result.admin.email if result.admin else None,
        admin_password=result.admin_password,
    )


@router.get("/organizations/{organization_id}", response_model=OrganizationSummary)
async def get_organization(
    organization_id: UUID, operator: CurrentOperator, service: Service
) -> OrganizationSummary:
    return OrganizationSummary.model_validate(await service.get_organization(organization_id))


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


@router.get("/organizations/{organization_id}/locations", response_model=list[LocationOut])
async def list_locations(
    organization_id: UUID, operator: CurrentOperator, service: Service
) -> list[LocationOut]:
    locations = await service.list_locations(organization_id)
    return [LocationOut.model_validate(loc) for loc in locations]


@router.post(
    "/organizations/{organization_id}/locations",
    response_model=LocationOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_location(
    organization_id: UUID,
    payload: PlatformLocationCreate,
    operator: CurrentOperator,
    service: Service,
) -> LocationOut:
    location = await service.create_location(
        operator,
        organization_id=organization_id,
        name=payload.name,
        slug=payload.slug,
        timezone=payload.timezone,
        settings_payload=payload.settings,
    )
    return LocationOut.model_validate(location)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@router.get("/organizations/{organization_id}/users", response_model=list[TenantUserOut])
async def list_users(
    organization_id: UUID, operator: CurrentOperator, service: Service
) -> list[TenantUserOut]:
    users = await service.list_users(organization_id)
    return [TenantUserOut.model_validate(u) for u in users]


@router.post(
    "/organizations/{organization_id}/users",
    response_model=TenantUserOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_user(
    organization_id: UUID,
    payload: TenantUserCreate,
    operator: CurrentOperator,
    service: Service,
) -> TenantUserOut:
    """Create a user inside an organization.

    ``role=ADMIN`` manages that organization -- uploads documents, manages
    knowledge, creates further users. ``role=USER`` is an end customer: they can
    search and chat over the knowledge their location and organization expose,
    and nothing else.

    A ``location_id`` pins the user to one location. They then see that
    location's knowledge plus organization-wide knowledge, and can never reach
    another location's.
    """
    user = await service.create_user(
        operator,
        organization_id=organization_id,
        email=payload.email,
        password=payload.password,
        role=payload.role,
        location_id=payload.location_id,
        full_name=payload.full_name,
    )
    return TenantUserOut.model_validate(user)


@router.post(
    "/organizations/{organization_id}/users/{user_id}/disable",
    response_model=TenantUserOut,
)
async def disable_user(
    organization_id: UUID, user_id: UUID, operator: CurrentOperator, service: Service
) -> TenantUserOut:
    """Disable a user and revoke their outstanding tokens immediately."""
    user = await service.set_user_active(
        operator, organization_id=organization_id, user_id=user_id, active=False
    )
    return TenantUserOut.model_validate(user)


@router.post(
    "/organizations/{organization_id}/users/{user_id}/enable",
    response_model=TenantUserOut,
)
async def enable_user(
    organization_id: UUID, user_id: UUID, operator: CurrentOperator, service: Service
) -> TenantUserOut:
    user = await service.set_user_active(
        operator, organization_id=organization_id, user_id=user_id, active=True
    )
    return TenantUserOut.model_validate(user)


__all__ = ["Role", "router"]
