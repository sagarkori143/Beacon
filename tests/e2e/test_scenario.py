"""The Sagar Hotels scenario, end to end.

Upload -> process -> activate -> ask -> retrieve the right context -> answer,
through the real pipeline, the real schema and the real agent loop. Only the
model itself is faked, and it is scripted rather than random so the assertions
are about *what the agent gave the model*, not about what a model chose to say.

The scenario is the one from the specification: an organization whose breakfast
policy is stored once, and a location that overrides it.
"""

from __future__ import annotations

import pytest

from app.core.enums import JobStatus, Role, VersionStatus
from app.core.tenancy import Principal, scopes_for
from app.core.tracing import TraceContext
from app.providers.llm.fake import ScriptedResponse
from app.repositories import document as document_repo
from app.repositories import ingestion as job_repo
from app.services.agent.router import ModelRouter
from app.services.agent.runtime import AgentRequest, AgentRuntime
from app.tools.registry import build_default_registry

pytestmark = [pytest.mark.e2e]

GROUP_HANDBOOK = """# Sagar Hotels Guest Handbook

## Breakfast Service

Breakfast is served in the main dining room from 7:00 AM to 10:00 AM every day,
including weekends and public holidays.

## Cancellation Policy

Reservations may be cancelled free of charge up to 48 hours before arrival.
"""

GINZA_GUIDE = """# Sagar Ginza Property Guide

## Breakfast Hours

At Sagar Ginza breakfast is served from 7:00 AM to 11:00 AM in the Ginza Grill
on the second floor, every day of the week.
"""


@pytest.fixture
def agent(settings, providers, fake_llm, retriever) -> AgentRuntime:
    return AgentRuntime(
        settings=settings,
        providers=providers,
        router=ModelRouter(providers, settings),
        retriever=retriever,
        tools=build_default_registry(),
        redis=None,
    )


def principal_for(seeded_org: dict, location: str | None) -> Principal:

    return Principal(
        user_id=seeded_org["admin_id"],
        organization_id=seeded_org["organization_id"],
        location_id=seeded_org["locations"][location] if location else None,
        role=Role.USER,
        scopes=scopes_for(Role.USER),
    )


def script_answer(fake_llm, answer: str) -> None:
    """Queue a plan (no tools, retrieval on) followed by the answer."""
    fake_llm.script(
        ScriptedResponse(
            structured={
                "intent": "answer_from_knowledge",
                "needs_retrieval": True,
                "search_queries": ["breakfast hours serving time"],
                "candidate_tools": [],
                "reasoning": "policy question",
            }
        ),
        ScriptedResponse(text=answer),
    )


class TestFullScenario:
    async def test_upload_processes_and_activates(self, ingest, org_uow, org_tenant) -> None:
        """The ingestion half: a document becomes searchable knowledge."""
        result = await ingest(GROUP_HANDBOOK, title="Sagar Hotels Guest Handbook")

        async with org_uow.begin() as session:
            job = await job_repo.get_job(session, org_tenant, result.job_id)
            events = await job_repo.list_events(session, org_tenant, result.job_id)
            version = await document_repo.get_version(session, org_tenant, result.version_id)

        assert job.status is JobStatus.COMPLETED
        assert job.progress == 1.0
        assert version.status is VersionStatus.ACTIVE
        assert version.chunk_count > 0

        stages = [e.stage.value for e in events]
        for expected in (
            "PARSING",
            "CHUNKING",
            "EMBEDDING",
            "INDEXING",
            "VALIDATING",
            "ACTIVATING",
            "COMPLETED",
        ):
            assert expected in stages, f"{expected} missing from job history"

    async def test_ocr_is_not_run_on_a_text_document(self, ingest, org_uow, org_tenant) -> None:
        """OCR is the most expensive stage, so it must not fire speculatively.

        The decision and its reasons are recorded either way, so this is
        inspectable rather than a matter of trust.
        """
        result = await ingest(GROUP_HANDBOOK, title="Sagar Hotels Guest Handbook")

        async with org_uow.begin() as session:
            version = await document_repo.get_version(session, org_tenant, result.version_id)
        assert version.ocr_used is False
        assert version.ocr_page_count == 0

    async def test_a_ginza_guest_gets_the_ginza_answer(
        self, ingest, agent, fake_llm, org_uow, seeded_org
    ) -> None:
        """The headline scenario.

        The context handed to the model must contain 11:00 AM and must NOT
        contain 10:00 AM -- because a model shown two contradictory times cannot
        reliably choose between them, whatever the prompt says.
        """
        await ingest(GROUP_HANDBOOK, title="Sagar Hotels Guest Handbook")
        await ingest(GINZA_GUIDE, title="Sagar Ginza Property Guide", location="alpha")

        script_answer(fake_llm, "Breakfast at Sagar Ginza is served from 7 AM to 11 AM [S1].")
        principal = principal_for(seeded_org, "alpha")

        result = await agent.run(
            AgentRequest(
                query="What time is breakfast?",
                principal=principal,
                uow=org_uow.scoped_to(principal.tenant),
                organization_name="Sagar Hotels",
                location_name="Sagar Ginza",
                stream_tokens=False,
            ),
            trace=TraceContext.new(),
        )

        context = " ".join(p.content for p in result.context.passages)
        assert "11:00 AM" in context
        assert "10:00 AM" not in context, "the overridden group hours reached the model"
        assert "11 AM" in result.answer
        assert result.citations

    async def test_another_location_gets_the_group_answer(
        self, ingest, agent, fake_llm, org_uow, seeded_org
    ) -> None:
        """The same stored document, a different correct answer.

        Beta has no breakfast document of its own, so it inherits the group's --
        and must see nothing of Ginza's.
        """
        await ingest(GROUP_HANDBOOK, title="Sagar Hotels Guest Handbook")
        await ingest(GINZA_GUIDE, title="Sagar Ginza Property Guide", location="alpha")

        script_answer(fake_llm, "Breakfast is served from 7 AM to 10 AM [S1].")
        principal = principal_for(seeded_org, "beta")

        result = await agent.run(
            AgentRequest(
                query="What time is breakfast?",
                principal=principal,
                uow=org_uow.scoped_to(principal.tenant),
                organization_name="Sagar Hotels",
                location_name="Sagar Beta",
                stream_tokens=False,
            ),
            trace=TraceContext.new(),
        )

        context = " ".join(p.content for p in result.context.passages)
        assert "10:00 AM" in context
        assert "Ginza" not in context, "another location's knowledge leaked"

    async def test_the_trace_explains_the_answer(
        self, ingest, agent, fake_llm, org_uow, seeded_org
    ) -> None:
        """Every answer must be explainable after the fact."""
        await ingest(GROUP_HANDBOOK, title="Sagar Hotels Guest Handbook")
        script_answer(fake_llm, "Cancellation is free up to 48 hours before arrival [S1].")
        principal = principal_for(seeded_org, None)

        result = await agent.run(
            AgentRequest(
                query="What is the cancellation policy?",
                principal=principal,
                uow=org_uow,
                stream_tokens=False,
            ),
            trace=TraceContext.new(),
        )

        assert result.trace["routing"]["provider"] == "fake"
        assert result.trace["plan"]["needs_retrieval"] is True
        assert result.trace["context"]["chunk_ids"]
        assert result.grounding > 0


