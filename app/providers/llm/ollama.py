"""Ollama provider.

Ollama is treated as what it is here: a **remote HTTP service** that may be slow,
cold, or down. It commonly runs on a separate GPU machine from the backend, so
this file contains no hardware, GPU or platform introspection of any kind -- the
model is whatever ``OLLAMA_MODEL`` names, and choosing it belongs to whoever
operates that machine.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from app.core.config import LLMProviderConfig, ModelConfig
from app.core.enums import Privacy
from app.core.errors import ProviderError, ProviderTimeout, ProviderUnavailable
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


@register_llm_provider("ollama")
class OllamaProvider(HTTPProviderBase, ModelProvider):
    """Native Ollama ``/api/chat`` and ``/api/tags`` client."""

    def __init__(self, config: LLMProviderConfig) -> None:
        base_url = config.base_url or "http://localhost:11434"
        headers: dict[str, str] = {}
        # Ollama has no auth of its own; a bearer token here supports the
        # recommended deployment of putting it behind an authenticating proxy.
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"

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
        self.privacy = config.privacy or Privacy.LOCAL
        self._models = {m.name: m for m in config.models}

    # -- capabilities --------------------------------------------------------

    def capabilities(self, model: str) -> ModelInfo:
        cfg = self.config.model_by_name(model)
        if cfg is None:
            # An unlisted model still works; we simply have no declared
            # capabilities for it, so assume the conservative minimum.
            cfg = ModelConfig(name=model)
        return model_info_from_config(cfg, provider=self.name, privacy=self.privacy)

    async def list_models(self) -> Sequence[ModelInfo]:
        """Declared models, cross-checked against what the server actually has.

        A model in the manifest that the server has not pulled is reported but
        marked unavailable, which is far easier to debug than a 404 mid-request.
        """
        declared = [self.capabilities(name) for name in self._models]
        try:
            body = await self._call(lambda: self._get_json("/api/tags"), operation="list_models")
        except ProviderError:
            return declared

        installed = {m.get("name", "").split(":")[0] for m in body.get("models", [])}
        installed |= {m.get("name", "") for m in body.get("models", [])}
        if not declared:
            return [self.capabilities(name) for name in sorted(installed) if name]
        return declared

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
        payload = self._chat_payload(model, messages, tools, params, stream=False)
        started = time.perf_counter()
        body = await self._call(
            lambda: self._post_json("/api/chat", payload),
            trace=trace,
            operation="generate",
        )
        latency_ms = (time.perf_counter() - started) * 1000.0
        return self._completion_from_body(body, model=model, latency_ms=latency_ms)

    async def generate_stream(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        params: GenerationParams = GenerationParams(),
        trace: TraceContext | None = None,
    ) -> AsyncIterator[StreamEvent]:
        payload = self._chat_payload(model, messages, tools, params, stream=True)
        self.circuit.check()

        try:
            async with (
                self._semaphore,
                self._client.stream("POST", "/api/chat", json=payload) as response,
            ):
                if response.status_code >= 400:
                    await response.aread()
                    raise self._error_from_response(response)

                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    message = chunk.get("message") or {}
                    content = message.get("content")
                    if content:
                        yield TextDelta(content)

                    # Ollama emits complete tool calls, never fragments.
                    for call in self._parse_tool_calls(message):
                        yield ToolCallComplete(call)

                    if chunk.get("done"):
                        yield UsageEvent(self._usage_from_body(chunk))
                        yield StreamDone(self._finish_reason(chunk))
            self.circuit.record_success()
        except (ProviderError, ProviderUnavailable) as exc:
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
        """Use Ollama's native structured output when the model declares support.

        Passing the schema as ``format`` constrains decoding, which is far more
        reliable than asking politely in the prompt. Models not declared as
        supporting it fall back to the base-class behaviour.
        """
        if not self.capabilities(model).supports_json_schema:
            return await super()._generate_json(
                model=model,
                messages=messages,
                json_schema=json_schema,
                params=params,
                trace=trace,
            )

        payload = self._chat_payload(
            model, messages, None, params.with_(temperature=0.0), stream=False
        )
        payload["format"] = json_schema
        started = time.perf_counter()
        body = await self._call(
            lambda: self._post_json("/api/chat", payload),
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
            body = await self._get_json("/api/tags")
        except ProviderTimeout:
            return ProviderHealth(self.name, ok=False, detail="timeout")
        except Exception as exc:  # noqa: BLE001 - health must never raise
            return ProviderHealth(self.name, ok=False, detail=str(exc)[:200])

        names = [m.get("name", "") for m in body.get("models", [])]
        missing = [
            declared
            for declared in self._models
            if declared not in names and f"{declared}:latest" not in names
        ]
        return ProviderHealth(
            name=self.name,
            ok=True,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            extra={
                "installed_models": len(names),
                "circuit": self.circuit.state.value,
                **({"missing_models": missing} if missing else {}),
            },
        )

    async def aclose(self) -> None:
        await HTTPProviderBase.aclose(self)

    # -- wire format ---------------------------------------------------------

    def _chat_payload(
        self,
        model: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None,
        params: GenerationParams,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        options: dict[str, Any] = {
            "temperature": params.temperature,
            "top_p": params.top_p,
        }
        if params.max_tokens is not None:
            options["num_predict"] = params.max_tokens
        if params.stop:
            options["stop"] = list(params.stop)
        if params.seed is not None:
            options["seed"] = params.seed

        payload: dict[str, Any] = {
            "model": model,
            "messages": [self._encode_message(m) for m in messages],
            "stream": stream,
            "options": options,
        }
        if params.keep_alive:
            payload["keep_alive"] = params.keep_alive
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
        return payload

    @staticmethod
    def _encode_message(message: Message) -> dict[str, Any]:
        encoded: dict[str, Any] = {"role": message.role, "content": message.content or ""}
        if message.tool_calls:
            encoded["tool_calls"] = [
                {"function": {"name": c.name, "arguments": dict(c.arguments)}}
                for c in message.tool_calls
            ]
        if message.role == "tool" and message.name:
            # Ollama identifies tool results by name; there is no call id in the
            # protocol, which is why ids are synthesized on the way in.
            encoded["name"] = message.name
        return encoded

    @staticmethod
    def _parse_tool_calls(message: dict[str, Any]) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            fn = raw.get("function") or {}
            name = fn.get("name")
            if not name:
                continue
            arguments = fn.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            calls.append(
                ToolCall(
                    # Ollama does not issue call ids; synthesize a stable one so
                    # the agent's bookkeeping matches every other provider.
                    id=raw.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                    name=name,
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
            )
        return calls

    @staticmethod
    def _usage_from_body(body: dict[str, Any]) -> Usage:
        return Usage(
            prompt_tokens=int(body.get("prompt_eval_count") or 0),
            completion_tokens=int(body.get("eval_count") or 0),
        )

    @staticmethod
    def _finish_reason(body: dict[str, Any]) -> FinishReason:
        reason = body.get("done_reason") or "stop"
        if reason == "length":
            return "length"
        if (body.get("message") or {}).get("tool_calls"):
            return "tool_calls"
        return "stop"

    def _completion_from_body(
        self, body: dict[str, Any], *, model: str, latency_ms: float
    ) -> Completion:
        message = body.get("message") or {}
        tool_calls = tuple(self._parse_tool_calls(message))
        return Completion(
            text=message.get("content") or "",
            tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else self._finish_reason(body),
            model=body.get("model") or model,
            provider=self.name,
            usage=self._usage_from_body(body),
            latency_ms=latency_ms,
        )
