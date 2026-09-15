"""Model routing.

The property under test throughout: routing decisions come from declared
capabilities, never from provider names. A provider added to the manifest with
the right attributes becomes routable with no code change.
"""

from __future__ import annotations

import pytest

from app.core.config import LLMProviderConfig, ModelConfig, Settings
from app.core.enums import ModelTier, Privacy, TaskKind
from app.core.errors import NoModelAvailable
from app.providers.llm.fake import FakeLLMProvider
from app.providers.registry import ProviderBundle
from app.services.agent.router import ModelRouter, RoutingRequest

pytestmark = pytest.mark.unit


def provider(name: str, *models: ModelConfig, privacy: Privacy = Privacy.CLOUD) -> FakeLLMProvider:
    return FakeLLMProvider(
        LLMProviderConfig(name=name, type="fake", privacy=privacy, models=list(models))
    )


def model(
    name: str,
    tier: ModelTier = ModelTier.BALANCED,
    *,
    tools: bool = True,
    json_schema: bool = True,
    window: int = 32_000,
    cost_in: float = 0.0,
    cost_out: float = 0.0,
) -> ModelConfig:
    return ModelConfig(
        name=name,
        tier=tier,
        supports_tools=tools,
        supports_json_schema=json_schema,
        context_window=window,
        cost_per_1m_input=cost_in,
        cost_per_1m_output=cost_out,
    )


@pytest.fixture
def mixed_bundle() -> ProviderBundle:
    """A local model server plus a cloud vendor, as a real deployment has."""
    local = provider(
        "local",
        model("small-local", ModelTier.FAST, window=8_000),
        model("mid-local", ModelTier.BALANCED, window=32_000),
        privacy=Privacy.LOCAL,
    )
    local.privacy = Privacy.LOCAL
    cloud = provider(
        "cloud",
        model("cheap-cloud", ModelTier.FAST, window=128_000, cost_in=0.5, cost_out=1.5),
        model("big-cloud", ModelTier.QUALITY, window=200_000, cost_in=3.0, cost_out=15.0),
    )
    return ProviderBundle(llm={"local": local, "cloud": cloud})


@pytest.fixture
def router(mixed_bundle: ProviderBundle, settings: Settings) -> ModelRouter:
    return ModelRouter(mixed_bundle, settings)


class TestCapabilityFiltering:
    async def test_json_requirement_excludes_incapable_models(self, settings: Settings) -> None:
        """Routing a structured call to a model that cannot constrain output
        means falling back to prompt-and-pray parsing."""
        bundle = ProviderBundle(
            llm={
                "p": provider(
                    "p",
                    model("no-json", json_schema=False),
                    model("with-json", json_schema=True),
                )
            }
        )
        decision = await ModelRouter(bundle, settings).route(
            RoutingRequest(task=TaskKind.PLAN, needs_json=True)
        )
        assert decision.model == "with-json"

    async def test_tool_requirement_excludes_incapable_models(self, settings: Settings) -> None:
        bundle = ProviderBundle(
            llm={"p": provider("p", model("no-tools", tools=False), model("tooled"))}
        )
        decision = await ModelRouter(bundle, settings).route(
            RoutingRequest(task=TaskKind.TOOL_TURN, needs_tools=True)
        )
        assert decision.model == "tooled"

    async def test_context_window_pressure_escalates(self, router: ModelRouter) -> None:
        """A 100k-token prompt cannot go to a 32k model."""
        decision = await router.route(
            RoutingRequest(task=TaskKind.ANSWER, est_prompt_tokens=100_000)
        )
        assert decision.model in ("cheap-cloud", "big-cloud")

    async def test_no_capable_model_raises_rather_than_guessing(self, settings: Settings) -> None:
        bundle = ProviderBundle(
            llm={"p": provider("p", model("basic", tools=False, json_schema=False))}
        )
        with pytest.raises(NoModelAvailable):
            await ModelRouter(bundle, settings).route(
                RoutingRequest(task=TaskKind.TOOL_TURN, needs_tools=True)
            )

    async def test_empty_bundle_raises(self, settings: Settings) -> None:
        with pytest.raises(NoModelAvailable):
            await ModelRouter(ProviderBundle(), settings).route(
                RoutingRequest(task=TaskKind.ANSWER)
            )


class TestRoutingRules:
    async def test_org_pin_outranks_every_heuristic(self, router: ModelRouter) -> None:
        """Enterprises pin models for compliance; nothing may override that."""
        decision = await router.route(
            RoutingRequest(task=TaskKind.ANSWER, model_pins={"answer": "cloud/big-cloud"})
        )
        assert decision.qualified == "cloud/big-cloud"
        assert decision.rule == "org_pin"

    async def test_wildcard_pin_applies_to_every_task(self, router: ModelRouter) -> None:
        decision = await router.route(
            RoutingRequest(task=TaskKind.PLAN, model_pins={"*": "local/mid-local"})
        )
        assert decision.qualified == "local/mid-local"

    async def test_cheap_tasks_take_the_fast_tier(self, router: ModelRouter) -> None:
        decision = await router.route(RoutingRequest(task=TaskKind.CLASSIFY))
        assert decision.rule == "cheap_task"
        assert decision.model in ("small-local", "cheap-cloud")

    async def test_tool_turns_take_the_smallest_capable_model(self, router: ModelRouter) -> None:
        """Choosing which tool to call is easier than writing the answer."""
        decision = await router.route(RoutingRequest(task=TaskKind.TOOL_TURN, needs_tools=True))
        assert decision.rule == "tool_capable"
        assert decision.model in ("small-local", "cheap-cloud")

    async def test_high_stakes_content_takes_the_quality_tier(self, router: ModelRouter) -> None:
        decision = await router.route(
            RoutingRequest(task=TaskKind.ANSWER, document_types=("legal",))
        )
        assert decision.rule == "high_stakes"
        assert decision.model == "big-cloud"

    async def test_privacy_requirement_excludes_cloud_entirely(self, router: ModelRouter) -> None:
        """A tenant whose data may not leave their infrastructure."""
        decision = await router.route(RoutingRequest(task=TaskKind.ANSWER, require_local=True))
        assert decision.provider == "local"

    async def test_allow_list_restricts_providers(self, router: ModelRouter) -> None:
        decision = await router.route(
            RoutingRequest(task=TaskKind.ANSWER, allowed_providers=("local",))
        )
        assert decision.provider == "local"


class TestDecisionMetadata:
    async def test_fallbacks_are_offered(self, router: ModelRouter) -> None:
        decision = await router.route(RoutingRequest(task=TaskKind.ANSWER))
        assert decision.fallbacks
        assert decision.qualified not in {f"{p}/{m}" for p, m in decision.fallbacks}

    async def test_trace_records_the_rule_that_fired(self, router: ModelRouter) -> None:
        """ "Why did it pick that model?" must be answerable from the trace."""
        trace = (await router.route(RoutingRequest(task=TaskKind.ANSWER))).to_trace()
        assert {"provider", "model", "rule", "reason"} <= trace.keys()

    async def test_unhealthy_provider_is_skipped(self, router: ModelRouter) -> None:
        """An open circuit removes a provider from consideration entirely."""
        local = router.providers.llm["local"]
        local.is_available = False  # type: ignore[attr-defined]
        router.invalidate_catalog()

        decision = await router.route(RoutingRequest(task=TaskKind.CLASSIFY))
        assert decision.provider == "cloud"
