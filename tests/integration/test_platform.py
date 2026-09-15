"""Platform operators: provisioning, and the limits on what they can reach.

The interesting assertions here are not that provisioning works -- it is that a
platform credential is a *different kind* of credential, and that holding one
still does not let anyone read two tenants' data in a single request.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.core.enums import Role
from app.core.errors import AuthenticationError, ConflictError, NotFoundError
from app.core.security import create_token_pair, decode_platform_token, decode_token
from app.core.tenancy import PlatformPrincipal
from app.models.platform import PlatformUser
from app.services.auth.service import AuthService
from app.services.platform.service import PlatformService, ProvisionedOrganization

pytestmark = [pytest.mark.integration]

#: Long enough to satisfy the minimum the API schema enforces.
OPERATOR_PASSWORD = "operator-password-123"


@pytest.fixture
async def app_engine(settings: Settings, db_engine: object) -> AsyncIterator[None]:
    """Initialise the process-wide engine.

    ``PlatformService`` opens its own sessions rather than taking a UnitOfWork,
    because its whole job is to act *outside* one tenant's scope. That means it
    uses the module-level sessionmaker, so these tests have to stand it up --
    pointed at the unprivileged ``app_rw`` role, so RLS genuinely applies.
    """
    from app.core.db import dispose_engine, init_engine

    await dispose_engine()
    init_engine(settings)
    try:
        yield None
    finally:
        await dispose_engine()


@pytest.fixture
def platform_service(settings: Settings, app_engine: None) -> PlatformService:
    return PlatformService(settings)


@pytest.fixture
def cleanup(owner_engine: object) -> async_sessionmaker:
    """Sessions as the owner role, for removing fixture rows.

    Cleanup runs as the owner because the application role deliberately cannot
    delete an organization: no tenant context admits a row it is about to
    destroy along with its own scope.
    """
    return async_sessionmaker(owner_engine, expire_on_commit=False, autoflush=False)  # type: ignore[arg-type]


@pytest.fixture
async def operator(
    platform_service: PlatformService, cleanup: async_sessionmaker
) -> AsyncIterator[PlatformUser]:
    """A throwaway platform operator, removed afterwards."""
    email = f"op-{uuid.uuid4().hex[:8]}@platform.test"
    created = await platform_service.create_operator(
        email=email, password=OPERATOR_PASSWORD, full_name="Test Operator"
    )
    operator_id = created.id
    try:
        yield created
    finally:
        async with cleanup() as session, session.begin():
            await session.execute(
                text("DELETE FROM platform_users WHERE id = :id"), {"id": operator_id}
            )


@pytest.fixture
async def provisioned(
    platform_service: PlatformService,
    operator: PlatformUser,
    cleanup: async_sessionmaker,
    fake_embedding_space: None,
) -> AsyncIterator[tuple[PlatformPrincipal, ProvisionedOrganization]]:
    """An organization created through the platform API, dropped afterwards."""
    signed_in = await platform_service.login(operator.email, OPERATOR_PASSWORD)
    slug = f"acme-{uuid.uuid4().hex[:8]}"
    result = await platform_service.create_organization(
        signed_in.principal,
        name=f"Acme {slug}",
        slug=slug,
        admin_email=f"admin@{slug}.test",
    )
    organization_id = result.organization.id
    try:
        yield signed_in.principal, result
    finally:
        async with cleanup() as session, session.begin():
            await session.execute(
                text("DELETE FROM organizations WHERE id = :id"), {"id": organization_id}
            )


class TestOperatorAuthentication:
    async def test_login_issues_a_platform_token(self, platform_service, operator) -> None:
        result = await platform_service.login(operator.email, OPERATOR_PASSWORD)
        assert result.principal.email == operator.email

        claims = decode_platform_token(
            result.tokens.access_token, platform_service.settings.security
        )
        assert claims.subject == operator.id

    async def test_a_wrong_password_is_refused(self, platform_service, operator) -> None:
        with pytest.raises(AuthenticationError):
            await platform_service.login(operator.email, "wrong-password-entirely")

    async def test_an_unknown_account_is_refused(self, platform_service) -> None:
        with pytest.raises(AuthenticationError):
            await platform_service.login("nobody@platform.test", "whatever-password")

    async def test_duplicate_operators_are_rejected(self, platform_service, operator) -> None:
        with pytest.raises(ConflictError):
            await platform_service.create_operator(
                email=operator.email, password="another-password-123"
            )

    async def test_refresh_returns_a_new_pair_without_an_access_token(
        self, platform_service, operator
    ) -> None:
        """Refresh must work when the access token has already expired."""
        signed_in = await platform_service.login(operator.email, OPERATOR_PASSWORD)
        refreshed = await platform_service.refresh(signed_in.tokens.refresh_token)

        assert refreshed.principal.email == operator.email
        decode_platform_token(refreshed.tokens.access_token, platform_service.settings.security)

    async def test_an_access_token_cannot_be_used_to_refresh(
        self, platform_service, operator
    ) -> None:
        signed_in = await platform_service.login(operator.email, OPERATOR_PASSWORD)
        with pytest.raises(AuthenticationError):
            await platform_service.refresh(signed_in.tokens.access_token)


class TestCredentialSeparation:
    """A platform token and a tenant token are different kinds of credential.

    This is enforced by the token decoder, before any permission check runs --
    so a carelessly wired dependency cannot let one stand in for the other.
    """

    async def test_a_platform_token_is_not_a_tenant_token(self, platform_service, operator) -> None:
        result = await platform_service.login(operator.email, OPERATOR_PASSWORD)
        with pytest.raises(AuthenticationError):
            decode_token(result.tokens.access_token, platform_service.settings.security)

    async def test_a_tenant_token_is_not_a_platform_token(
        self, platform_service, settings, seeded_org
    ) -> None:
        tenant_tokens = create_token_pair(
            settings=settings.security,
            user_id=seeded_org["admin_id"],
            organization_id=seeded_org["organization_id"],
            role=Role.ADMIN,
            location_id=None,
            token_version=0,
        )
        with pytest.raises(AuthenticationError):
            decode_platform_token(tenant_tokens.access_token, settings.security)


class TestProvisioning:
    async def test_creating_an_organization_with_its_first_admin(
        self, platform_service, provisioned
    ) -> None:
        _, result = provisioned

        assert result.organization.name.startswith("Acme ")
        assert result.admin is not None
        assert result.admin.role is Role.ADMIN
        # Generated once, returned once, stored only as a hash.
        assert result.admin_password
        assert len(result.admin_password) >= 16

    async def test_the_new_admin_can_sign_in_to_their_own_organization(
        self, platform_service, provisioned, settings
    ) -> None:
        _, result = provisioned
        signed_in = await AuthService(settings).login(result.admin.email, result.admin_password)

        assert signed_in.principal.organization_id == result.organization.id
        assert signed_in.principal.role is Role.ADMIN
        assert signed_in.organization_name == result.organization.name

    async def test_a_duplicate_slug_is_rejected(self, platform_service, provisioned) -> None:
        principal, result = provisioned
        with pytest.raises(ConflictError):
            await platform_service.create_organization(
                principal, name="Other", slug=result.organization.slug
            )

    async def test_users_are_created_inside_the_named_organization(
        self, platform_service, provisioned
    ) -> None:
        principal, result = provisioned

        customer = await platform_service.create_user(
            principal,
            organization_id=result.organization.id,
            email=f"customer-{uuid.uuid4().hex[:6]}@acme.test",
            password="customer-password-123",
            role=Role.USER,
        )
        assert customer.organization_id == result.organization.id
        assert customer.role is Role.USER

    async def test_a_user_can_be_pinned_to_a_location(self, platform_service, provisioned) -> None:
        principal, result = provisioned

        location = await platform_service.create_location(
            principal, organization_id=result.organization.id, name="Bandra Branch"
        )
        user = await platform_service.create_user(
            principal,
            organization_id=result.organization.id,
            email=f"pinned-{uuid.uuid4().hex[:6]}@acme.test",
            password="pinned-password-123",
            role=Role.USER,
            location_id=location.id,
        )
        assert user.location_id == location.id

    async def test_an_email_already_in_use_anywhere_is_rejected(
        self, platform_service, provisioned
    ) -> None:
        """The login directory is global, so addresses are unique deployment-wide.

        Catching it here gives a clear conflict rather than a constraint error
        surfacing from deep inside the tenant-scoped insert.
        """
        principal, result = provisioned
        with pytest.raises(ConflictError):
            await platform_service.create_user(
                principal,
                organization_id=result.organization.id,
                email=result.admin.email,
                password="another-password-123",
                role=Role.USER,
            )

    async def test_an_unknown_organization_is_rejected(self, platform_service, provisioned) -> None:
        principal, _ = provisioned
        with pytest.raises(NotFoundError):
            await platform_service.create_user(
                principal,
                organization_id=uuid.uuid4(),
                email="ghost@nowhere.test",
                password="ghost-password-123",
                role=Role.USER,
            )


class TestIsolationStillHolds:
    """Provisioning power is not read power."""

    async def test_an_operator_sees_every_organization(
        self, platform_service, provisioned, seeded_org
    ) -> None:
        _, result = provisioned
        slugs = {o.slug for o in await platform_service.list_organizations()}

        assert result.organization.slug in slugs
        assert seeded_org["slug"] in slugs

    async def test_listing_users_is_scoped_to_the_named_organization(
        self, platform_service, provisioned, seeded_org
    ) -> None:
        """Even holding the platform flag, a session sees one tenant at a time."""
        _, result = provisioned

        theirs = await platform_service.list_users(result.organization.id)
        assert {u.organization_id for u in theirs} == {result.organization.id}

        others = await platform_service.list_users(seeded_org["organization_id"])
        assert {u.organization_id for u in others} == {seeded_org["organization_id"]}

    async def test_a_provisioned_tenant_is_isolated_once_it_holds_knowledge(
        self, platform_service, provisioned, seeded_org, settings, providers, db_engine
    ) -> None:
        """Provision a tenant, give it a document, and check both directions.

        Everything before this proves an operator can *create* a tenant. This
        proves the tenant they created is a real one -- its knowledge is
        retrievable by its own users and invisible to everyone else. Model calls
        are faked, so the assertion does not depend on a reachable model server.
        """
        from app.core.db import UnitOfWork
        from app.core.tenancy import TenantContext, system_principal
        from app.core.tracing import TraceContext
        from app.services.documents.service import DocumentService
        from app.services.ingestion.pipeline import IngestionPipeline
        from app.services.retrieval.service import Retriever

        _, result = provisioned
        tenant = TenantContext(organization_id=result.organization.id)
        maker = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)
        uow = UnitOfWork(tenant, settings, sessionmaker=maker)

        secret = (
            "# Aurora Clinic Handbook\n\n"
            "## Consultation Hours\n\n"
            "Consultations run from 9:00 AM to 5:00 PM. The zolpidem protocol "
            "requires a signed consent form before dispensing.\n"
        )
        upload = await DocumentService(settings, providers).upload(
            uow,
            system_principal(result.organization.id, result.admin.id),
            data=secret.encode("utf-8"),
            filename="handbook.md",
            content_type="text/markdown",
            title="Clinic Handbook",
            location_id=None,
            document_type="policy",
            trace=TraceContext.new(organization_id=result.organization.id),
            enqueue=False,
        )
        await IngestionPipeline(settings=settings, providers=providers).run(
            uow, upload.job_id, tenant=tenant, trace=TraceContext.new(), worker_id="test"
        )

        retriever = Retriever(
            settings=settings,
            search=providers.require_search(),
            embeddings=providers.require_embeddings(),
        )
        query = "zolpidem consultation hours consent form"

        mine = await retriever.retrieve(uow, tenant, queries=[query], top_k=20)
        assert mine.hits, "the provisioned tenant cannot retrieve its own document"

        stranger = TenantContext(organization_id=seeded_org["organization_id"])
        theirs = await retriever.retrieve(
            UnitOfWork(stranger, settings, sessionmaker=maker),
            stranger,
            queries=[query],
            top_k=20,
        )
        assert not [h for h in theirs.hits if "zolpidem" in h.content.lower()]

    async def test_a_platform_session_reads_no_tenant_rows_without_naming_one(
        self, settings, db_engine, seeded_org
    ) -> None:
        """The boundary, asserted at the database.

        `app.platform_admin` widens `organizations` and nothing else. Without an
        organization named, every tenant table stays empty -- which is what stops
        a platform credential becoming a cross-tenant read.
        """
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from app.core.db import platform_session

        maker = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)
        async with platform_session(None, settings, sessionmaker=maker) as session:
            organizations = (
                await session.execute(text("SELECT count(*) FROM organizations"))
            ).scalar_one()
            users = (await session.execute(text("SELECT count(*) FROM users"))).scalar_one()
            documents = (await session.execute(text("SELECT count(*) FROM documents"))).scalar_one()
            chunks = (await session.execute(text("SELECT count(*) FROM chunks"))).scalar_one()

        assert organizations >= 1, "an operator must be able to enumerate tenants"
        assert users == 0
        assert documents == 0
        assert chunks == 0


class TestUserLifecycle:
    async def test_disabling_a_user_revokes_their_tokens(
        self, platform_service, provisioned, settings
    ) -> None:
        """Revocation must take effect now, not at the token's natural expiry."""
        principal, result = provisioned
        auth = AuthService(settings)

        signed_in = await auth.login(result.admin.email, result.admin_password)
        await platform_service.set_user_active(
            principal,
            organization_id=result.organization.id,
            user_id=result.admin.id,
            active=False,
        )

        with pytest.raises(AuthenticationError):
            await auth.refresh(signed_in.tokens.refresh_token)
        with pytest.raises(AuthenticationError):
            await auth.login(result.admin.email, result.admin_password)

    async def test_re_enabling_restores_access(
        self, platform_service, provisioned, settings
    ) -> None:
        principal, result = provisioned
        for active in (False, True):
            await platform_service.set_user_active(
                principal,
                organization_id=result.organization.id,
                user_id=result.admin.id,
                active=active,
            )

        signed_in = await AuthService(settings).login(result.admin.email, result.admin_password)
        assert signed_in.principal.organization_id == result.organization.id
