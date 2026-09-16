"""Progress over a Redis Stream, one stream per job.

A Stream rather than pub/sub, for two reasons that matter here:

* pub/sub drops anything published while nobody is listening, and the first
  stages of an ingestion run happen in the seconds before a browser has finished
  opening the page. Those are exactly the stages someone is watching for.
* a Stream gives every entry an id, so a reconnecting watcher can say "carry on
  after this one" instead of starting over or missing the gap.

Streams are already the proven cross-process mechanism in this codebase -- the
ingestion queue is one -- so this introduces no new operational surface.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.redis import run_events_key
from app.providers.progress.base import (
    ProgressEvent,
    ProgressPublisher,
    register_progress_publisher,
)

log = get_logger(__name__)

#: Long enough that a watcher who opens the page after a run finished still sees
#: the whole thing, short enough that finished jobs do not accumulate.
_TTL_S = 3600

#: A run has a bounded number of stages; this is generous headroom, and caps
#: what one pathological job can hold in memory.
_MAX_LEN = 500


@register_progress_publisher("redis")
class RedisStreamProgress(ProgressPublisher):
    def __init__(self, settings: Settings, *, redis: Redis | None = None) -> None:
        self.settings = settings
        self._redis = redis

    @property
    def redis(self) -> Redis:
        if self._redis is None:  # pragma: no cover - wiring error
            raise RuntimeError("RedisStreamProgress was built without a Redis client")
        return self._redis

    async def publish(self, event: ProgressEvent) -> None:
        """Announce one event, and never let that failure become the job's.

        The durable record is the `ingestion_job_events` row written in the same
        transaction as the stage update. This is a copy for live viewing, so a
        Redis hiccup should cost a watcher a frame, not cost the document its
        ingestion.
        """
        key = run_events_key(event.job_id)
        payload = event.to_payload()
        try:
            fields: dict[str, Any] = {
                "stage": payload["stage"],
                "status": payload["status"],
                "progress": str(payload["progress"]),
                "message": payload["message"] or "",
                "duration_ms": ""
                if payload["duration_ms"] is None
                else str(payload["duration_ms"]),
                "detail": _dumps(payload["detail"]),
                "at": payload["at"],
            }
            pipeline = self.redis.pipeline()
            pipeline.xadd(key, fields, maxlen=_MAX_LEN, approximate=True)
            pipeline.expire(key, _TTL_S)
            await pipeline.execute()
        except Exception as exc:  # noqa: BLE001 - visibility must not break work
            log.warning(
                "progress_publish_failed",
                job_id=str(event.job_id),
                stage=event.stage,
                error=str(exc)[:200],
            )

    async def follow(
        self, job_id: UUID, *, after: str | None = None
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Yield `(entry_id, event)` pairs as they arrive.

        Starts after ``after`` when given, so a reconnecting watcher resumes
        rather than replaying. ``"0"`` means "everything this stream still
        holds", which is how a late arrival catches up.
        """
        cursor = after or "0"
        key = run_events_key(job_id)

        while True:
            try:
                batches = await self.redis.xread({key: cursor}, count=50, block=15_000)
            except RedisTimeoutError:
                # An idle read is how a quiet stage looks, not a failure.
                yield "", {}
                continue
            except Exception as exc:  # noqa: BLE001
                log.warning("progress_follow_failed", job_id=str(job_id), error=str(exc)[:200])
                return

            if not batches:
                # Nothing new: hand the caller a beat so it can send a keep-alive
                # and notice a disconnected client.
                yield "", {}
                continue

            for _stream, entries in batches:
                for entry_id, fields in entries:
                    cursor = entry_id
                    yield entry_id, _to_event(fields)


def _dumps(value: Any) -> str:
    import json

    try:
        return json.dumps(value, default=str)
    except Exception:  # noqa: BLE001 - detail is diagnostic, never load-bearing
        return "{}"


def _to_event(fields: dict[str, str]) -> dict[str, Any]:
    import json

    try:
        detail = json.loads(fields.get("detail") or "{}")
    except Exception:  # noqa: BLE001
        detail = {}

    duration = fields.get("duration_ms") or ""
    return {
        "stage": fields.get("stage", ""),
        "status": fields.get("status", ""),
        "progress": float(fields.get("progress") or 0.0),
        "message": fields.get("message") or None,
        "duration_ms": float(duration) if duration else None,
        "detail": detail,
        "at": fields.get("at"),
    }
