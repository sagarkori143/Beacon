"""Google Gemini provider (Generative Language API).

Gemini's shape differs again: turns are ``contents`` with ``parts``, the
assistant role is called ``model``, tool calls are ``functionCall`` parts and
tool results are ``functionResponse`` parts. All of that is contained here.

Structured output uses ``responseSchema``, which constrains decoding natively.
"""

from __future__ import annotations

import json
import time
import uuid
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
    ToolSpec,
    Usage,
    UsageEvent,
    model_info_from_config,
)
from app.providers.llm.registry import register_llm_provider

log = get_logger(__name__)

_FINISH_MAP: dict[str, FinishReason] = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
}

#: JSON Schema keywords the Gemini schema dialect rejects.
_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {"$schema", "$defs", "$ref", "additionalProperties", "title", "default", "examples"}
)


@register_llm_provider("gemini")
class GeminiProvider(HTTPProviderBase, ModelProvider):
    default_base_url = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, config: LLMProviderConfig) -> None:
        if not config.api_key:
            raise ConfigurationError(
                f"Provider '{config.name}' (gemini) requires an API key; "
                "set GEMINI_API_KEY or remove the entry from the manifest"
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
                "content-type": "application/json",
                "x-goog-api-key": config.api_key,
            },
        )
        self.config = config
        self.privacy = config.privacy
        self._models = {m.name: m for m in config.models}

    # -- capabilities --------------------------------------------------------

    def capabilities(self, model: str) -> ModelInfo:
        cfg = self.config.model_by_name(model) or ModelConfig(
            name=model, supports_tools=True, supports_json_schema=True, context_window=1_000_000
        )
        return model_info_from_config(cfg, provider=self.name, privacy=self.privacy)

    async def list_models(self) -> Sequence[ModelInfo]:
        if self._models:
            return [self.capabilities(name) for name in self._models]
        try:
            body = await self._call(lambda: self._get_json("/models"), operation="list_models")
        except ProviderError:
            return []
        return [
            self.capabilities(item["name"].removeprefix("models/"))
            for item in body.get("models", [])
            if item.get("name")
        ]

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
        payload = self._payload(messages, tools, params)
        started = time.perf_counter()
        body = await self._call(
            lambda: self._post_json(f"/models/{model}:generateContent", payload),
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
        payload = self._payload(messages, tools, params)
        self.circuit.check()

        usage = Usage()
        finish: FinishReason = "stop"
        pending_calls: list[ToolCall] = []

        try:
            async with (
                self._semaphore,
                self._client.stream(
                    "POST",
                    f"/models/{model}:streamGenerateContent",
                    json=payload,
                    params={"alt": "sse"},
                ) as response,
            ):
                if response.status_code >= 400:
                    await response.aread()
                    raise self._error_from_response(response)

                async for raw in response.aiter_lines():
                    line = raw.strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        chunk = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue

                    for candidate in chunk.get("candidates") or []:
                        for part in (candidate.get("content") or {}).get("parts") or []:
                            if text := part.get("text"):
                                yield TextDelta(text)
                            if call := part.get("functionCall"):
                                # Gemini emits whole function calls, never fragments.
                                pending_calls.append(self._tool_call_from(call))
                        if reason := candidate.get("finishReason"):
                            finish = _FINISH_MAP.get(reason, "stop")

                    if meta := chunk.get("usageMetadata"):
                        usage = self._usage_from(meta)

            for call in pending_calls:
                yield ToolCallComplete(call)

            self.circuit.record_success()
            yield UsageEvent(usage)
            yield StreamDone("tool_calls" if pending_calls else finish)
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
        if not self.capabilities(model).supports_json_schema:
            return await super()._generate_json(
                model=model,
                messages=messages,
                json_schema=json_schema,
                params=params,
                trace=trace,
            )

        payload = self._payload(messages, None, params.with_(temperature=0.0))
        payload["generationConfig"]["responseMimeType"] = "application/json"
        payload["generationConfig"]["responseSchema"] = _to_gemini_schema(json_schema)

        started = time.perf_counter()
        body = await self._call(
            lambda: self._post_json(f"/models/{model}:generateContent", payload),
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
            await self._get_json("/models", params={"pageSize": 1})
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
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None,
        params: GenerationParams,
    ) -> dict[str, Any]:
        system_parts: list[str] = []
        contents: list[dict[str, Any]] = []

        for message in messages:
            if message.role == "system":
                if message.content:
                    system_parts.append(message.content)
                continue

            if message.role == "tool":
                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            {
                                "functionResponse": {
                                    "name": message.name or "",
                                    "response": {"result": message.content or ""},
                                }
                            }
                        ],
                    }
                )
                continue

            parts: list[dict[str, Any]] = []
            if message.content:
                parts.append({"text": message.content})
            parts.extend(
                {"functionCall": {"name": c.name, "args": dict(c.arguments)}}
                for c in message.tool_calls
            )
            contents.append(
                {
                    "role": "model" if message.role == "assistant" else "user",
                    "parts": parts or [{"text": ""}],
                }
            )

        generation_config: dict[str, Any] = {
            "temperature": params.temperature,
            "topP": params.top_p,
        }
        if params.max_tokens is not None:
            generation_config["maxOutputTokens"] = params.max_tokens
        if params.stop:
            generation_config["stopSequences"] = list(params.stop)

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": generation_config,
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        if tools:
            payload["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "parameters": _to_gemini_schema(t.input_schema),
                        }
                        for t in tools
                    ]
                }
            ]
        payload.update(self.config.options.get("extra_body") or {})
        return payload

    @staticmethod
    def _tool_call_from(call: dict[str, Any]) -> ToolCall:
        args = call.get("args")
        return ToolCall(
            id=f"call_{uuid.uuid4().hex[:12]}",
            name=call.get("name", ""),
            arguments=args if isinstance(args, dict) else {},
        )

    @staticmethod
    def _usage_from(meta: dict[str, Any]) -> Usage:
        return Usage(
            prompt_tokens=int(meta.get("promptTokenCount") or 0),
            completion_tokens=int(meta.get("candidatesTokenCount") or 0),
        )

    def _completion_from_body(
        self, body: dict[str, Any], *, model: str, latency_ms: float
    ) -> Completion:
        candidates = body.get("candidates") or [{}]
        candidate = candidates[0]

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for part in (candidate.get("content") or {}).get("parts") or []:
            if text := part.get("text"):
                text_parts.append(text)
            if call := part.get("functionCall"):
                tool_calls.append(self._tool_call_from(call))

        return Completion(
            text="".join(text_parts),
            tool_calls=tuple(tool_calls),
            finish_reason=(
                "tool_calls"
                if tool_calls
                else _FINISH_MAP.get(candidate.get("finishReason") or "STOP", "stop")
            ),
            model=model,
            provider=self.name,
            usage=self._usage_from(body.get("usageMetadata") or {}),
            latency_ms=latency_ms,
        )


def _to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Translate a Pydantic JSON Schema into Gemini's narrower dialect.

    Gemini accepts a subset of JSON Schema and rejects the request outright on
    unknown keywords, so ``$defs``/``$ref`` are inlined and unsupported keys
    dropped. Types are upper-cased, which is what its schema dialect expects.
    """
    defs = schema.get("$defs") or {}

    def convert(node: Any) -> Any:
        if isinstance(node, list):
            return [convert(v) for v in node]
        if not isinstance(node, dict):
            return node

        if ref := node.get("$ref"):
            target = defs.get(str(ref).rsplit("/", 1)[-1], {})
            merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
            return convert(merged)

        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in _UNSUPPORTED_SCHEMA_KEYS:
                continue
            if key == "type" and isinstance(value, str):
                out["type"] = value.upper()
            elif key == "properties" and isinstance(value, dict):
                out["properties"] = {k: convert(v) for k, v in value.items()}
            elif key in ("items", "anyOf", "oneOf", "allOf"):
                out[key] = convert(value)
            else:
                out[key] = value
        return out

    converted = convert({k: v for k, v in schema.items() if k != "$defs"})
    return converted if isinstance(converted, dict) else {}
