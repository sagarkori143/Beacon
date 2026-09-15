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


class TestRedisStreams:
    """The real backend. Skipped when Redis is not running."""

    @pytest.fixture
    async def redis_queue(self):  # type: ignore[no-untyped-def]
        import uuid as _uuid

        from redis.asyncio import Redis

        from app.providers.queue.redis_streams import RedisStreamsQueue
        from tests.conftest import TEST_REDIS_URL

        client = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
        try:
            await client.ping()
        except Exception:  # noqa: BLE001 - absence is the answer
            await client.aclose()
            pytest.skip("Redis not reachable. Start it with: docker compose up -d redis")

        suffix = _uuid.uuid4().hex[:8]
        settings = QueueSettings(
            provider="redis_streams",
            stream=f"test-ingestion-{suffix}",
            group="test-workers",
            dead_letter_stream=f"test-dlq-{suffix}",
            block_ms=300,
        )
        queue = RedisStreamsQueue(settings, redis=client)
        await queue.setup()
        try:
            yield queue
        finally:
            await client.delete(settings.stream, settings.dead_letter_stream)
            await client.aclose()

    async def test_an_idle_queue_returns_empty_rather_than_raising(self, redis_queue) -> None:
        """The bug that crash-looped the worker on every quiet period.

        redis-py 8.x applies the block duration as a read deadline and raises
        TimeoutError when a blocking XREADGROUP finds nothing -- which, for a
        queue that is idle most of the time, is the normal case.
        """
        assert await redis_queue.consume(consumer="w1", count=1, block_ms=200) == []

    async def test_a_full_round_trip(self, redis_queue) -> None:
        sent = message()
        await redis_queue.enqueue(sent)

        [delivered] = await redis_queue.consume(consumer="w1", count=1, block_ms=500)
        assert delivered.message.job_id == sent.job_id
        assert delivered.message.organization_id == sent.organization_id

        await redis_queue.ack(delivered)
        assert (await redis_queue.stats())["pending"] == 0

    async def test_abandoned_work_is_reclaimed(self, redis_queue) -> None:
        """A worker died holding this message; another must pick it up."""
        await redis_queue.enqueue(message())
        [taken] = await redis_queue.consume(consumer="worker-1", count=1, block_ms=500)

        reclaimed = await redis_queue.claim_stale(consumer="worker-2", min_idle_ms=0)
        assert [d.message.job_id for d in reclaimed] == [taken.message.job_id]
        assert await redis_queue.delivery_count(taken.id) >= 2

    async def test_dead_lettering_moves_the_message(self, redis_queue) -> None:
        await redis_queue.enqueue(message())
        [delivered] = await redis_queue.consume(consumer="w1", count=1, block_ms=500)
        await redis_queue.dead_letter(delivered, "unparseable")

        stats = await redis_queue.stats()
        assert stats["pending"] == 0
        assert stats["dead_letter_length"] == 1

    async def test_stats_expose_queue_depth_and_staleness(self, redis_queue) -> None:
        """The two numbers worth alerting on."""
        await redis_queue.enqueue(message())
        stats = await redis_queue.stats()
        assert stats["length"] == 1
        assert "oldest_pending_idle_ms" in stats
