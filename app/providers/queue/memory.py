"""In-process queue for tests.

Models the parts of the contract that matter for correctness: acknowledgement,
redelivery of unacked messages after a visibility timeout, delivery counting,
and a dead-letter destination. That is enough to test worker-crash recovery
without standing up Redis.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from app.core.config import QueueSettings
from app.providers.base import ProviderHealth
from app.providers.queue.base import (
    DeliveredMessage,
    QueueMessage,
    QueueProvider,
    register_queue_provider,
)


@dataclass(slots=True)
class _Entry:
    id: str
    message: QueueMessage
    delivery_count: int = 0
    #: Monotonic time this entry was last handed to a consumer.
    claimed_at: float | None = None
    consumer: str | None = None


@register_queue_provider("memory")
class MemoryQueue(QueueProvider):
    name = "memory"

    def __init__(self, settings: QueueSettings | None = None, **_: Any) -> None:
        self.settings = settings or QueueSettings(provider="memory")
        self._ready: list[_Entry] = []
        self._pending: dict[str, _Entry] = {}
        self.dead_lettered: list[tuple[QueueMessage, str]] = []
        self._counter = 0
        self._lock = asyncio.Lock()

    async def setup(self) -> None:
        return None

    async def enqueue(self, message: QueueMessage) -> str:
        async with self._lock:
            self._counter += 1
            entry = _Entry(id=f"{self._counter}-0", message=message)
            self._ready.append(entry)
            return entry.id

    async def consume(
        self, *, consumer: str, count: int = 1, block_ms: int = 5000
    ) -> list[DeliveredMessage]:
        async with self._lock:
            taken = self._ready[:count]
            self._ready = self._ready[count:]
            out: list[DeliveredMessage] = []
            for entry in taken:
                entry.delivery_count += 1
                entry.claimed_at = time.monotonic()
                entry.consumer = consumer
                self._pending[entry.id] = entry
                out.append(
                    DeliveredMessage(
                        id=entry.id, message=entry.message, delivery_count=entry.delivery_count
                    )
                )
            return out

    async def ack(self, delivered: DeliveredMessage) -> None:
        async with self._lock:
            self._pending.pop(delivered.id, None)

    async def nack(self, delivered: DeliveredMessage) -> None:
        """Make the message immediately claimable again."""
        async with self._lock:
            entry = self._pending.get(delivered.id)
            if entry is not None:
                entry.claimed_at = 0.0

    async def claim_stale(
        self, *, consumer: str, min_idle_ms: int, count: int = 10
    ) -> list[DeliveredMessage]:
        async with self._lock:
            now = time.monotonic()
            threshold = min_idle_ms / 1000.0
            out: list[DeliveredMessage] = []
            for entry in list(self._pending.values())[:count]:
                if entry.claimed_at is None or (now - entry.claimed_at) < threshold:
                    continue
                entry.delivery_count += 1
                entry.claimed_at = now
                entry.consumer = consumer
                out.append(
                    DeliveredMessage(
                        id=entry.id, message=entry.message, delivery_count=entry.delivery_count
                    )
                )
            return out

    async def dead_letter(self, delivered: DeliveredMessage, reason: str) -> None:
        async with self._lock:
            self._pending.pop(delivered.id, None)
            self.dead_lettered.append((delivered.message, reason))

    async def stats(self) -> dict[str, Any]:
        return {
            "length": len(self._ready),
            "pending": len(self._pending),
            "dead_letter_length": len(self.dead_lettered),
        }

    async def health(self) -> ProviderHealth:
        return ProviderHealth(self.name, ok=True, extra=await self.stats())

    async def delivery_count(self, message_id: str) -> int:
        entry = self._pending.get(message_id)
        return entry.delivery_count if entry else 1

    # -- test helpers --------------------------------------------------------

    def simulate_crash(self) -> None:
        """Mark every in-flight message as abandoned by its consumer."""
        for entry in self._pending.values():
            entry.claimed_at = 0.0
