"""Redis Streams queue with consumer groups.

Chosen over a plain list (``LPUSH``/``BRPOP``) because a list gives no
acknowledgement: the moment a worker pops a job, a crash loses it. Streams with
consumer groups give a pending-entries list, per-message delivery counts, and
``XAUTOCLAIM`` to take work back from a dead consumer -- which is exactly what
"the worker died during EMBEDDING" requires.

Chosen over Celery because the abstraction needed here is small and explicit,
and because a bespoke 200-line implementation of the six operations the
interface declares is easier to reason about during an incident than a
general-purpose task framework.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.core.config import QueueSettings
from app.core.logging import get_logger
from app.providers.base import ProviderHealth
from app.providers.queue.base import (
    DeliveredMessage,
    QueueMessage,
    QueueProvider,
    register_queue_provider,
)

log = get_logger(__name__)


@register_queue_provider("redis_streams")
class RedisStreamsQueue(QueueProvider):
    name = "redis_streams"

    def __init__(self, settings: QueueSettings, *, redis: Redis) -> None:
        self.settings = settings
        self.redis = redis
        self.stream = settings.stream
        self.group = settings.group
        self.dlq = settings.dead_letter_stream

    async def setup(self) -> None:
        """Create the consumer group, creating the stream if needed."""
        try:
            await self.redis.xgroup_create(
                name=self.stream, groupname=self.group, id="0", mkstream=True
            )
            log.info("queue_group_created", stream=self.stream, group=self.group)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def enqueue(self, message: QueueMessage) -> str:
        message_id = await self.redis.xadd(
            name=self.stream,
            fields=message.to_fields(),
            maxlen=self.settings.max_stream_length,
            approximate=True,
        )
        log.info(
            "job_enqueued",
            job_id=str(message.job_id),
            stream=self.stream,
            message_id=message_id,
        )
        return str(message_id)

    async def consume(
        self, *, consumer: str, count: int = 1, block_ms: int = 5000
    ) -> list[DeliveredMessage]:
        """Read new messages, blocking briefly when the stream is empty."""
        try:
            response = await self.redis.xreadgroup(
                groupname=self.group,
                consumername=consumer,
                streams={self.stream: ">"},
                count=count,
                block=block_ms,
            )
        except ResponseError as exc:
            if "NOGROUP" in str(exc):
                await self.setup()
                return []
            raise

        return [
            delivered
            for _stream, entries in (response or [])
            for delivered in self._decode_entries(entries)
        ]

    async def ack(self, delivered: DeliveredMessage) -> None:
        await self.redis.xack(self.stream, self.group, delivered.id)

    async def nack(self, delivered: DeliveredMessage) -> None:
        """Leave the message pending so ``claim_stale`` redelivers it.

        Redis has no explicit negative acknowledgement: not acking *is* the
        nack. This method exists so callers state the intent explicitly, and so
        other backends can implement a real nack.
        """
        log.info(
            "job_nacked",
            job_id=str(delivered.message.job_id),
            delivery_count=delivered.delivery_count,
        )

    async def claim_stale(
        self, *, consumer: str, min_idle_ms: int, count: int = 10
    ) -> list[DeliveredMessage]:
        """Reclaim messages whose consumer has gone away.

        This is what makes a worker crash recoverable: the message stays in the
        group's pending list, and after the visibility timeout any other worker
        can take ownership of it.
        """
        try:
            result = await self.redis.xautoclaim(
                name=self.stream,
                groupname=self.group,
                consumername=consumer,
                min_idle_time=min_idle_ms,
                start_id="0-0",
                count=count,
            )
        except ResponseError as exc:
            if "NOGROUP" in str(exc):
                await self.setup()
                return []
            raise

        # xautoclaim returns (next_cursor, entries) or (cursor, entries, deleted).
        entries = result[1] if len(result) > 1 else []
        claimed = self._decode_entries(entries)
        if claimed:
            log.info("jobs_reclaimed", count=len(claimed), consumer=consumer)
        return claimed

    async def dead_letter(self, delivered: DeliveredMessage, reason: str) -> None:
        """Park the message in a separate stream and stop redelivering it."""
        await self.redis.xadd(
            name=self.dlq,
            fields={
                **delivered.message.to_fields(),
                "reason": reason[:500],
                "delivery_count": str(delivered.delivery_count),
                "failed_at": datetime.now(UTC).isoformat(),
            },
            maxlen=self.settings.max_stream_length,
            approximate=True,
        )
        await self.redis.xack(self.stream, self.group, delivered.id)
        log.error(
            "job_dead_lettered",
            job_id=str(delivered.message.job_id),
            delivery_count=delivered.delivery_count,
            reason=reason[:200],
        )

    async def stats(self) -> dict[str, Any]:
        """Depth and staleness -- the two numbers worth alerting on."""
        try:
            length = await self.redis.xlen(self.stream)
            pending = await self.redis.xpending(self.stream, self.group)
            dlq_length = await self.redis.xlen(self.dlq)
        except ResponseError:
            return {"stream": self.stream, "available": False}

        pending_count = pending.get("pending", 0) if isinstance(pending, dict) else 0
        oldest_idle_ms = 0
        if pending_count:
            detail = await self.redis.xpending_range(
                self.stream, self.group, min="-", max="+", count=1
            )
            if detail:
                oldest_idle_ms = int(detail[0].get("time_since_delivered", 0))

        return {
            "stream": self.stream,
            "length": length,
            "pending": pending_count,
            "oldest_pending_idle_ms": oldest_idle_ms,
            "dead_letter_length": dlq_length,
        }

    async def health(self) -> ProviderHealth:
        started = time.perf_counter()
        try:
            await self.redis.ping()
            stats = await self.stats()
        except Exception as exc:  # noqa: BLE001 - health must never raise
            return ProviderHealth(self.name, ok=False, detail=str(exc)[:200])
        return ProviderHealth(
            name=self.name,
            ok=True,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            extra=stats,
        )

    # -- decoding ------------------------------------------------------------

    def _decode_entries(self, entries: Any) -> list[DeliveredMessage]:
        delivered: list[DeliveredMessage] = []
        for entry in entries or []:
            if entry is None:
                continue
            message_id, fields = entry
            if not fields:
                # A claimed entry whose payload was trimmed away; drop it so the
                # pending list does not grow forever.
                continue
            try:
                message = QueueMessage.from_fields(dict(fields))
            except (KeyError, ValueError) as exc:
                log.warning("queue_message_undecodable", message_id=message_id, error=str(exc))
                continue
            delivered.append(
                DeliveredMessage(
                    id=str(message_id),
                    message=message,
                    delivery_count=1,
                )
            )
        return delivered

    async def delivery_count(self, message_id: str) -> int:
        """Exact delivery count from the pending-entries list.

        ``XREADGROUP`` does not report it, so it is fetched on demand -- only the
        retry path needs it.
        """
        detail = await self.redis.xpending_range(
            self.stream, self.group, min=message_id, max=message_id, count=1
        )
        if not detail:
            return 1
        return int(detail[0].get("times_delivered", 1))
