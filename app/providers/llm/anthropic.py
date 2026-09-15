"""Anthropic provider (Messages API).

Anthropic's wire format differs from the Chat Completions family in three ways
that all live inside this file: system prompts are a top-level field rather than
a message, assistant output is a list of content blocks, and tool results are
sent back as ``user`` messages containing ``tool_result`` blocks.

Structured output uses forced tool use. Anthropic has no ``response_format``
equivalent, but constraining the model to a single tool whose ``input_schema``
is the target schema gives the same guarantee and is far more reliable than
asking for JSON in the prompt.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from app.core.config import LLMProviderConfig, ModelConfig
from app.core.errors import ConfigurationError, ProviderError, ProviderTimeout
from app.core.logging import get_logger
from app.core.tracing import TraceContext
from app.providers.base import HTTPProviderBase, ProviderHealth
from app.providers.llm.base import (
    Completion,
    FinishReason,
    GenerationParams,
    Message,
    ModelInfo,
    ModelProvider,
    StreamDone,
    StreamError,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallComplete,
    ToolCallDelta,
    ToolSpec,
    Usage,
    UsageEvent,
    model_info_from_config,
)
from app.providers.llm.registry import register_llm_provider

log = get_logger(__name__)

_STOP_MAP: dict[str, FinishReason] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
}

#: Anthropic requires max_tokens. Used when the caller did not specify one.
_DEFAULT_MAX_TOKENS = 2048

_STRUCTURED_TOOL_NAME = "emit_result"


@register_llm_provider("anthropic")
class AnthropicProvider(HTTPProviderBase, ModelProvider):
    default_base_url = "https://api.anthropic.com/v1"

    def __init__(self, config: LLMProviderConfig) -> None:
        if not config.api_key:
            raise ConfigurationError(
                f"Provider '{config.name}' (anthropic) requires an API key; "
                "set ANTHROPIC_API_KEY or remove the entry from the manifest"
            )

        HTTPProviderBase.__init__(
            self,
            name=config.name,
            base_url=config.base_url or self.default_base_url,
            timeout_s=config.timeout_s,
            connect_timeout_s=config.connect_timeout_s,
            max_concurrency=config.max_concurrency,
            max_retries=config.max_retries,
            headers={
                "x-api-key": config.api_key,
                "anthropic-version": str(config.options.get("api_version", "2023-06-01")),
                "content-type": "application/json",
            },
        )
        self.config = config
        self.privacy = config.privacy
        self._models = {m.name: m for m in config.models}

    # -- capabilities --------------------------------------------------------

    def capabilities(self, model: str) -> ModelInfo:
        cfg = self.config.model_by_name(model) or ModelConfig(
            name=model, supports_tools=True, supports_json_schema=True, context_window=200_000
        )
        return model_info_from_config(cfg, provider=self.name, privacy=self.privacy)

    async def list_models(self) -> Sequence[ModelInfo]:
        if self._models:
            return [self.capabilities(name) for name in self._models]
        try:
            body = await self._call(lambda: self._get_json("/models"), operation="list_models")
        except ProviderError:
            return []
        return [self.capabilities(item["id"]) for item in body.get("data", []) if item.get("id")]

    # -- generation ----------------------------------------------------------

    async def generate(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        params: GenerationParams = GenerationParams(),
        trace: TraceContext | None = None,
    ) -> Completion:
        payload = self._payload(model, messages, tools, params, stream=False)
        started = time.perf_counter()
        body = await self._call(
            lambda: self._post_json("/messages", payload), trace=trace, operation="generate"
        )
        return self._completion_from_body(
            body, model=model, latency_ms=(time.perf_counter() - started) * 1000.0
        )

    async def generate_stream(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        params: GenerationParams = GenerationParams(),
        trace: TraceContext | None = None,
    ) -> AsyncIterator[StreamEvent]:
        payload = self._payload(model, messages, tools, params, stream=True)
        self.circuit.check()

        blocks: dict[int, dict[str, Any]] = {}
        usage = Usage()
        finish: FinishReason = "stop"

        try:
            async with (
                self._semaphore,
                self._client.stream("POST", "/messages", json=payload) as response,
            ):
                if response.status_code >= 400:
                    await response.aread()
                    raise self._error_from_response(response)

                async for raw in response.aiter_lines():
                    line = raw.strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue

                    etype = event.get("type")

                    if etype == "message_start":
                        u = (event.get("message") or {}).get("usage") or {}
                        usage = Usage(prompt_tokens=int(u.get("input_tokens") or 0))

                    elif etype == "content_block_start":
                        index = int(event.get("index", 0))
                        block = event.get("content_block") or {}
                        if block.get("type") == "tool_use":
                            blocks[index] = {
                                "id": block.get("id"),
                                "name": block.get("name"),
                                "arguments": "",
                            }

                    elif etype == "content_block_delta":
                        index = int(event.get("index", 0))
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            yield TextDelta(delta.get("text", ""))
                        elif delta.get("type") == "input_json_delta":
                            slot = blocks.setdefault(
                                index, {"id": None, "name": None, "arguments": ""}
                            )
                            fragment = delta.get("partial_json", "")
                            slot["arguments"] += fragment
                            yield ToolCallDelta(
                                index=index,
                                id=slot["id"],
                                name=slot["name"],
                                arguments_delta=fragment,
                            )

                    elif etype == "message_delta":
                        d = event.get("delta") or {}
                        if stop := d.get("stop_reason"):
                            finish = _STOP_MAP.get(stop, "stop")
                        u = event.get("usage") or {}
                        if "output_tokens" in u:
                            usage = Usage(
                                prompt_tokens=usage.prompt_tokens,
                                completion_tokens=int(u["output_tokens"]),
                            )

                    elif etype == "error":
                        err = event.get("error") or {}
                        yield StreamError(
                            code="provider_error",
                            message=str(err.get("message") or err),
                            retryable=err.get("type") in ("overloaded_error", "api_error"),
                        )
                        return

            for index in sorted(blocks):
                slot = blocks[index]
                if not slot.get("name"):
                    continue
                try:
                    arguments = json.loads(slot["arguments"] or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                yield ToolCallComplete(
                    ToolCall(
                        id=slot.get("id") or f"call_{index}",
                        name=slot["name"],
                        arguments=arguments if isinstance(arguments, dict) else {},
                    )
                )

            self.circuit.record_success()
            yield UsageEvent(usage)
            yield StreamDone(finish)
        except ProviderError as exc:
            self.circuit.record_failure()
            yield StreamError(code=exc.code, message=str(exc), retryable=exc.retryable)
        except httpx.TimeoutException:
            self.circuit.record_failure()
            yield StreamError(
                code="provider_timeout", message=f"{self.name}: stream timed out", retryable=True
            )
        except httpx.HTTPError as exc:
            self.circuit.record_failure()
            yield StreamError(code="provider_error", message=str(exc), retryable=True)

    async def _generate_json(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        json_schema: dict[str, Any],
        params: GenerationParams,
        trace: TraceContext | None,
    ) -> Completion:
        """Force a single tool whose input schema is the target schema.

        The model then cannot emit anything but a conforming object, which is a
        stronger guarantee than prompt-level instruction. The tool input is
        returned as the completion text so the base class can validate it
        through exactly the same path as every other provider.
        """
        tool = ToolSpec(
            name=_STRUCTURED_TOOL_NAME,
            description="Return the result. Call this exactly once.",
            input_schema=json_schema,
        )
        payload = self._payload(
            model, messages, [tool], params.with_(temperature=0.0), stream=False
        )
        payload["tool_choice"] = {"type": "tool", "name": _STRUCTURED_TOOL_NAME}

        started = time.perf_counter()
        body = await self._call(
            lambda: self._post_json("/messages", payload),
            trace=trace,
            operation="generate_structured",
        )
        completion = self._completion_from_body(
            body, model=model, latency_ms=(time.perf_counter() - started) * 1000.0
        )

        for call in completion.tool_calls:
            if call.name == _STRUCTURED_TOOL_NAME:
                return Completion(
                    text=json.dumps(dict(call.arguments)),
                    finish_reason="stop",
                    model=completion.model,
                    provider=self.name,
                    usage=completion.usage,
                    latency_ms=completion.latency_ms,
                )
        return completion

    # -- health --------------------------------------------------------------

    async def health(self) -> ProviderHealth:
        started = time.perf_counter()
        try:
            await self._get_json("/models", params={"limit": 1})
        except ProviderTimeout:
            return ProviderHealth(self.name, ok=False, detail="timeout")
        except Exception as exc:  # noqa: BLE001 - health must never raise
            return ProviderHealth(self.name, ok=False, detail=str(exc)[:200])
        return ProviderHealth(
            name=self.name,
            ok=True,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            extra={"circuit": self.circuit.state.value},
        )

    async def aclose(self) -> None:
        await HTTPProviderBase.aclose(self)

    # -- wire format ---------------------------------------------------------

    def _payload(
        self,
        model: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None,
        params: GenerationParams,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        system_parts, conversation = self._split_system(messages)

        payload: dict[str, Any] = {
            "model": model,
            "messages": conversation,
            "max_tokens": params.max_tokens or _DEFAULT_MAX_TOKENS,
            "temperature": params.temperature,
            "stream": stream,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if params.top_p != 1.0:
            payload["top_p"] = params.top_p
        if params.stop:
            payload["stop_sequences"] = list(params.stop)
        if tools:
            payload["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in tools
            ]
        payload.update(self.config.options.get("extra_body") or {})
        return payload

    @staticmethod
    def _split_system(messages: Sequence[Message]) -> tuple[list[str], list[dict[str, Any]]]:
        """Lift system messages out and fold tool results into user turns.

        Anthropic requires alternating user/assistant turns, and a tool result is
        a ``user`` message. Consecutive tool results are merged into one turn so
        parallel tool calls do not produce an illegal run of user messages.
        """
        system: list[str] = []
        out: list[dict[str, Any]] = []

        for message in messages:
            if message.role == "system":
                if message.content:
                    system.append(message.content)
                continue

            if message.role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id or "",
                    "content": message.content or "",
                }
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
                continue

            if message.role == "assistant" and message.tool_calls:
                content: list[dict[str, Any]] = []
                if message.content:
                    content.append({"type": "text", "text": message.content})
                content.extend(
                    {
                        "type": "tool_use",
                        "id": c.id,
                        "name": c.name,
                        "input": dict(c.arguments),
                    }
                    for c in message.tool_calls
                )
                out.append({"role": "assistant", "content": content})
                continue

            out.append({"role": message.role, "content": message.content or ""})

        return system, out

    def _completion_from_body(
        self, body: dict[str, Any], *, model: str, latency_ms: float
    ) -> Completion:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for block in body.get("content") or []:
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                raw_input = block.get("input")
                tool_calls.append(
                    ToolCall(
                        id=block.get("id") or f"call_{len(tool_calls)}",
                        name=block.get("name", ""),
                        arguments=raw_input if isinstance(raw_input, dict) else {},
                    )
                )

        usage_body = body.get("usage") or {}
        return Completion(
            text="".join(text_parts),
            tool_calls=tuple(tool_calls),
            finish_reason=_STOP_MAP.get(body.get("stop_reason") or "end_turn", "stop"),
            model=body.get("model") or model,
            provider=self.name,
            usage=Usage(
                prompt_tokens=int(usage_body.get("input_tokens") or 0),
                completion_tokens=int(usage_body.get("output_tokens") or 0),
            ),
            latency_ms=latency_ms,
        )
