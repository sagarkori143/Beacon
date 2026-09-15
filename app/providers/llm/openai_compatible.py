"""OpenAI-compatible Chat Completions provider.

One class, many vendors. Anything that speaks the ``/chat/completions`` shape
works here with nothing but a ``base_url`` and a key: vLLM, Groq, Together,
Fireworks, OpenRouter, DeepSeek, Mistral, LM Studio, llama.cpp's server, and
OpenAI itself (see :mod:`app.providers.llm.openai`).

This is the main reason the provider layer is a registry rather than a fixed
set: supporting a new vendor of this family is a manifest entry, not code.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from app.core.config import LLMProviderConfig, ModelConfig
from app.core.errors import ProviderError, ProviderTimeout
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

_FINISH_MAP: dict[str, FinishReason] = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",
    "content_filter": "content_filter",
}


@register_llm_provider("openai_compatible")
class OpenAICompatibleProvider(HTTPProviderBase, ModelProvider):
    #: Overridden by subclasses that have a canonical endpoint.
    default_base_url: str = ""
    #: Some gateways reject unknown fields; subclasses can opt out of extras.
    supports_stream_usage: bool = True

    def __init__(self, config: LLMProviderConfig) -> None:
        base_url = config.base_url or self.default_base_url
        if not base_url:
            raise ProviderError(
                f"Provider '{config.name}' of type '{config.type}' requires base_url",
                provider=config.name,
            )

        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        headers.update({k: str(v) for k, v in (config.options.get("headers") or {}).items()})

        HTTPProviderBase.__init__(
            self,
            name=config.name,
            base_url=base_url,
            timeout_s=config.timeout_s,
            connect_timeout_s=config.connect_timeout_s,
            max_concurrency=config.max_concurrency,
            max_retries=config.max_retries,
            headers=headers,
        )
        self.config = config
        self.privacy = config.privacy
        self._models = {m.name: m for m in config.models}

    # -- capabilities --------------------------------------------------------

    def capabilities(self, model: str) -> ModelInfo:
        cfg = self.config.model_by_name(model) or ModelConfig(name=model)
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
            lambda: self._post_json("/chat/completions", payload),
            trace=trace,
            operation="generate",
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

        # Tool calls arrive as fragments keyed by index; accumulate and emit a
        # complete call only once the stream closes.
        partial: dict[int, dict[str, Any]] = {}
        finish: FinishReason = "stop"

        try:
            async with (
                self._semaphore,
                self._client.stream("POST", "/chat/completions", json=payload) as response,
            ):
                if response.status_code >= 400:
                    await response.aread()
                    raise self._error_from_response(response)

                async for raw in response.aiter_lines():
                    line = raw.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    if usage := chunk.get("usage"):
                        yield UsageEvent(self._usage_from(usage))

                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if content := delta.get("content"):
                            yield TextDelta(content)

                        for frag in delta.get("tool_calls") or []:
                            index = int(frag.get("index", 0))
                            slot = partial.setdefault(
                                index, {"id": None, "name": None, "arguments": ""}
                            )
                            fn = frag.get("function") or {}
                            if frag.get("id"):
                                slot["id"] = frag["id"]
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                            args_delta = fn.get("arguments") or ""
                            slot["arguments"] += args_delta
                            yield ToolCallDelta(
                                index=index,
                                id=slot["id"],
                                name=slot["name"],
                                arguments_delta=args_delta,
                            )

                        if reason := choice.get("finish_reason"):
                            finish = _FINISH_MAP.get(reason, "stop")

            for index in sorted(partial):
                if call := self._materialize_tool_call(index, partial[index]):
                    yield ToolCallComplete(call)

            self.circuit.record_success()
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
        """Use ``response_format`` when the model declares JSON-schema support."""
        if not self.capabilities(model).supports_json_schema:
            return await super()._generate_json(
                model=model,
                messages=messages,
                json_schema=json_schema,
                params=params,
                trace=trace,
            )

        payload = self._payload(model, messages, None, params.with_(temperature=0.0), stream=False)
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": json_schema.get("title", "Response"),
                "schema": _strictify(json_schema),
                "strict": True,
            },
        }
        started = time.perf_counter()
        body = await self._call(
            lambda: self._post_json("/chat/completions", payload),
            trace=trace,
            operation="generate_structured",
        )
        return self._completion_from_body(
            body, model=model, latency_ms=(time.perf_counter() - started) * 1000.0
        )

    # -- health --------------------------------------------------------------

    async def health(self) -> ProviderHealth:
        started = time.perf_counter()
        try:
            await self._get_json("/models")
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
        payload: dict[str, Any] = {
            "model": model,
            "messages": [self._encode_message(m) for m in messages],
            "temperature": params.temperature,
            "top_p": params.top_p,
            "stream": stream,
        }
        if params.max_tokens is not None:
            payload["max_tokens"] = params.max_tokens
        if params.stop:
            payload["stop"] = list(params.stop)
        if params.seed is not None:
            payload["seed"] = params.seed
        if stream and self.supports_stream_usage:
            payload["stream_options"] = {"include_usage": True}
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ]
            payload["tool_choice"] = "auto"
        payload.update(self.config.options.get("extra_body") or {})
        return payload

    @staticmethod
    def _encode_message(message: Message) -> dict[str, Any]:
        if message.role == "tool":
            return {
                "role": "tool",
                "tool_call_id": message.tool_call_id or "",
                "content": message.content or "",
            }

        encoded: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.tool_calls:
            encoded["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": json.dumps(dict(c.arguments))},
                }
                for c in message.tool_calls
            ]
            # The API rejects a null content alongside tool_calls on some gateways.
            encoded["content"] = message.content or ""
        return encoded

    @staticmethod
    def _materialize_tool_call(index: int, slot: dict[str, Any]) -> ToolCall | None:
        if not slot.get("name"):
            return None
        try:
            arguments = json.loads(slot["arguments"] or "{}")
        except json.JSONDecodeError:
            arguments = {}
        return ToolCall(
            id=slot.get("id") or f"call_{index}",
            name=slot["name"],
            arguments=arguments if isinstance(arguments, dict) else {},
        )

    @staticmethod
    def _usage_from(usage: dict[str, Any]) -> Usage:
        return Usage(
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )

    def _completion_from_body(
        self, body: dict[str, Any], *, model: str, latency_ms: float
    ) -> Completion:
        choices = body.get("choices") or [{}]
        choice = choices[0]
        message = choice.get("message") or {}

        tool_calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            fn = raw.get("function") or {}
            if not fn.get("name"):
                continue
            try:
                arguments = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {}
            tool_calls.append(
                ToolCall(
                    id=raw.get("id") or f"call_{len(tool_calls)}",
                    name=fn["name"],
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
            )

        return Completion(
            text=message.get("content") or "",
            tool_calls=tuple(tool_calls),
            finish_reason=_FINISH_MAP.get(choice.get("finish_reason") or "stop", "stop"),
            model=body.get("model") or model,
            provider=self.name,
            usage=self._usage_from(body.get("usage") or {}),
            latency_ms=latency_ms,
        )


def _strictify(schema: dict[str, Any]) -> dict[str, Any]:
    """Make a Pydantic JSON Schema acceptable to strict structured-output modes.

    Strict mode requires ``additionalProperties: false`` on every object and
    every property listed as required. Pydantic emits neither for optional
    fields, so we add them; optional-ness is still expressed by the field's
    ``null`` union, which strict mode does allow.
    """
    if not isinstance(schema, dict):
        return schema

    out = dict(schema)
    if out.get("type") == "object":
        out["additionalProperties"] = False
        properties = out.get("properties")
        if isinstance(properties, dict):
            out["properties"] = {k: _strictify(v) for k, v in properties.items()}
            out["required"] = list(properties)
    for key in ("items", "additionalItems"):
        if key in out:
            out[key] = _strictify(out[key])
    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        if isinstance(out.get(key), list):
            out[key] = [_strictify(v) for v in out[key]]
    if isinstance(out.get("$defs"), dict):
        out["$defs"] = {k: _strictify(v) for k, v in out["$defs"].items()}
    return out
