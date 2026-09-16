"""In-process progress, for tests and for a deployment with no Redis.

Publishing to it is a no-op that records, so the pipeline behaves identically
whether or not anyone is watching -- which is the property the tests need.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from app.core.config import Settings
from app.providers.progress.base import (
    ProgressEvent,
    ProgressPublisher,
    register_progress_publisher,
)


@register_progress_publisher("memory")
class MemoryProgress(ProgressPublisher):
    def __init__(self, settings: Settings, **_: Any) -> None:
        self.settings = settings
        self.published: list[ProgressEvent] = []

    async def publish(self, event: ProgressEvent) -> None:
        self.published.append(event)

    async def cursor(self, job_id: UUID) -> str:
        return str(len(self.published))

    async def follow(
        self, job_id: UUID, *, after: str | None = None
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        start = int(after) if after else 0
        for index, event in enumerate(self.published):
            if index >= start and event.job_id == job_id:
                yield str(index), event.to_payload()
