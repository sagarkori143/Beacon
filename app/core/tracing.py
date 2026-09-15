"""Tracing primitives.

:class:`TraceContext` is passed **explicitly** into every provider and service
call rather than relying on contextvars alone. The agent fans out with
``asyncio.gather`` over parallel searches and tool calls, where implicit context
silently misattributes spans; an explicit parameter is trivially correct and
makes the dependency visible in the signature.

Spans here are lightweight: they record timings and attributes into an in-memory
tree that becomes the ``AgentTrace`` returned to the caller and the structured
log line. An OpenTelemetry exporter can wrap this without changing call sites.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.core.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class Span:
    name: str
    span_id: str
    parent_id: str | None
    started_at: float
    ended_at: float | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def duration_ms(self) -> float:
        end = self.ended_at if self.ended_at is not None else time.perf_counter()
        return (end - self.started_at) * 1000.0

    def set(self, **attrs: Any) -> None:
        self.attributes.update(attrs)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "duration_ms": round(self.duration_ms, 2),
            **self.attributes,
        }
        if self.error:
            out["error"] = self.error
        return out


@dataclass(slots=True)
class TraceContext:
    """Correlation identity threaded through a single request or job."""

    trace_id: str
    organization_id: UUID | None = None
    user_id: UUID | None = None
    request_id: str | None = None
    run_id: UUID | None = None
    job_id: UUID | None = None
    span_id: str | None = None
    spans: list[Span] = field(default_factory=list)

    @classmethod
    def new(
        cls,
        *,
        organization_id: UUID | None = None,
        user_id: UUID | None = None,
        request_id: str | None = None,
        run_id: UUID | None = None,
        job_id: UUID | None = None,
    ) -> TraceContext:
        return cls(
            trace_id=uuid.uuid4().hex,
            organization_id=organization_id,
            user_id=user_id,
            request_id=request_id,
            run_id=run_id,
            job_id=job_id,
        )

    @contextmanager
    def span(self, name: str, **attrs: Any) -> Iterator[Span]:
        """Open a child span. Records duration and any exception, then re-raises."""
        s = Span(
            name=name,
            span_id=uuid.uuid4().hex[:16],
            parent_id=self.span_id,
            started_at=time.perf_counter(),
            attributes=dict(attrs),
        )
        self.spans.append(s)
        previous, self.span_id = self.span_id, s.span_id
        try:
            yield s
        except Exception as exc:
            s.error = type(exc).__name__
            raise
        finally:
            s.ended_at = time.perf_counter()
            self.span_id = previous

    def child(self, **overrides: Any) -> TraceContext:
        """A context sharing this trace's identity and span list.

        Used when handing the trace to a nested component that should contribute
        spans to the same tree.
        """
        data = {
            "trace_id": self.trace_id,
            "organization_id": self.organization_id,
            "user_id": self.user_id,
            "request_id": self.request_id,
            "run_id": self.run_id,
            "job_id": self.job_id,
            "span_id": self.span_id,
            "spans": self.spans,
        }
        data.update(overrides)
        return TraceContext(**data)

    def timeline(self) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self.spans]

    def total_ms(self) -> float:
        return sum(s.duration_ms for s in self.spans if s.parent_id is None)


def null_trace() -> TraceContext:
    """A throwaway trace for call sites that do not have one (tests, scripts)."""
    return TraceContext.new()
