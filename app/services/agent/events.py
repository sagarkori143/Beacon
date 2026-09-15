"""Agent events.

The agent emits one stream of typed events; both the streaming and the
non-streaming API are built from it, so the two cannot drift apart in what they
report.

These are *semantic* events, not raw model output. That is a deliberate choice
for the tool phase: a partially-streamed tool-arguments blob tells a user
nothing -- you cannot act on half a tool call -- while "looking up availability
for 2026-10-02" is genuinely useful progress. Token deltas are streamed only for
the final answer, where they are what the user is waiting for.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class AgentEvent:
    type: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_sse(self, event_id: str | None = None) -> str:
        """Render as a Server-Sent Event frame."""
        payload = json.dumps(self.data, ensure_ascii=False, default=str)
        prefix = f"id: {event_id}\n" if event_id else ""
        return f"{prefix}event: {self.type}\ndata: {payload}\n\n"


def stage(name: str, **extra: Any) -> AgentEvent:
    return AgentEvent("stage", {"stage": name, **extra})


def plan_event(plan: dict[str, Any]) -> AgentEvent:
    return AgentEvent("plan", plan)


def search_event(
    *, queries: list[str], hits: int, per_scope: dict[str, int], degraded: bool
) -> AgentEvent:
    return AgentEvent(
        "search",
        {"queries": queries, "hits": hits, "per_scope": per_scope, "degraded": degraded},
    )


def conflict_event(*, suppressed: int, topics: list[str]) -> AgentEvent:
    """Emitted when location knowledge overrode organization knowledge.

    Surfaced to the client on purpose: "we used the Ginza figure, not the
    group-wide one" is exactly the thing a user needs to be able to see when an
    answer surprises them.
    """
    return AgentEvent("conflict", {"suppressed": suppressed, "topics": topics})


def citation_event(citation: dict[str, Any]) -> AgentEvent:
    return AgentEvent("citation", citation)


def tool_call_event(*, id: str, name: str, arguments: dict[str, Any]) -> AgentEvent:
    return AgentEvent("tool_call", {"id": id, "name": name, "arguments": arguments})


def tool_result_event(
    *, id: str, name: str, ok: bool, summary: str, latency_ms: float, error_code: str | None
) -> AgentEvent:
    return AgentEvent(
        "tool_result",
        {
            "id": id,
            "name": name,
            "ok": ok,
            "summary": summary[:300],
            "latency_ms": round(latency_ms, 1),
            "error_code": error_code,
        },
    )


def token_event(text: str) -> AgentEvent:
    return AgentEvent("token", {"text": text})


def usage_event(*, prompt_tokens: int, completion_tokens: int, cost_usd: float) -> AgentEvent:
    return AgentEvent(
        "usage",
        {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "estimated_cost_usd": round(cost_usd, 6),
        },
    )


def done_event(
    *,
    message_id: str | None,
    conversation_id: str | None,
    trace_id: str,
    finish_reason: str,
    grounding: float,
) -> AgentEvent:
    return AgentEvent(
        "done",
        {
            "message_id": message_id,
            "conversation_id": conversation_id,
            "trace_id": trace_id,
            "finish_reason": finish_reason,
            "grounding_ratio": round(grounding, 3),
        },
    )


def error_event(*, code: str, message: str, retryable: bool = False) -> AgentEvent:
    return AgentEvent("error", {"code": code, "message": message, "retryable": retryable})