class TestStreaming:
    async def test_the_stream_reports_progress_then_tokens(
        self, ingest, agent, fake_llm, org_uow, seeded_org
    ) -> None:
        """Semantic events first, then the answer.

        A client should be able to show "searching", "found 3 sources" and the
        override notice before the first token arrives.
        """
        await ingest(GROUP_HANDBOOK, title="Sagar Hotels Guest Handbook")
        await ingest(GINZA_GUIDE, title="Sagar Ginza Property Guide", location="alpha")

        script_answer(fake_llm, "Breakfast runs until 11 AM at Ginza [S1].")
        principal = principal_for(seeded_org, "alpha")

        events = [
            event
            async for event in agent.run_stream(
                AgentRequest(
                    query="What time is breakfast?",
                    principal=principal,
                    uow=org_uow.scoped_to(principal.tenant),
                    location_name="Sagar Ginza",
                    stream_tokens=True,
                ),
                trace=TraceContext.new(),
            )
        ]

        types = [e.type for e in events]
        assert "stage" in types
        assert "search" in types
        assert "citation" in types
        assert "token" in types
        assert types[-1] == "done"

        # The override is surfaced to the client, not just applied silently.
        conflicts = [e for e in events if e.type == "conflict"]
        assert conflicts and conflicts[0].data["suppressed"] >= 1

        # Progress genuinely precedes generation.
        assert types.index("search") < types.index("token")

    async def test_sse_frames_are_well_formed(
        self, ingest, agent, fake_llm, org_uow, seeded_org
    ) -> None:
        await ingest(GROUP_HANDBOOK, title="Sagar Hotels Guest Handbook")
        script_answer(fake_llm, "Answer [S1].")
        principal = principal_for(seeded_org, None)

        async for event in agent.run_stream(
            AgentRequest(
                query="What is the cancellation policy?",
                principal=principal,
                uow=org_uow,
                stream_tokens=True,
            ),
            trace=TraceContext.new(),
        ):
            frame = event.to_sse()
            assert frame.startswith("event: ")
            assert "\ndata: " in frame
            assert frame.endswith("\n\n")


class TestNoKnowledge:
    async def test_an_unanswerable_question_gets_no_context(
        self, ingest, agent, fake_llm, org_uow, seeded_org
    ) -> None:
        """With nothing indexed, the model must be told so explicitly.

        The prompt instructs it to say it does not know; the important part
        here is that no context is fabricated to fill the gap.
        """
        script_answer(fake_llm, "I do not have that information.")
        principal = principal_for(seeded_org, None)

        result = await agent.run(
            AgentRequest(
                query="What is the helicopter landing procedure?",
                principal=principal,
                uow=org_uow,
                stream_tokens=False,
            ),
            trace=TraceContext.new(),
        )

        assert result.context.is_empty
        assert result.citations == []
        assert result.grounding == 0.0
