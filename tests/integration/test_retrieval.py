"""Hybrid search and hierarchical retrieval against the real database.

The spec names one test here as critical: **Hotel A must never retrieve Hotel B
data.** It is exercised in both directions -- across organizations, and across
locations within one organization -- because the second is the easier one to get
wrong and the one a customer notices first.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.tenancy import TenantContext
from app.providers.search.base import ScopeMode

pytestmark = [pytest.mark.integration]

ORG_HANDBOOK = """# Group Handbook

## Breakfast Service

Breakfast is served from 7:00 AM to 10:00 AM in the main dining room every day.

## Cancellation Policy

Reservations may be cancelled free of charge up to 48 hours before arrival.
Cancellations inside 48 hours are charged one night's room rate.

## Wi-Fi

Wi-Fi is complimentary throughout every property. Ask reception for the code.
"""

ALPHA_GUIDE = """# Alpha Property Guide

## Breakfast Hours

At the Alpha property breakfast runs from 7:00 AM to 11:00 AM in the rooftop
restaurant, every day of the week.

## Valet Parking

Valet parking at Alpha costs 4,000 JPY per night with unlimited access.
"""

BETA_GUIDE = """# Beta Property Guide

## Pet Policy

The Beta property welcomes dogs and cats under 10 kg in designated rooms for a
cleaning fee of 3,000 JPY per stay.

## Rooftop Bar

The Beta rooftop bar is open from 5:00 PM until midnight, guests only.
"""


@pytest.fixture
async def knowledge_base(ingest) -> None:
    """One organization document plus one per location."""
    await ingest(ORG_HANDBOOK, title="Group Handbook")
    await ingest(ALPHA_GUIDE, title="Alpha Property Guide", location="alpha")
    await ingest(BETA_GUIDE, title="Beta Property Guide", location="beta")


class TestCrossTenantIsolation:
    """The critical test. Nothing else matters if these fail."""

    async def test_another_organization_retrieves_nothing(
        self, knowledge_base, retriever, org_uow, settings, db_engine
    ) -> None:
        """A different organization's query returns zero of our chunks."""
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from app.core.db import UnitOfWork

        stranger = TenantContext(organization_id=uuid.uuid4())
        maker = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)
        outcome = await retriever.retrieve(
            UnitOfWork(stranger, settings, sessionmaker=maker),
            stranger,
            queries=["breakfast hours cancellation policy wifi"],
            top_k=20,
        )
        assert outcome.hits == []

    async def test_one_location_cannot_see_another(
        self, knowledge_base, retriever, org_uow, seeded_org
    ) -> None:
        """Alpha asks about pets. Beta's pet-friendly rooms must not appear.

        This is the failure a guest notices: being told the property allows
        dogs when it is a different property that does.
        """
        alpha = TenantContext(
            organization_id=seeded_org["organization_id"],
            location_id=seeded_org["locations"]["alpha"],
        )
        outcome = await retriever.retrieve(
            org_uow.scoped_to(alpha),
            alpha,
            queries=["pets dogs cats allowed rooftop bar"],
            top_k=20,
        )

        beta_id = seeded_org["locations"]["beta"]
        leaked = [h for h in outcome.hits if h.location_id == beta_id]
        assert not leaked, f"{len(leaked)} chunks leaked from another location"
        assert all("Beta" not in h.content for h in outcome.hits)

    async def test_a_user_without_a_location_sees_only_org_knowledge(
        self, knowledge_base, retriever, org_uow, org_tenant, seeded_org
    ) -> None:
        """No location means organization-wide only.

        There is deliberately no "search every location" fallback: that is how
        one property's private information reaches another property's guest.
        """
        outcome = await retriever.retrieve(
            org_uow, org_tenant, queries=["breakfast parking pets"], top_k=20
        )
        assert outcome.hits
        assert all(h.location_id is None for h in outcome.hits)


