"""The LLM provider abstraction.

Everything above this module -- the agent, RAG, the router, the tools -- depends
only on the types declared here. Vendor wire formats (Anthropic content blocks,
OpenAI tool-call deltas, Ollama's NDJSON) never escape their own provider file.

Adding a vendor means writing one file that implements :class:`ModelProvider`
and registering it with ``@register_llm_provider("<type>")``. Nothing else in
the codebase changes.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, Literal, NamedTuple, TypeVar

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from app.core.config import ModelConfig
from app.core.enums import ModelTier, Privacy
from app.core.errors import StructuredOutputError
from app.core.logging import get_logger
from app.core.tracing import TraceContext
from app.providers.base import ProviderHealth

log = get_logger(__name__)

TModel = TypeVar("TModel", bound=BaseModel)

Role = Literal["system", "user", "assistant", "tool"]
FinishReason = Literal["stop", "length", "tool_calls", "content_filter", "timeout", "error"]


# ---------------------------------------------------------------------------
# Message and tool types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A model's request to invoke a tool. Arguments are already parsed JSON."""

    id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool as presented to the model.

    ``input_schema`` is JSON Schema. It is produced from the tool's Pydantic
    input model, so the schema the model sees and the validation applied to what
    it returns cannot drift apart.
    """

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None

    @classmethod
    def system(cls, content: str) -> Message:
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> Message:
        return cls(role="user", content=content)

    @classmethod
    def assistant(
        cls, content: str | None = None, *, tool_calls: Sequence[ToolCall] = ()
    ) -> Message:
        return cls(role="assistant", content=content, tool_calls=tuple(tool_calls))

    @classmethod
    def tool_result(cls, tool_call_id: str, name: str, content: str) -> Message:
        return cls(role="tool", content=content, tool_call_id=tool_call_id, name=name)


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
        )

    def cost_usd(self, model: ModelConfig | None) -> float:
        if model is None:
            return 0.0
        return (
            self.prompt_tokens * model.cost_per_1m_input
            + self.completion_tokens * model.cost_per_1m_output
        ) / 1_000_000


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """Capabilities of one model, as declared in configuration or discovered.

    The router reads these attributes and never a provider name, which is what
    makes a newly registered vendor immediately routable.
    """

    name: str
    provider: str
    context_window: int = 8192
    max_output_tokens: int = 2048
    supports_tools: bool = False
    supports_json_schema: bool = False
    supports_streaming: bool = True
    tier: ModelTier = ModelTier.BALANCED
    privacy: Privacy = Privacy.CLOUD
    cost_per_1m_input: float = 0.0
    cost_per_1m_output: float = 0.0

    @property
    def qualified_name(self) -> str:
        return f"{self.provider}/{self.name}"


@dataclass(frozen=True, slots=True)
class GenerationParams:
    temperature: float = 0.2
    top_p: float = 0.95
    max_tokens: int | None = None
    stop: tuple[str, ...] = ()
    seed: int | None = None
    #: Passed through to providers that keep models resident (Ollama). Ignored
    #: elsewhere. Avoids a cold model load on every request.
    keep_alive: str | None = "30m"

    def with_(self, **overrides: Any) -> GenerationParams:
        data = {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "stop": self.stop,
            "seed": self.seed,
            "keep_alive": self.keep_alive,
        }
        data.update(overrides)
        return GenerationParams(**data)


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: FinishReason = "stop"
    model: str = ""
    provider: str = ""
    usage: Usage = field(default_factory=Usage)
    latency_ms: float = 0.0

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


@dataclass(frozen=True, slots=True)
class StructuredCompletion(Generic[TModel]):
    value: TModel
    completion: Completion
    repairs: int = 0


# ---------------------------------------------------------------------------
# Stream events
# ---------------------------------------------------------------------------


class TextDelta(NamedTuple):
    text: str


class ToolCallDelta(NamedTuple):
    """Partial tool call. Kept in the union so a vendor that streams tool calls
    reliably can be adopted without changing the protocol, even though the agent
    currently runs tool turns non-streaming."""

    index: int
    id: str | None
    name: str | None
    arguments_delta: str


class ToolCallComplete(NamedTuple):
    call: ToolCall


class UsageEvent(NamedTuple):
    usage: Usage


class StreamError(NamedTuple):
    code: str
    message: str
    retryable: bool


class StreamDone(NamedTuple):
    finish_reason: FinishReason


StreamEvent = TextDelta | ToolCallDelta | ToolCallComplete | UsageEvent | StreamError | StreamDone


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------


class ModelProvider(ABC):
    """One LLM vendor or endpoint."""

    name: str
    privacy: Privacy

    @abstractmethod
    async def list_models(self) -> Sequence[ModelInfo]:
        """Models this provider can serve, with their declared capabilities."""

    @abstractmethod
    def capabilities(self, model: str) -> ModelInfo:
        """Capabilities of one model. Must not perform I/O."""

    def supports_tools(self, model: str) -> bool:
        return self.capabilities(model).supports_tools

    @abstractmethod
    async def generate(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        params: GenerationParams = GenerationParams(),
        trace: TraceContext | None = None,
    ) -> Completion: ...

    @abstractmethod
    def generate_stream(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        params: GenerationParams = GenerationParams(),
        trace: TraceContext | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Note the signature: a plain ``def`` returning an async iterator, not
        an ``async def``. An ``async def`` generator function satisfies this, and
        it means callers write ``async for`` without an extra ``await``."""

    @abstractmethod
    async def health(self) -> ProviderHealth: ...

    @abstractmethod
    async def aclose(self) -> None: ...

    # -- structured output ---------------------------------------------------

    async def generate_structured(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        schema: type[TModel],
        params: GenerationParams = GenerationParams(),
        max_repairs: int = 2,
        trace: TraceContext | None = None,
    ) -> StructuredCompletion[TModel]:
        """Produce a schema-valid object, uniformly across vendors.

        Providers whose API supports native JSON-schema constraint override
        ``_generate_json`` to use it. Everyone else falls back to instructing the
        model in the prompt. Either way the result is validated against the
        Pydantic model here, and a failure is fed back to the model as a repair
        turn rather than raised -- models correct their own JSON reliably when
        shown the validation error.
        """
        json_schema = schema.model_json_schema()
        convo = list(messages)
        repairs = 0
        last_error = ""

        for attempt in range(max_repairs + 1):
            completion = await self._generate_json(
                model=model,
                messages=convo,
                json_schema=json_schema,
                params=params,
                trace=trace,
            )
            raw = _extract_json_object(completion.text)
            if raw is not None:
                try:
                    value = schema.model_validate(raw)
                except PydanticValidationError as exc:
                    last_error = _format_validation_errors(exc)
                else:
                    return StructuredCompletion(value=value, completion=completion, repairs=repairs)
            else:
                last_error = "Response did not contain a JSON object."

            if attempt == max_repairs:
                break

            repairs += 1  # noqa: SIM113 - counts repairs performed, not loop index
            convo = [
                *convo,
                Message.assistant(completion.text),
                Message.user(
                    "That response was not valid for the required schema.\n"
                    f"{last_error}\n"
                    "Reply with ONLY the corrected JSON object."
                ),
            ]

        raise StructuredOutputError(
            f"{self.name}/{model}: could not produce schema-valid output for "
            f"{schema.__name__} after {max_repairs} repair attempt(s): {last_error}",
            provider=self.name,
        )

    async def _generate_json(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        json_schema: dict[str, Any],
        params: GenerationParams,
        trace: TraceContext | None,
    ) -> Completion:
        """Default: instruct the model in the prompt.

        Overridden by providers that can constrain decoding natively.
        """
        instruction = (
            "You must reply with a single JSON object and nothing else -- no "
            "prose, no markdown fences. It must validate against this JSON Schema:\n"
            f"{json.dumps(json_schema, separators=(',', ':'))}"
        )
        augmented = _append_system(messages, instruction)
        return await self.generate(
            model=model,
            messages=augmented,
            params=params.with_(temperature=0.0),
            trace=trace,
        )

    async def count_tokens(self, *, model: str, messages: Sequence[Message]) -> int:
        """Approximate token count. Providers with an exact endpoint override this."""
        from app.services.rag.token_budget import count_message_tokens

        return count_message_tokens(messages)


