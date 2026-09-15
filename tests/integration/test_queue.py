"""Queue semantics and worker crash recovery.

Delivery is at-least-once with explicit acknowledgement, because the alternative
-- popping a job and losing it when the worker dies -- is not acceptable for a
document someone just uploaded.

These run against the in-process queue, which implements the same contract. The
Redis Streams implementation is covered by ``-m modelserver``-free integration
runs when Redis is available.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.config import QueueSettings
from app.providers.queue.base import QueueMessage
from app.providers.queue.memory import MemoryQueue

pytestmark = [pytest.mark.integration]


def message(**overrides) -> QueueMessage:  # type: ignore[no-untyped-def]
    return QueueMessage(
        job_id=overrides.get("job_id", uuid.uuid4()),
        organization_id=overrides.get("organization_id", uuid.uuid4()),
        document_id=uuid.uuid4(),
        document_version_id=uuid.uuid4(),
        trace_id="trace",
    )


@pytest.fixture
async def queue() -> MemoryQueue:
    q = MemoryQueue(QueueSettings(provider="memory", visibility_timeout_ms=50))
    await q.setup()
    return q


class TestDeliveryContract:
    async def test_a_message_survives_until_acknowledged(self, queue: MemoryQueue) -> None:
        await queue.enqueue(message())
        [delivered] = await queue.consume(consumer="w1", count=1)

        assert (await queue.stats())["pending"] == 1
        await queue.ack(delivered)
        assert (await queue.stats())["pending"] == 0

    async def test_the_tenant_travels_with_the_message(self, queue: MemoryQueue) -> None:
        """A worker must be able to open a correctly-scoped session before it
        reads anything -- there is no unscoped lookup to discover the tenant."""
        org = uuid.uuid4()
        await queue.enqueue(message(organization_id=org))
        [delivered] = await queue.consume(consumer="w1")
        assert delivered.message.organization_id == org

    async def test_round_trip_through_the_wire_format(self) -> None:
        original = message()
        assert QueueMessage.from_fields(original.to_fields()) == original


class TestCrashRecovery:
    async def test_work_abandoned_by_a_dead_worker_is_reclaimed(self, queue: MemoryQueue) -> None:
        """The scenario that justifies the whole design.

        A worker takes a job, gets as far as EMBEDDING, and its container is
        killed. The message must become available to another worker rather than
        being lost with the process.
        """
        await queue.enqueue(message())
        [taken] = await queue.consume(consumer="worker-1")
        assert taken.delivery_count == 1

        queue.simulate_crash()

        reclaimed = await queue.claim_stale(consumer="worker-2", min_idle_ms=0)
        assert len(reclaimed) == 1
        assert reclaimed[0].message.job_id == taken.message.job_id
        assert reclaimed[0].delivery_count == 2
        assert reclaimed[0].is_redelivery

    async def test_healthy_in_flight_work_is_not_stolen(self, queue: MemoryQueue) -> None:
        """Reclaiming too eagerly means two workers processing one document."""
        await queue.enqueue(message())
        await queue.consume(consumer="worker-1")

        stolen = await queue.claim_stale(consumer="worker-2", min_idle_ms=60_000)
        assert stolen == []

    async def test_nack_returns_a_message_for_retry(self, queue: MemoryQueue) -> None:
        await queue.enqueue(message())
        [delivered] = await queue.consume(consumer="w1")
        await queue.nack(delivered)

        again = await queue.claim_stale(consumer="w2", min_idle_ms=0)
        assert len(again) == 1


class TestDeadLettering:
    async def test_exhausted_messages_leave_the_main_flow(self, queue: MemoryQueue) -> None:
        """A document that cannot be parsed must stop consuming attempts."""
        await queue.enqueue(message())
        [delivered] = await queue.consume(consumer="w1")
        await queue.dead_letter(delivered, "unparseable PDF")

        assert (await queue.stats())["pending"] == 0
        assert len(queue.dead_lettered) == 1
        assert queue.dead_lettered[0][1] == "unparseable PDF"

    async def test_a_dead_lettered_message_is_not_redelivered(self, queue: MemoryQueue) -> None:
        await queue.enqueue(message())
        [delivered] = await queue.consume(consumer="w1")
        await queue.dead_letter(delivered, "terminal failure")

        assert await queue.claim_stale(consumer="w2", min_idle_ms=0) == []
        assert await queue.consume(consumer="w2") == []


class TestWorkerLoop:
    async def test_a_terminal_failure_is_dead_lettered_immediately(
        self, settings, providers
    ) -> None:
        """Retrying a document that cannot be parsed only delays the operator
        finding out, and burns the attempt budget doing it."""
        from app.core.errors import IngestionError
        from app.workers.ingestion_worker import IngestionWorker

        queue = MemoryQueue(QueueSettings(provider="memory"))
        worker = IngestionWorker(
            settings=settings,
            providers=providers,
            queue=queue,
            uow_factory=lambda _tenant: None,  # the pipeline never runs
        )

        async def explode(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise IngestionError("corrupt PDF", stage="PARSING", retryable=False)

        worker.pipeline.run = explode  # type: ignore[method-assign]

        await queue.enqueue(message())
        await worker.run(max_messages=1)

        assert worker.stats.dead_lettered == 1
        assert worker.stats.processed == 0

    async def test_a_transient_failure_is_retried(self, settings, providers) -> None:
        """An unreachable model server should delay ingestion, not reject it."""
        from app.core.errors import IngestionError
        from app.workers.ingestion_worker import IngestionWorker

        queue = MemoryQueue(QueueSettings(provider="memory", max_attempts=3))
        worker = IngestionWorker(
            settings=settings,
            providers=providers,
            queue=queue,
            uow_factory=lambda _tenant: None,
        )

        async def explode(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise IngestionError("model server unreachable", stage="EMBEDDING", retryable=True)

        worker.pipeline.run = explode  # type: ignore[method-assign]

        await queue.enqueue(message())
        await worker.run(max_messages=1)

        assert worker.stats.dead_lettered == 0
        assert (await queue.stats())["pending"] == 1

    async def test_the_worker_survives_a_failing_job(self, settings, providers) -> None:
        """One bad document must not take the worker down with it."""
        from app.workers.ingestion_worker import IngestionWorker

        queue = MemoryQueue(QueueSettings(provider="memory"))
        worker = IngestionWorker(
            settings=settings,
            providers=providers,
            queue=queue,
            uow_factory=lambda _tenant: None,
        )

        async def explode(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("something unexpected")

        worker.pipeline.run = explode  # type: ignore[method-assign]

        await queue.enqueue(message())
        await queue.enqueue(message())
        stats = await worker.run(max_messages=2)

        assert stats.failed == 2
