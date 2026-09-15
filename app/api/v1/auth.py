"""Authentication endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status

from app.api.deps import CurrentAdmin, CurrentPrincipal, Uow, get_auth_service
from app.core.tenancy import ROLE_SCOPES
from app.schemas.auth import LoginRequest, RefreshRequest, TokenResponse, UserCreate
from app.schemas.common import LocationOut, MeOut, OrganizationOut, UserOut
from app.services.auth.service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    auth: AuthService = Depends(get_auth_service),
) -> TokenResponse:
    """Exchange credentials for an access and refresh token pair.

    The returned access token is what carries tenant scope for every subsequent
    request; no endpoint accepts an organization id from the client.
    """
    result = await auth.login(
        payload.email,
        payload.password,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        request_id=getattr(request.state, "request_id", None),
    )
    return TokenResponse(
        access_token=result.tokens.access_token,
        refresh_token=result.tokens.refresh_token,
        expires_in=result.tokens.expires_in,
        organization=result.organization_name,
        location=result.location_name,
        role=result.principal.role,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    payload: RefreshRequest,
    auth: AuthService = Depends(get_auth_service),
) -> TokenResponse:
    """Exchange a refresh token for a new pair.

    Re-reads the user, so a deactivation or role change takes effect here rather
    than at the refresh token's natural expiry.
    """
    tokens = await auth.refresh(payload.refresh_token)
    return TokenResponse(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
    )


@router.get("/me", response_model=MeOut)
async def me(principal: CurrentPrincipal, uow: Uow) -> MeOut:
    """Who the caller is, and what they can see."""
    from app.repositories.organization import get_location, get_organization
    from app.repositories.user import get_user

    async with uow.begin() as session:
        user = await get_user(session, principal.tenant, principal.user_id)
        organization = await get_organization(session, principal.organization_id)
        location = (
            await get_location(session, principal.tenant, principal.location_id)
            if principal.location_id
            else None
        )

        return MeOut(
            user=UserOut.model_validate(user),
            organization=OrganizationOut.model_validate(organization),
            location=LocationOut.model_validate(location) if location else None,
            scopes=sorted(ROLE_SCOPES.get(principal.role, frozenset())),
        )


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreate,
    admin: CurrentAdmin,
    uow: Uow,
    auth: AuthService = Depends(get_auth_service),
) -> UserOut:
    """Create a user in the caller's organization.

    The organization is taken from the admin's token. A location, if given, is
    validated against that organization.
    """
    from app.repositories.organization import get_location
    from app.repositories.user import get_user

    if payload.location_id is not None:
        async with uow.begin() as session:
            await get_location(session, admin.tenant, payload.location_id)

    created = await auth.create_user(
        uow,
        admin.tenant,
        email=payload.email,
        password=payload.password,
        role=payload.role,
        location_id=payload.location_id,
        full_name=payload.full_name,
    )

    async with uow.begin() as session:
        user = await get_user(session, admin.tenant, created.user_id)
        return UserOut.model_validate(user)
