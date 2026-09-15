"""Deterministic in-process LLM provider.

Used by every test that is not specifically exercising a real vendor, and usable
in development when no model server is reachable. It implements the full
interface -- tools, streaming, structured output -- so the agent under test
takes exactly the same code path it takes in production.

Behaviour is scripted rather than random: queue responses with
``FakeLLMProvider.script(...)`` and they are returned in order. When the script
runs out it echoes a deterministic answer derived from the last user message, so
a test that forgot to script still gets a stable result instead of a crash.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

from app.core.config import LLMProviderConfig, ModelConfig
from app.core.enums import ModelTier, Privacy
from app.providers.base import ProviderHealth
from app.providers.llm.base import (
    Completion,
    GenerationParams,
    Message,
    ModelInfo,
    ModelProvider,
    StreamDone,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallComplete,
    ToolSpec,
    Usage,
    UsageEvent,
)
from app.providers.llm.registry import register_llm_provider


@dataclass(slots=True)
class ScriptedResponse:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    #: Returned verbatim by ``generate_structured``; must satisfy the schema.
    structured: dict[str, Any] | None = None
    prompt_tokens: int = 100
    completion_tokens: int = 20


@register_llm_provider("fake")
class FakeLLMProvider(ModelProvider):
    def __init__(self, config: LLMProviderConfig | None = None) -> None:
        self.config = config or LLMProviderConfig(name="fake", type="fake")
        self.name = self.config.name
        self.privacy = Privacy.LOCAL
        self._script: deque[ScriptedResponse] = deque()
        #: Every call made, for assertions.
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    # -- test helpers --------------------------------------------------------

    def script(self, *responses: ScriptedResponse | str) -> FakeLLMProvider:
        for response in responses:
            self._script.append(
                ScriptedResponse(text=response) if isinstance(response, str) else response
            )
        return self

    def reset(self) -> None:
        self._script.clear()
        self.calls.clear()

    def _next(self, messages: Sequence[Message]) -> ScriptedResponse:
        if self._script:
            return self._script.popleft()
        last_user = next(
            (m.content for m in reversed(list(messages)) if m.role == "user" and m.content),
            "",
        )
        return ScriptedResponse(text=f"[fake answer] {last_user[:200]}")

    # -- interface -----------------------------------------------------------

    def capabilities(self, model: str) -> ModelInfo:
        cfg = self.config.model_by_name(model) or ModelConfig(
            name=model,
            supports_tools=True,
            supports_json_schema=True,
            tier=ModelTier.BALANCED,
            context_window=32_000,
        )
        return ModelInfo(
            name=cfg.name,
            provider=self.name,
            context_window=cfg.context_window,
            max_output_tokens=cfg.max_output_tokens,
            supports_tools=cfg.supports_tools,
            supports_json_schema=cfg.supports_json_schema,
            supports_streaming=True,
            tier=cfg.tier,
            privacy=self.privacy,
        )

    async def list_models(self) -> Sequence[ModelInfo]:
        names = [m.name for m in self.config.models] or ["fake-model"]
        return [self.capabilities(n) for n in names]

    async def generate(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        params: GenerationParams = GenerationParams(),
        trace: Any = None,
    ) -> Completion:
        self.calls.append(
            {
                "op": "generate",
                "model": model,
                "messages": len(messages),
                "tools": [t.name for t in tools or []],
            }
        )
        response = self._next(messages)
        return Completion(
            text=response.text,
            tool_calls=response.tool_calls,
            finish_reason="tool_calls" if response.tool_calls else "stop",
            model=model,
            provider=self.name,
            usage=Usage(response.prompt_tokens, response.completion_tokens),
            latency_ms=1.0,
        )

    async def generate_stream(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        params: GenerationParams = GenerationParams(),
        trace: Any = None,
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append({"op": "generate_stream", "model": model})
        response = self._next(messages)
        # Word-at-a-time so tests can assert on incremental delivery.
        for word in response.text.split(" "):
            yield TextDelta(word + " ")
        for call in response.tool_calls:
            yield ToolCallComplete(call)
        yield UsageEvent(Usage(response.prompt_tokens, response.completion_tokens))
        yield StreamDone("tool_calls" if response.tool_calls else "stop")

    async def _generate_json(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        json_schema: dict[str, Any],
        params: GenerationParams,
        trace: Any,
    ) -> Completion:
        self.calls.append({"op": "generate_structured", "model": model})
        response = self._next(messages)
        text = json.dumps(response.structured) if response.structured is not None else response.text
        return Completion(
            text=text,
            model=model,
            provider=self.name,
            usage=Usage(response.prompt_tokens, response.completion_tokens),
            latency_ms=1.0,
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(name=self.name, ok=True, latency_ms=0.0)

    async def aclose(self) -> None:
        self.closed = True
