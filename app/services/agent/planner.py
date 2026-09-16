"""The planning step.

One cheap structured call decides what kind of request this is before any
expensive work happens. It earns its cost three ways: small talk skips retrieval
entirely, an ambiguous request asks a clarifying question instead of guessing,
and a question that does need documents gets searched with the vocabulary those
documents actually use rather than the user's phrasing.

The result is validated against a Pydantic schema before anything acts on it --
a plan is a decision the system will execute, so it is never a parsed blob of
model prose.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.enums import TaskKind
from app.core.logging import get_logger
from app.core.tracing import TraceContext
from app.providers.llm.base import Message, ModelProvider
from app.services.rag.prompts import planner_system

log = get_logger(__name__)


class Intent(StrEnum):
    ANSWER_FROM_KNOWLEDGE = "answer_from_knowledge"
    TOOL_ACTION = "tool_action"
    SMALL_TALK = "small_talk"
    NEEDS_CLARIFICATION = "needs_clarification"
    OUT_OF_SCOPE = "out_of_scope"


class Plan(BaseModel):
    """The structured decision the agent acts on."""

    intent: Intent = Field(description="What kind of request this is.")
    needs_retrieval: bool = Field(
        description="True when answering requires the organization's documents."
    )
    search_queries: list[str] = Field(
        default_factory=list,
        max_length=3,
        description="Up to 3 short search phrasings, in document vocabulary.",
    )
    candidate_tools: list[str] = Field(
        default_factory=list, max_length=4, description="Tools that would help."
    )
    clarifying_question: str | None = Field(
        default=None, max_length=300, description="Set only when genuinely ambiguous."
    )
    reasoning: str = Field(default="", max_length=400)

    @field_validator("search_queries")
    @classmethod
    def _clean_queries(cls, value: list[str]) -> list[str]:
        return [q.strip() for q in value if q and q.strip()][:3]

    @model_validator(mode="after")
    def _reconcile_retrieval(self) -> Plan:
        """Naming what to search for means a search is needed.

        Models return plans that contradict themselves -- three specific search
        phrasings alongside ``needs_retrieval: false`` -- and small ones do it
        often. Of the two fields, the queries are the stronger signal: they are
        concrete work the model chose to specify, while the boolean is a
        judgement call it can flip for no visible reason.

        Reconciling here rather than at each call site means every consumer sees
        a coherent plan, and follows the same rule the planner is given: an
        unnecessary search is cheap, a confidently wrong answer is not.
        """
        if self.search_queries and not self.needs_retrieval:
            object.__setattr__(self, "needs_retrieval", True)
        return self

    @property
    def needs_tools(self) -> bool:
        return bool(self.candidate_tools) or self.intent is Intent.TOOL_ACTION

    def to_trace(self) -> dict[str, Any]:
        return {
            "intent": self.intent.value,
            "needs_retrieval": self.needs_retrieval,
            "queries": self.search_queries,
            "tools": self.candidate_tools,
        }


def fallback_plan(query: str, *, tool_names: Sequence[str] = ()) -> Plan:
    """The plan used when the planner cannot run.

    Retrieval is enabled and the raw query is used. That is deliberately the
    safe direction: an unnecessary search costs one embedding call, while
    skipping retrieval produces an answer from the model's own priors, which is
    exactly the failure this system exists to prevent.
    """
    return Plan(
        intent=Intent.ANSWER_FROM_KNOWLEDGE,
        needs_retrieval=True,
        search_queries=[query.strip()[:500]],
        candidate_tools=[],
        reasoning="planner unavailable; defaulting to retrieval",
    )


def plan_cache_hash(query: str) -> str:
    return hashlib.blake2b(
        " ".join(query.lower().split()).encode("utf-8"), digest_size=12
    ).hexdigest()


async def make_plan(
    provider: ModelProvider,
    model: str,
    *,
    query: str,
    organization: str,
    location: str | None,
    tools: Sequence[tuple[str, str]],
    history: Sequence[Message] = (),
    trace: TraceContext | None = None,
) -> Plan:
    """Run the planning call, falling back rather than failing the request."""
    messages: list[Message] = [
        Message.system(
            planner_system(organization=organization, location=location, tools=list(tools))
        ),
        *history,
        Message.user(query),
    ]

    try:
        result = await provider.generate_structured(
            model=model,
            messages=messages,
            schema=Plan,
            trace=trace,
        )
    except Exception as exc:  # noqa: BLE001 - never fail a request on planning
        log.warning("planner_failed", error=str(exc)[:200])
        return fallback_plan(query, tool_names=[name for name, _ in tools])

    plan = result.value

    # The model may name a tool that does not exist or that this caller cannot
    # use. Filter rather than trust: the tool loop would reject it anyway, and
    # this keeps the plan honest for the trace.
    known = {name for name, _ in tools}
    plan.candidate_tools = [t for t in plan.candidate_tools if t in known]

    if plan.needs_retrieval and not plan.search_queries:
        plan.search_queries = [query.strip()[:500]]

    log.info("plan_ready", repairs=result.repairs, **plan.to_trace())
    return plan


#: Task used to route the planning call itself.
PLAN_TASK = TaskKind.PLAN
