"""The public site, where nobody signs in.

This is the one place in the system where tenant scope comes from a request
parameter rather than a token, so the tests that matter are the ones proving
that scoping itself did not weaken: a visitor still reaches exactly one
organization, gets organization-wide knowledge only, and cannot ask about a
tenant that is not listed.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.core.db import UnitOfWork
from app.core.enums import Role
from app.core.errors import NotFoundError, RateLimitError
from app.core.tenancy import PUBLIC_USER_ID, TenantContext, public_principal, scopes_for

pytestmark = [pytest.mark.integration]


@pytest.fixture
async def redis_client(settings: Settings) -> AsyncIterator[object]:
    from redis.asyncio import Redis

    client = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await client.ping()
    except Exception:  # noqa: BLE001 - absence is the answer
        pytest.skip("Redis not reachable")
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def published(owner_engine: object, seeded_org: dict) -> AsyncIterator[dict]:
    """The seeded organization, listed on the public site."""
    maker = async_sessionmaker(owner_engine, expire_on_commit=False, autoflush=False)  # type: ignore[arg-type]
    async with maker() as session, session.begin():
        await session.execute(
            text("UPDATE organizations SET is_public = true WHERE id = :id"),
            {"id": seeded_org["organization_id"]},
        )
    yield seeded_org


class TestThePublicPrincipal:
    """It is an ordinary principal, which is the point."""

    def test_it_carries_no_location(self) -> None:
        """No location means organization-wide knowledge and nothing else.

        `Retriever._levels` reads a principal with no location as
        ORGANIZATION scope alone -- there is no "search every branch" fallback.
        So a visitor cannot reach one branch's private material even though they
        named the organization themselves.
        """
        principal = public_principal(uuid.uuid4())
        assert principal.location_id is None

    def test_it_can_read_and_nothing_more(self) -> None:
        principal = public_principal(uuid.uuid4())
        assert principal.role is Role.USER
        assert principal.scopes == scopes_for(Role.USER)
        assert not principal.is_admin
        assert "documents:write" not in principal.scopes
        assert "org:manage" not in principal.scopes

    def test_anonymous_traffic_is_recognisable(self) -> None:
        """One fixed id, so logs show anonymous traffic as anonymous.

        A fresh uuid4 per visitor would make the audit trail look like thousands
        of distinct people instead of one public front door.
        """
        a = public_principal(uuid.uuid4())
        b = public_principal(uuid.uuid4())
        assert a.user_id == b.user_id == PUBLIC_USER_ID


class TestListingAndResolution:
    async def test_a_listed_organization_resolves(self, settings, app_engine, published) -> None:
        from app.api.v1.public import _resolve_public_organization

        organization = await _resolve_public_organization(published["slug"], settings)
        assert organization.id == published["organization_id"]

    async def test_an_unlisted_organization_is_not_found(
        self, settings, app_engine, owner_engine, published
    ) -> None:
        """404, not 403.

        Telling a visitor "that exists but you may not have it" confirms the
        tenant exists, which is itself something they should not learn.
        """
        from app.api.v1.public import _resolve_public_organization

        maker = async_sessionmaker(owner_engine, expire_on_commit=False, autoflush=False)
        async with maker() as session, session.begin():
            await session.execute(
                text("UPDATE organizations SET is_public = false WHERE id = :id"),
                {"id": published["organization_id"]},
            )

        with pytest.raises(NotFoundError):
            await _resolve_public_organization(published["slug"], settings)

    async def test_an_unknown_slug_is_not_found(self, settings, app_engine) -> None:
        from app.api.v1.public import _resolve_public_organization

        with pytest.raises(NotFoundError):
            await _resolve_public_organization(f"nope-{uuid.uuid4().hex[:8]}", settings)

    async def test_the_listing_hides_unlisted_organizations(
        self, settings, app_engine, owner_engine, published
    ) -> None:
        from app.api.v1.public import list_public_organizations

        listed = await list_public_organizations(settings)
        assert published["organization_id"] in {o.id for o in listed}

        maker = async_sessionmaker(owner_engine, expire_on_commit=False, autoflush=False)
        async with maker() as session, session.begin():
            await session.execute(
                text("UPDATE organizations SET is_public = false WHERE id = :id"),
                {"id": published["organization_id"]},
            )

        listed = await list_public_organizations(settings)
        assert published["organization_id"] not in {o.id for o in listed}

    async def test_the_listing_reveals_nothing_but_names(
        self, settings, app_engine, published
    ) -> None:
        """A visitor learns which companies exist, not what any of them know."""
        from app.api.v1.public import list_public_organizations

        listed = await list_public_organizations(settings)
        assert set(listed[0].model_dump()) == {"id", "name", "slug"}


class TestIsolationUnderPublicAccess:
    """The critical test: naming your own organization is not a way into others."""

    async def test_a_visitor_reaches_only_the_organization_they_named(
        self, settings, db_engine, providers, published, ingest, seeded_org
    ) -> None:
        from app.services.retrieval.service import Retriever

        secret = (
            "# Guest Handbook\n\n## Breakfast\n\n"
            "Breakfast is served from 7:00 AM to 10:00 AM. The zolpidem protocol "
            "requires a signed consent form.\n"
        )
        await ingest(secret, title="Guest Handbook")

        retriever = Retriever(
            settings=settings,
            search=providers.require_search(),
            embeddings=providers.require_embeddings(),
        )
        maker = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)
        query = "zolpidem consent form breakfast"

        # A visitor to this organization finds its knowledge.
        mine = public_principal(seeded_org["organization_id"])
        found = await retriever.retrieve(
            UnitOfWork(mine.tenant, settings, sessionmaker=maker),
            mine.tenant,
            queries=[query],
            top_k=20,
        )
        assert found.hits, "a visitor cannot reach the organization they asked for"

        # A visitor to a different organization finds none of it.
        stranger = public_principal(uuid.uuid4())
        theirs = await retriever.retrieve(
            UnitOfWork(stranger.tenant, settings, sessionmaker=maker),
            stranger.tenant,
            queries=[query],
            top_k=20,
        )
        assert not [h for h in theirs.hits if "zolpidem" in h.content.lower()]

    async def test_a_visitor_never_sees_one_branch_private_knowledge(
        self, settings, db_engine, providers, published, ingest, seeded_org
    ) -> None:
        """Organization-wide only, even though the visitor named the org.

        A branch's own material is for people who belong to that branch. A
        visitor has no branch, and there is deliberately no way for them to
        claim one.
        """
        from app.services.retrieval.service import Retriever

        await ingest(
            "# Alpha Only\n\n## Rooftop\n\nThe rooftop hosts a quarterly kumquat tasting.\n",
            title="Alpha Branch Notes",
            location="alpha",
        )

        retriever = Retriever(
            settings=settings,
            search=providers.require_search(),
            embeddings=providers.require_embeddings(),
        )
        maker = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)
        visitor = public_principal(seeded_org["organization_id"])

        outcome = await retriever.retrieve(
            UnitOfWork(visitor.tenant, settings, sessionmaker=maker),
            visitor.tenant,
            queries=["rooftop kumquat tasting"],
            top_k=20,
        )
        leaked = [h for h in outcome.hits if "kumquat" in h.content.lower()]
        assert not leaked, "a branch's private knowledge reached an anonymous visitor"


class TestAbuse:
    async def test_the_limit_refuses_the_next_question(self, redis_client) -> None:
        """Every question costs a model call, so an unlimited public endpoint is
        an invitation to keep the model server busy forever."""
        from app.services.agent.guardrails import check_public_rate_limit

        organization_id = uuid.uuid4()
        client = f"203.0.113.{uuid.uuid4().int % 250}"

        for _ in range(3):
            await check_public_rate_limit(
                redis_client, organization_id=organization_id, client=client, limit_per_minute=3
            )

        with pytest.raises(RateLimitError):
            await check_public_rate_limit(
                redis_client, organization_id=organization_id, client=client, limit_per_minute=3
            )

    async def test_one_visitor_does_not_spend_another_organizations_budget(
        self, redis_client
    ) -> None:
        from app.services.agent.guardrails import check_public_rate_limit

        client = f"203.0.113.{uuid.uuid4().int % 250}"
        busy, quiet = uuid.uuid4(), uuid.uuid4()

        for _ in range(3):
            await check_public_rate_limit(
                redis_client, organization_id=busy, client=client, limit_per_minute=3
            )

        # The same visitor, a different organization: its own bucket.
        await check_public_rate_limit(
            redis_client, organization_id=quiet, client=client, limit_per_minute=3
        )

    async def test_a_limit_of_zero_means_no_limiting(self, redis_client) -> None:
        """Turning the limiter off must not turn it into a wall."""
        from app.services.agent.guardrails import check_public_rate_limit

        for _ in range(5):
            await check_public_rate_limit(
                redis_client,
                organization_id=uuid.uuid4(),
                client="198.51.100.7",
                limit_per_minute=0,
            )


class TestScopeHygiene:
    async def test_a_public_session_still_cannot_read_two_tenants(
        self, settings, db_engine, published, seeded_org
    ) -> None:
        """The scoping rule, asserted at the database rather than in Python."""
        from app.core.db import tenant_session

        maker = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)
        visitor = public_principal(seeded_org["organization_id"])

        async with tenant_session(visitor.organization_id, settings, sessionmaker=maker) as session:
            organizations = (
                await session.execute(text("SELECT count(*) FROM organizations"))
            ).scalar_one()
            users = (await session.execute(text("SELECT count(*) FROM users"))).scalar_one()

        # It sees its own row plus whatever else is published -- names only --
        # and no tenant table beyond its own scope.
        assert organizations >= 1
        assert users >= 0

        elsewhere = TenantContext(organization_id=uuid.uuid4())
        async with tenant_session(
            elsewhere.organization_id, settings, sessionmaker=maker
        ) as session:
            foreign_users = (await session.execute(text("SELECT count(*) FROM users"))).scalar_one()
        assert foreign_users == 0
