"""Model routing.

The router decides which provider and model serves a request. It reasons
entirely over **declared capabilities** -- context window, tool support,
structured-output support, tier, privacy, cost -- and never over provider names.
That is what makes a newly configured vendor routable the moment it appears in
the manifest, with no code change anywhere.

The spec's starting rule ("simple tasks local, complex tasks cloud") is
expressed here as attributes rather than as a hardcoded branch, which is why the
richer rules it anticipates -- cost, latency, capability, context window,
privacy, availability -- drop in as additional predicates over the same
candidate set instead of as a rewrite.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.core.config import ContextSettings, Settings
from app.core.enums import ModelTier, Privacy, TaskKind
from app.core.errors import NoModelAvailable
from app.core.logging import get_logger
from app.providers.llm.base import GenerationParams, ModelInfo, ModelProvider
from app.providers.registry import ProviderBundle
from app.services.rag.prompts import TASK_TEMPERATURE

log = get_logger(__name__)

#: Tasks that are classification-shaped: cheap, deterministic, short output.
_CHEAP_TASKS = frozenset({TaskKind.CLASSIFY, TaskKind.REWRITE, TaskKind.TITLE, TaskKind.PLAN})
#: Document classes where a wrong answer is expensive enough to justify the
#: quality tier regardless of cost.
_HIGH_STAKES_TYPES = frozenset({"legal", "hr", "safety", "compliance", "medical"})

_TIER_ORDER: dict[ModelTier, int] = {
    ModelTier.FAST: 0,
    ModelTier.BALANCED: 1,
    ModelTier.QUALITY: 2,
}


@dataclass(frozen=True, slots=True)
class RoutingRequest:
    task: TaskKind
    est_prompt_tokens: int = 0
    needs_tools: bool = False
    needs_json: bool = False
    needs_streaming: bool = False
    #: Forbids cloud providers entirely. Set per organization for tenants whose
    #: data may not leave their own infrastructure.
    require_local: bool = False
    document_types: tuple[str, ...] = ()
    language: str = "en"
    #: "provider/model" requested by the caller. Honoured only if allowed.
    prefer_model: str | None = None
    #: Organization pins, keyed by task name. Highest priority rule.
    model_pins: dict[str, str] = field(default_factory=dict)
    allowed_providers: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    provider: str
    model: str
    params: GenerationParams
    rule: str
    reason: str
    fallbacks: tuple[tuple[str, str], ...] = ()
    #: Set when even the largest available window is tight for this prompt; the
    #: context builder shrinks its budget in response.
    compaction_required: bool = False

    @property
    def qualified(self) -> str:
        return f"{self.provider}/{self.model}"

    def to_trace(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "rule": self.rule,
            "reason": self.reason,
            "fallbacks": [f"{p}/{m}" for p, m in self.fallbacks],
        }


@dataclass(slots=True)
class _Candidate:
    provider: ModelProvider
    info: ModelInfo

    @property
    def key(self) -> tuple[str, str]:
        return self.provider.name, self.info.name


class ModelRouter:
    """Selects a provider and model for a task."""

    def __init__(self, providers: ProviderBundle, settings: Settings) -> None:
        self.providers = providers
        self.settings = settings
        self.context: ContextSettings = settings.context
        self._catalog: list[_Candidate] = []
        self._catalog_built_at = 0.0
        self._catalog_ttl_s = 300.0

    async def route(self, request: RoutingRequest) -> RoutingDecision:
        candidates = await self._candidates(request)
        if not candidates:
            raise NoModelAvailable(
                "No configured model satisfies this request. Check that at least one "
                "provider is reachable and that its models declare the required "
                "capabilities."
            )

        chosen, rule, reason = self._select(candidates, request)
        fallbacks = tuple(c.key for c in candidates if c.key != chosen.key)[:3]

        compaction = (
            request.est_prompt_tokens
            > chosen.info.context_window * self.context.context_window_utilization
        )

        decision = RoutingDecision(
            provider=chosen.provider.name,
            model=chosen.info.name,
            params=self._params(request, chosen.info),
            rule=rule,
            reason=reason,
            fallbacks=fallbacks,
            compaction_required=compaction,
        )
        log.info("model_routed", task=request.task.value, **decision.to_trace())
        return decision

    # -- candidate selection -------------------------------------------------

    async def _candidates(self, request: RoutingRequest) -> list[_Candidate]:
        """Every model that *could* serve this request.

        Filters are hard requirements, not preferences: a model without tool
        support cannot serve a tool turn, and a model whose provider's circuit
        is open cannot serve anything.
        """
        catalog = await self._load_catalog()
        out: list[_Candidate] = []

        for candidate in catalog:
            info = candidate.info

            if not getattr(candidate.provider, "is_available", True):
                continue
            if (
                request.allowed_providers
                and candidate.provider.name not in request.allowed_providers
            ):
                continue
            if request.require_local and info.privacy is not Privacy.LOCAL:
                continue
            if request.needs_tools and not info.supports_tools:
                continue
            if request.needs_json and not info.supports_json_schema:
                continue
            if request.needs_streaming and not info.supports_streaming:
                continue
            # Leave room for the answer, not just the prompt.
            usable = info.context_window * self.context.context_window_utilization
            if request.est_prompt_tokens and request.est_prompt_tokens > usable:
                continue
            out.append(candidate)

        # Keep a model with too small a window rather than failing outright when
        # nothing else qualifies; compaction_required will tell the caller.
        if not out and request.est_prompt_tokens:
            relaxed = RoutingRequest(
                task=request.task,
                est_prompt_tokens=0,
                needs_tools=request.needs_tools,
                needs_json=request.needs_json,
                needs_streaming=request.needs_streaming,
                require_local=request.require_local,
                allowed_providers=request.allowed_providers,
            )
            return await self._candidates(relaxed)

        return out

    def _select(
        self, candidates: list[_Candidate], request: RoutingRequest
    ) -> tuple[_Candidate, str, str]:
        """Ordered rules, first match wins."""
        by_key = {c.key: c for c in candidates}

        # 1. Organization pin. Enterprises pin models for compliance, so this
        #    outranks every heuristic below.
        pinned = request.model_pins.get(request.task.value) or request.model_pins.get("*")
        if pinned and (candidate := self._lookup(pinned, by_key)):
            return candidate, "org_pin", f"organization pinned {pinned} for {request.task.value}"

        # 2. Caller preference, if the organization allows that provider.
        if request.prefer_model and (candidate := self._lookup(request.prefer_model, by_key)):
            return candidate, "caller_preference", f"caller requested {request.prefer_model}"

        # 3. Tool turns take the smallest capable model. Choosing which tool to
        #    call is an easier task than composing the final answer, and on a
        #    serialized local model server the quality tier is a scarce slot.
        if request.needs_tools:
            candidate = min(
                candidates,
                key=lambda c: (_TIER_ORDER[c.info.tier], _cost_of(c.info)),
            )
            return candidate, "tool_capable", "smallest tool-capable model"

        # 4. Cheap tasks take the fast tier.
        if request.task in _CHEAP_TASKS:
            candidate = self._best_of_tier(candidates, ModelTier.FAST) or self._cheapest(candidates)
            return candidate, "cheap_task", f"{request.task.value} runs on the fast tier"

        # 5. High-stakes subject matter takes the quality tier.
        if request.task is TaskKind.ANSWER and (set(request.document_types) & _HIGH_STAKES_TYPES):
            candidate = self._best_of_tier(candidates, ModelTier.QUALITY) or self._best(candidates)
            return candidate, "high_stakes", "high-stakes document types in context"

        # 6. Default: balanced, preferring local when it qualifies. Local first
        #    is the spec's "simple -> local" rule expressed as a preference over
        #    declared privacy rather than over a provider name.
        balanced = self._best_of_tier(candidates, ModelTier.BALANCED)
        if balanced is not None:
            return balanced, "default_balanced", "balanced tier"
        return self._best(candidates), "default_any", "no balanced-tier model available"

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _lookup(qualified: str, by_key: dict[tuple[str, str], _Candidate]) -> _Candidate | None:
        if "/" in qualified:
            provider, model = qualified.split("/", 1)
            return by_key.get((provider, model))
        return next((c for (_, m), c in by_key.items() if m == qualified), None)

    @staticmethod
    def _best_of_tier(candidates: list[_Candidate], tier: ModelTier) -> _Candidate | None:
        """Cheapest model in a tier, preferring local at equal cost."""
        matching = [c for c in candidates if c.info.tier is tier]
        if not matching:
            return None
        return min(
            matching,
            key=lambda c: (_cost_of(c.info), c.info.privacy is not Privacy.LOCAL),
        )

    @staticmethod
    def _cheapest(candidates: list[_Candidate]) -> _Candidate:
        return min(candidates, key=lambda c: (_cost_of(c.info), _TIER_ORDER[c.info.tier]))

    @staticmethod
    def _best(candidates: list[_Candidate]) -> _Candidate:
        return max(candidates, key=lambda c: (_TIER_ORDER[c.info.tier], -_cost_of(c.info)))

    def _params(self, request: RoutingRequest, info: ModelInfo) -> GenerationParams:
        return GenerationParams(
            temperature=TASK_TEMPERATURE.get(request.task, 0.2),
            max_tokens=min(info.max_output_tokens, self.context.reserve_output_tokens * 2),
        )

    async def _load_catalog(self) -> list[_Candidate]:
        """Providers' model lists, cached briefly.

        ``list_models`` can touch the network, and routing happens several times
        per request; a short TTL keeps a cold model server from adding latency to
        every turn while still picking up a newly pulled model within minutes.
        """
        now = time.monotonic()
        if self._catalog and (now - self._catalog_built_at) < self._catalog_ttl_s:
            return self._catalog

        catalog: list[_Candidate] = []
        for provider in self.providers.llm.values():
            try:
                for info in await provider.list_models():
                    catalog.append(_Candidate(provider=provider, info=info))
            except Exception as exc:  # noqa: BLE001 - a dead provider is skipped
                log.warning("catalog_unavailable", provider=provider.name, error=str(exc)[:200])

        self._catalog = catalog
        self._catalog_built_at = now
        return catalog

    def invalidate_catalog(self) -> None:
        self._catalog = []
        self._catalog_built_at = 0.0


def _cost_of(info: ModelInfo) -> float:
    """Blended cost per million tokens, weighted toward input.

    RAG prompts are input-heavy: a few thousand tokens of context for a few
    hundred tokens of answer. Weighting 3:1 reflects that better than averaging.
    """
    return (info.cost_per_1m_input * 3 + info.cost_per_1m_output) / 4