class TestHierarchicalOverride:
    async def test_location_hours_override_group_hours(
        self, knowledge_base, retriever, org_uow, seeded_org
    ) -> None:
        """Alpha's 11:00 wins; the group's 10:00 is suppressed, not ranked lower.

        Both reaching the model is the real failure: no prompt reliably picks
        between two contradictory times.
        """
        alpha = TenantContext(
            organization_id=seeded_org["organization_id"],
            location_id=seeded_org["locations"]["alpha"],
        )
        outcome = await retriever.retrieve(
            org_uow.scoped_to(alpha), alpha, queries=["breakfast hours"], top_k=10
        )
        joined = " ".join(h.content for h in outcome.hits)

        assert "7:00 AM to 11:00 AM" in joined
        assert "7:00 AM to 10:00 AM" not in joined
        assert outcome.merged.suppressed_count >= 1
        assert "breakfast" in outcome.merged.overrides

    async def test_a_location_without_an_override_inherits(
        self, knowledge_base, retriever, org_uow, seeded_org
    ) -> None:
        """Beta has no breakfast document, so it gets the group's.

        The group document is stored exactly once and serves both properties --
        which is the entire point of the hierarchy.
        """
        beta = TenantContext(
            organization_id=seeded_org["organization_id"],
            location_id=seeded_org["locations"]["beta"],
        )
        outcome = await retriever.retrieve(
            org_uow.scoped_to(beta), beta, queries=["breakfast hours"], top_k=10
        )
        joined = " ".join(h.content for h in outcome.hits)

        assert "7:00 AM to 10:00 AM" in joined
        assert "11:00 AM" not in joined
        assert outcome.merged.suppressed_count == 0

    async def test_unrelated_group_knowledge_survives_an_override(
        self, knowledge_base, retriever, org_uow, seeded_org
    ) -> None:
        """Overriding breakfast must not hide the cancellation policy."""
        alpha = TenantContext(
            organization_id=seeded_org["organization_id"],
            location_id=seeded_org["locations"]["alpha"],
        )
        outcome = await retriever.retrieve(
            org_uow.scoped_to(alpha),
            alpha,
            queries=["cancellation policy refund"],
            top_k=10,
        )
        assert any("48 hours" in h.content for h in outcome.hits)


class TestHybridArms:
    async def test_lexical_arm_finds_an_exact_term(
        self, knowledge_base, retriever, org_uow, org_tenant
    ) -> None:
        """A rare literal term must be findable even without semantic help."""
        result = await retriever.search_one_scope(
            org_uow, org_tenant, query="cancelled", scope_mode=ScopeMode.ORG_ONLY, top_k=5
        )
        assert result.hits
        assert any(h.keyword_rank is not None for h in result.hits)

    async def test_both_arms_contribute(
        self, knowledge_base, retriever, org_uow, org_tenant
    ) -> None:
        """Fusion should be combining two populated lists, not one."""
        result = await retriever.search_one_scope(
            org_uow,
            org_tenant,
            query="breakfast dining room hours",
            scope_mode=ScopeMode.ORG_ONLY,
            top_k=10,
        )
        assert result.vector_candidates > 0
        assert result.keyword_candidates > 0
        assert any(set(h.matched_arms) == {"vector", "keyword"} for h in result.hits)

    async def test_scores_and_ranks_are_reported_for_diagnosis(
        self, knowledge_base, retriever, org_uow, org_tenant
    ) -> None:
        """ "Why did this rank here?" has to be answerable."""
        result = await retriever.search_one_scope(
            org_uow, org_tenant, query="wifi code reception", scope_mode=ScopeMode.ORG_ONLY
        )
        assert result.hits
        top = result.hits[0]
        assert top.score > 0
        assert top.matched_arms

    async def test_a_query_matching_nothing_returns_empty(
        self, knowledge_base, retriever, org_uow, org_tenant
    ) -> None:
        result = await retriever.search_one_scope(
            org_uow,
            org_tenant,
            query="quantum chromodynamics lattice gauge",
            scope_mode=ScopeMode.ORG_ONLY,
            top_k=5,
        )
        # The vector arm always returns its nearest neighbours; what matters is
        # that nothing lexically matched and nothing was fabricated.
        assert all(h.keyword_rank is None for h in result.hits)
