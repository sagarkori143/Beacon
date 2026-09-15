"""Tool registry and invocation.

``invoke`` is where most agent implementations go wrong, so it is worth being
explicit about the design: **argument-validation failures, unknown tool names,
timeouts and permission denials are returned to the model as tool results, not
raised.** Models correct themselves reliably when shown the actual validation
error; an exception ends the turn and produces nothing useful.

What is *not* returned to the model: internal exception text. An unexpected
error is logged with its trace id and reported to the model as a generic
failure, because a stack trace in the context window is both a leak and a
distraction.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Collection, Iterator
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from app.core.logging import get_logger
from app.core.tenancy import Principal
from app.providers.llm.base import ToolSpec
from app.services.rag.token_budget import count_tokens, truncate_to_tokens
from app.tools.base import Tool, ToolContext, ToolResult

log = get_logger(__name__)


class ToolRegistry:
    """Holds tools and runs them safely."""

    def __init__(self, *, max_result_tokens: int = 1500) -> None:
        self._tools: dict[str, Tool] = {}
        self.max_result_tokens = max_result_tokens

    # -- registration --------------------------------------------------------

    def register(self, tool: Tool) -> Tool:
        name = tool.definition.name
        if name in self._tools and self._tools[name] is not tool:
            raise ValueError(f"Tool '{name}' is already registered")
        self._tools[name] = tool
        return tool

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    # -- exposure ------------------------------------------------------------

    def available_for(
        self, principal: Principal, *, enabled: Collection[str] | None = None
    ) -> list[Tool]:
        """Tools this caller may use.

        A tool the caller lacks scopes for is not merely refused at call time --
        it is never shown to the model, so the model does not waste a turn
        attempting something it cannot have.
        """
        return [
            tool
            for name, tool in sorted(self._tools.items())
            if (enabled is None or name in enabled)
            and all(principal.has_scope(scope) for scope in tool.definition.scopes)
        ]

    def specs_for(
        self, principal: Principal, *, enabled: Collection[str] | None = None
    ) -> list[ToolSpec]:
        return [
            ToolSpec(
                name=tool.definition.name,
                description=tool.definition.description,
                input_schema=tool.definition.json_schema,
            )
            for tool in self.available_for(principal, enabled=enabled)
        ]

    def descriptions_for(
        self, principal: Principal, *, enabled: Collection[str] | None = None
    ) -> list[tuple[str, str]]:
        return [
            (t.definition.name, t.definition.description)
            for t in self.available_for(principal, enabled=enabled)
        ]

    # -- invocation ----------------------------------------------------------

    async def invoke(self, name: str, raw_args: dict[str, Any], context: ToolContext) -> ToolResult:
        started = time.perf_counter()

        tool = self._tools.get(name)
        if tool is None:
            available = ", ".join(sorted(self._tools)) or "(none)"
            return ToolResult.failure(
                "unknown_tool",
                f"No tool named '{name}'. Available tools: {available}.",
            )

        definition = tool.definition

        missing = [s for s in definition.scopes if not context.principal.has_scope(s)]
        if missing:
            log.warning(
                "tool_denied",
                tool=name,
                user_id=str(context.principal.user_id),
                missing_scopes=missing,
            )
            return ToolResult.failure(
                "forbidden", f"You do not have permission to use the '{name}' tool."
            )

        try:
            args = definition.input_model.model_validate(raw_args or {})
        except PydanticValidationError as exc:
            return ToolResult.failure(
                "invalid_arguments",
                f"Arguments for '{name}' were not valid:\n{_format_errors(exc)}\n"
                f"Call it again with corrected arguments.",
            )

        # The tool's own timeout, but never past the run's overall deadline.
        remaining = max(0.0, context.deadline - time.monotonic())
        timeout = min(definition.timeout_s, remaining) if remaining else definition.timeout_s
        if timeout <= 0:
            return ToolResult.failure(
                "deadline_exceeded", f"Ran out of time before '{name}' could be called."
            )

        try:
            with context.trace.span(f"tool.{name}"):
                result = await asyncio.wait_for(tool.execute(args, context), timeout=timeout)
        except TimeoutError:
            log.warning("tool_timeout", tool=name, timeout_s=timeout)
            return ToolResult.failure(
                "timeout", f"The '{name}' tool did not respond within {timeout:.0f}s."
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a tool must not kill the turn
            log.exception(
                "tool_error", tool=name, trace_id=context.trace.trace_id, error=str(exc)[:300]
            )
            return ToolResult.failure(
                "internal_error", f"The '{name}' tool failed. Try a different approach."
            )

        return self._finalize(result, started)

    def _finalize(self, result: ToolResult, started: float) -> ToolResult:
        """Bound the result size so one chatty tool cannot exhaust the context."""
        content = result.content
        truncated = result.truncated
        if count_tokens(content) > self.max_result_tokens:
            content = truncate_to_tokens(content, self.max_result_tokens)
            truncated = True

        return ToolResult(
            ok=result.ok,
            content=content,
            data=result.data,
            citations=result.citations,
            error_code=result.error_code,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            truncated=truncated,
        )


def _format_errors(exc: PydanticValidationError) -> str:
    lines = []
    for error in exc.errors()[:8]:
        location = ".".join(str(p) for p in error["loc"]) or "<root>"
        lines.append(f"- {location}: {error['msg']}")
    return "\n".join(lines)


def build_default_registry(*, max_result_tokens: int = 1500) -> ToolRegistry:
    """The tools every deployment ships with."""
    from app.tools.builtin import (
        calculator,
        currency,
        current_datetime,
        document_lookup,
        hotel_availability,
        knowledge_search,
        list_documents,
        location_info,
        weather,
    )

    registry = ToolRegistry(max_result_tokens=max_result_tokens)
    for module in (
        knowledge_search,
        document_lookup,
        list_documents,
        location_info,
        current_datetime,
        calculator,
        weather,
        currency,
        hotel_availability,
    ):
        registry.register(module.TOOL)
    return registry
