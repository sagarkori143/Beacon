"""Carrying ingestion progress from the worker to whoever is watching.

The worker and the API are separate processes -- separate containers, scaled
independently -- so a browser watching a document being processed cannot simply
read the worker's memory. Something has to carry each stage transition across
that gap while the job is still running.

This is an interface rather than a Redis call in the pipeline for the same
reason every other boundary here is: the pipeline should not know what Redis is,
and a deployment that wants progress over NATS or Postgres LISTEN should not
have to edit an ingestion stage to get it.

Note what this is *not*: the durable record of what happened is
``ingestion_job_events`` in Postgres, written in the same transaction as the
stage update. This carries a copy for live viewing, and is allowed to lose
events, expire, or be switched off entirely without anything being lost.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.core.config import Settings
from app.core.errors import ConfigurationError
from app.providers.base import ProviderHealth


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One thing that happened to a job, as a watcher would want to see it."""

    job_id: UUID
    stage: str
    status: str
    progress: float
    message: str | None = None
    duration_ms: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_payload(self) -> dict[str, Any]:
        return {
            "job_id": str(self.job_id),
            "stage": self.stage,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "duration_ms": self.duration_ms,
            "detail": self.detail,
            "at": self.at.isoformat(),
        }


class ProgressPublisher(ABC):
    """Publishes stage transitions for live viewing."""

    @abstractmethod
    async def publish(self, event: ProgressEvent) -> None:
        """Announce one event. Must never raise -- see the note below.

        A failure to publish is a failure to *show* something, not a failure to
        *do* it. An ingestion run that completed correctly must not be marked
        failed because a watcher's feed hiccupped, so implementations swallow
        their own errors and log them.
        """

    @abstractmethod
    async def follow(self, job_id: UUID, *, after: str | None = None):
        """Yield events for a job as they arrive, resuming after a cursor."""

    @abstractmethod
    async def cursor(self, job_id: UUID) -> str:
        """The position to resume from to receive only what happens next.

        A caller that replays durable history first needs this *before* it
        reads that history. Publishing happens after the database commit, so
        anything already in the feed is also in the history and would otherwise
        be delivered twice; anything published during the read arrives after
        this cursor and is not missed.
        """

    async def health(self) -> ProviderHealth:  # pragma: no cover - trivial
        return ProviderHealth(ok=True, detail={"provider": type(self).__name__})


_TYPES: dict[str, type[ProgressPublisher]] = {}


def register_progress_publisher(name: str):
    def decorator(cls: type[ProgressPublisher]) -> type[ProgressPublisher]:
        _TYPES[name] = cls
        return cls

    return decorator


def _load_builtin() -> None:
    from app.providers.progress import memory, redis_stream  # noqa: F401


def build_progress_publisher(settings: Settings, **kwargs: Any) -> ProgressPublisher:
    _load_builtin()
    # Progress rides on whatever the queue already uses: if the deployment has
    # Redis for the queue, it has Redis for this. There is no separate knob to
    # get wrong.
    name = "redis" if settings.queue.provider == "redis_streams" else "memory"
    cls = _TYPES.get(name)
    if cls is None:  # pragma: no cover - registry is populated above
        raise ConfigurationError(f"Unknown progress publisher '{name}'")
    return cls(settings, **kwargs)  # type: ignore[call-arg]


__all__ = [
    "ProgressEvent",
    "ProgressPublisher",
    "build_progress_publisher",
    "register_progress_publisher",
]
