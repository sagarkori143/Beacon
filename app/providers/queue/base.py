"""Queue abstraction for asynchronous ingestion work.

The contract is deliberately at-least-once with explicit acknowledgement, not
fire-and-forget. A worker that dies mid-pipeline must have its message reclaimed
by another worker, which is why ``claim_stale`` is part of the interface rather
than an implementation detail: any backend that cannot do it (Kafka, SQS,
RabbitMQ can all do it differently) still has to express it here.

Because delivery is at-least-once, **every pipeline stage must be idempotent.**
That requirement is carried by the ingestion stages, not by the queue.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypeVar
from uuid import UUID

from app.core.config import QueueSettings
from app.core.errors import ConfigurationError
from app.providers.base import ProviderHealth


@dataclass(frozen=True, slots=True)
class QueueMessage:
    """Payload enqueued for an ingestion run.

    Carries the tenant explicitly so a worker can open a correctly-scoped
    database session before reading anything -- there is no unscoped lookup to
    discover which organization a job belongs to.
    """

    job_id: UUID
    organization_id: UUID
    document_id: UUID
    document_version_id: UUID
    trace_id: str | None = None
    attempt: int = 0
    payload: dict[str, Any] = field(default_factory=dict)

    def to_fields(self) -> dict[str, str]:
        return {
            "job_id": str(self.job_id),
            "organization_id": str(self.organization_id),
            "document_id": str(self.document_id),
            "document_version_id": str(self.document_version_id),
            "trace_id": self.trace_id or "",
            "attempt": str(self.attempt),
        }

    @classmethod
    def from_fields(cls, fields: dict[str, str]) -> QueueMessage:
        return cls(
            job_id=UUID(fields["job_id"]),
            organization_id=UUID(fields["organization_id"]),
            document_id=UUID(fields["document_id"]),
            document_version_id=UUID(fields["document_version_id"]),
            trace_id=fields.get("trace_id") or None,
            attempt=int(fields.get("attempt") or 0),
        )


@dataclass(frozen=True, slots=True)
class DeliveredMessage:
    """A message handed to a consumer, awaiting acknowledgement."""

    id: str
    message: QueueMessage
    #: How many times this message has been delivered, including now. Used to
    #: decide when to give up and dead-letter.
    delivery_count: int = 1
    delivered_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_redelivery(self) -> bool:
        return self.delivery_count > 1


class QueueProvider(ABC):
    name: str

    @abstractmethod
    async def setup(self) -> None:
        """Create whatever the backend needs (stream, consumer group, topic)."""

    @abstractmethod
    async def enqueue(self, message: QueueMessage) -> str: ...

    @abstractmethod
    async def consume(
        self, *, consumer: str, count: int = 1, block_ms: int = 5000
    ) -> list[DeliveredMessage]: ...

    @abstractmethod
    async def ack(self, delivered: DeliveredMessage) -> None: ...

    @abstractmethod
    async def nack(self, delivered: DeliveredMessage) -> None:
        """Return a message for redelivery without acknowledging it."""

    @abstractmethod
    async def claim_stale(
        self, *, consumer: str, min_idle_ms: int, count: int = 10
    ) -> list[DeliveredMessage]:
        """Take over messages a dead consumer never acknowledged."""

    @abstractmethod
    async def dead_letter(self, delivered: DeliveredMessage, reason: str) -> None:
        """Move a message that exhausted its attempts out of the main flow."""

    @abstractmethod
    async def stats(self) -> dict[str, Any]: ...

    @abstractmethod
    async def health(self) -> ProviderHealth: ...

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

TQueue = TypeVar("TQueue", bound="type[QueueProvider]")

_QUEUE_TYPES: dict[str, type[QueueProvider]] = {}


def register_queue_provider(type_name: str) -> Callable[[TQueue], TQueue]:
    def decorator(cls: TQueue) -> TQueue:
        _QUEUE_TYPES[type_name] = cls
        return cls

    return decorator


_loaded = False


def _load_builtin() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    from app.providers.queue import memory, redis_streams  # noqa: F401


def build_queue_provider(settings: QueueSettings, **kwargs: Any) -> QueueProvider:
    _load_builtin()
    cls = _QUEUE_TYPES.get(settings.provider)
    if cls is None:
        raise ConfigurationError(
            f"Unknown queue provider '{settings.provider}'. "
            f"Known: {', '.join(sorted(_QUEUE_TYPES))}"
        )
    return cls(settings, **kwargs)  # type: ignore[call-arg]


__all__ = [
    "DeliveredMessage",
    "QueueMessage",
    "QueueProvider",
    "Sequence",
    "build_queue_provider",
    "register_queue_provider",
]
