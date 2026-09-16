"""Watching a document being processed, live.

The worker and the API are separate processes, so the interesting properties are
about the gap between them: that progress crosses it, that a run behaves
identically when nobody is watching, and that a broken feed costs a frame rather
than a document.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

from app.core.config import QueueSettings, Settings
from app.core.enums import IngestionStage, VersionStatus
from app.core.tracing import TraceContext
from app.providers.progress.base import ProgressEvent, ProgressPublisher
from app.providers.progress.memory import MemoryProgress
from app.services.documents.service import DocumentService
from app.services.ingestion.pipeline import IngestionPipeline

pytestmark = [pytest.mark.integration]


class BrokenProgress(ProgressPublisher):
    """A feed that fails on every publish."""

    def __init__(self) -> None:
        self.attempts = 0

    async def publish(self, event: ProgressEvent) -> None:
        self.attempts += 1
        raise RuntimeError("the feed is down")

    async def follow(self, job_id, *, after=None):  # pragma: no cover - unused
        yield "", {}

    async def cursor(self, job_id):  # pragma: no cover - unused
        return "0"


@pytest.fixture
def progress() -> MemoryProgress:
    return MemoryProgress(Settings(queue=QueueSettings(provider="memory")))


@pytest.fixture
def run_ingestion(settings, providers, org_uow, org_tenant, seeded_org):
    """Upload a document and run the pipeline with a chosen publisher."""
    from app.core.tenancy import system_principal

    service = DocumentService(settings, providers)
    admin = system_principal(seeded_org["organization_id"], seeded_org["admin_id"])

    async def _run(publisher, *, title: str = "Handbook"):
        result = await service.upload(
            org_uow,
            admin,
            data=b"# Handbook\n\n## Breakfast\n\nBreakfast runs 7:00 to 10:00.\n",
            filename=f"{title}.md",
            content_type="text/markdown",
            title=title,
            location_id=None,
            document_type="policy",
            trace=TraceContext.new(organization_id=seeded_org["organization_id"]),
            enqueue=False,
        )
        pipeline = IngestionPipeline(settings=settings, providers=providers, progress=publisher)
        await pipeline.run(
            org_uow,
            result.job_id,
            tenant=org_tenant,
            trace=TraceContext.new(),
            worker_id="test",
        )
        return result

    return _run


class TestPublishing:
    async def test_every_stage_is_announced(self, run_ingestion, progress) -> None:
        result = await run_ingestion(progress)

        stages = [e.stage for e in progress.published]
        assert stages, "nothing was published at all"
        assert all(e.job_id == result.job_id for e in progress.published)

        # The stages a watcher actually cares about, in order.
        for expected in ("PARSING", "CHUNKING", "EMBEDDING", "INDEXING", "ACTIVATING"):
            assert expected in stages, f"{expected} never reached a watcher"
        assert stages[-1] == IngestionStage.COMPLETED.value

    async def test_progress_only_moves_forward(self, run_ingestion, progress) -> None:
        """A bar that jumps backwards reads as a bug even when nothing is wrong."""
        await run_ingestion(progress)
        values = [e.progress for e in progress.published]
        assert values == sorted(values)
        assert values[-1] == pytest.approx(1.0)

    async def test_the_detail_a_timeline_wants_survives(self, run_ingestion, progress) -> None:
        """Per-stage detail is the difference between a spinner and an answer."""
        await run_ingestion(progress)
        by_stage = {e.stage: e for e in progress.published}

        assert by_stage["CHUNKING"].detail.get("chunks")
        assert by_stage["EMBEDDING"].detail.get("model")
        assert by_stage["OCR"].detail, "the OCR decision is the whole point of the OCR row"
        assert all(e.duration_ms is not None for e in progress.published[:-1])


class TestTheFeedIsNotLoadBearing:
    async def test_a_run_with_nobody_watching_still_completes(
        self, run_ingestion, org_uow, org_tenant
    ) -> None:
        from app.repositories import document as document_repo

        result = await run_ingestion(None)

        async with org_uow.begin() as session:
            version = await document_repo.get_version(session, org_tenant, result.version.id)
        assert version.status is VersionStatus.ACTIVE

    async def test_a_broken_feed_does_not_fail_the_document(
        self, run_ingestion, org_uow, org_tenant
    ) -> None:
        """Failing to *show* something is not failing to *do* it.

        A publisher that raises must not turn a document that ingested correctly
        into a failed one -- the durable record is the Postgres event row, and
        this feed is a copy for live viewing.
        """
        from app.repositories import document as document_repo

        broken = BrokenProgress()
        result = await run_ingestion(broken, title="Resilient")

        assert broken.attempts > 0, "the publisher was never even called"
        async with org_uow.begin() as session:
            version = await document_repo.get_version(session, org_tenant, result.version.id)
        assert version.status is VersionStatus.ACTIVE


class TestRedisRoundTrip:
    @pytest.fixture
    async def publisher(self, settings) -> AsyncIterator[object]:
        from redis.asyncio import Redis

        from app.providers.progress.redis_stream import RedisStreamProgress

        client = Redis.from_url(settings.redis_url, decode_responses=True)
        try:
            await client.ping()
        except Exception:  # noqa: BLE001
            pytest.skip("Redis not reachable")
        try:
            yield RedisStreamProgress(settings, redis=client)
        finally:
            await client.aclose()

    async def test_an_event_survives_the_trip_between_processes(self, publisher) -> None:
        """The worker publishes, the API reads. That is the whole point."""
        job_id = uuid.uuid4()
        await publisher.publish(
            ProgressEvent(
                job_id=job_id,
                stage="CHUNKING",
                status="COMPLETED",
                progress=0.55,
                duration_ms=12.5,
                detail={"chunks": 7, "sections": 3},
            )
        )

        received = []
        async for entry_id, payload in publisher.follow(job_id):
            if payload:
                received.append(payload)
                break

        assert received, "nothing came back out of the stream"
        event = received[0]
        assert event["stage"] == "CHUNKING"
        assert event["progress"] == pytest.approx(0.55)
        assert event["duration_ms"] == pytest.approx(12.5)
        # Structured detail must arrive as structure, not as a string.
        assert event["detail"] == {"chunks": 7, "sections": 3}

    async def test_a_cursor_taken_first_skips_what_history_already_covered(self, publisher) -> None:
        """Why the stream endpoint reads the cursor before the database.

        The endpoint replays a job's recorded history from Postgres and then
        follows the live feed. Following from the start of the stream delivers
        the early stages a second time -- the watcher sees PARSING, OCR and
        CLEANING twice. Taking the cursor first, and resuming from it, is what
        makes the two halves meet exactly once.

        Ordering matters both ways: publishing happens after the database
        commit, so anything before the cursor is already in the history, and
        anything after it arrives live rather than being skipped.
        """
        job_id = uuid.uuid4()

        # What the durable history would already contain.
        for stage, value in (("PARSING", 0.2), ("OCR", 0.35)):
            await publisher.publish(
                ProgressEvent(job_id=job_id, stage=stage, status="COMPLETED", progress=value)
            )

        resume_from = await publisher.cursor(job_id)

        # What happens after the watcher connects.
        await publisher.publish(
            ProgressEvent(job_id=job_id, stage="CHUNKING", status="COMPLETED", progress=0.55)
        )

        seen = []
        async for _entry_id, payload in publisher.follow(job_id, after=resume_from):
            if payload:
                seen.append(payload["stage"])
                break

        assert seen == ["CHUNKING"], f"expected only what came after the cursor, got {seen}"

    async def test_a_late_watcher_still_sees_what_it_missed(self, publisher) -> None:
        """Published before anyone was listening, and still delivered.

        This is why the feed is a Stream and not pub/sub: the first stages
        happen in the seconds before a browser finishes opening the page, and
        those are exactly the ones someone is watching for.
        """
        job_id = uuid.uuid4()
        for stage, value in (("PARSING", 0.2), ("CHUNKING", 0.55)):
            await publisher.publish(
                ProgressEvent(job_id=job_id, stage=stage, status="COMPLETED", progress=value)
            )

        seen = []
        async for _entry_id, payload in publisher.follow(job_id):
            if payload:
                seen.append(payload["stage"])
            if len(seen) == 2:
                break

        assert seen == ["PARSING", "CHUNKING"]