# ---------------------------------------------------------------------------
# Helpers shared by providers
# ---------------------------------------------------------------------------


def _append_system(messages: Sequence[Message], text: str) -> list[Message]:
    """Attach an instruction to the system message, or prepend one."""
    result = list(messages)
    for i, msg in enumerate(result):
        if msg.role == "system":
            result[i] = Message.system(f"{msg.content}\n\n{text}" if msg.content else text)
            return result
    return [Message.system(text), *result]


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Pull a JSON object out of a model response.

    Handles the three things models actually do: emit clean JSON, wrap it in a
    markdown fence, or surround it with a sentence of commentary.
    """
    if not text:
        return None

    candidates: list[str] = []
    stripped = text.strip()
    candidates.append(stripped)

    fence = _FENCE_RE.search(text)
    if fence:
        candidates.append(fence.group(1).strip())

    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        candidates.append(stripped[start : end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _format_validation_errors(exc: PydanticValidationError) -> str:
    lines = []
    for err in exc.errors()[:8]:
        location = ".".join(str(p) for p in err["loc"]) or "<root>"
        lines.append(f"- {location}: {err['msg']}")
    return "Validation errors:\n" + "\n".join(lines)


def model_info_from_config(cfg: ModelConfig, *, provider: str, privacy: Privacy) -> ModelInfo:
    """Build a :class:`ModelInfo` from a manifest entry."""
    return ModelInfo(
        name=cfg.name,
        provider=provider,
        context_window=cfg.context_window,
        max_output_tokens=cfg.max_output_tokens,
        supports_tools=cfg.supports_tools,
        supports_json_schema=cfg.supports_json_schema,
        supports_streaming=cfg.supports_streaming,
        tier=cfg.tier,
        privacy=privacy,
        cost_per_1m_input=cfg.cost_per_1m_input,
        cost_per_1m_output=cfg.cost_per_1m_output,
    )
