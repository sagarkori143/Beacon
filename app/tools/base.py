"""Tool contract.

A tool's input schema is a Pydantic model, and the JSON Schema shown to the
model is generated from that same model. One source of truth means the schema
the model sees and the validation applied to what it returns cannot drift apart
-- which is the usual way tool calling rots over time.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from app.core.tenancy import Principal, TenantContext
from app.core.tracing import TraceContext

if TYPE_CHECKING:
    from app.core.db import UnitOfWork
    from app.providers.registry import ProviderBundle

TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,47}$")


@lru_cache(maxsize=256)
def _schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema for a tool's input model.

    Generated once per model class. This is the *only* place a tool schema is
    produced, so what the model is shown and what its arguments are validated
    against are necessarily the same thing.
    """
    return model.model_json_schema()


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Everything the registry and the model need to know about a tool."""

    name: str
    description: str
    input_model: type[BaseModel]
    #: Scopes the caller must hold. Checked before execution, and the tool is
    #: hidden from models when the caller lacks them.
    scopes: frozenset[str] = frozenset()
    timeout_s: float = 15.0
    #: A tool that changes state is never retried automatically.
    side_effecting: bool = False

    def __post_init__(self) -> None:
        if not TOOL_NAME_RE.match(self.name):
            raise ValueError(
                f"Invalid tool name {self.name!r}: must be lowercase, 3-48 chars, "
                "letters/digits/underscore, starting with a letter"
            )

    @property
    def json_schema(self) -> dict[str, Any]:
        # Cached on the model class rather than the instance: ToolDefinition
        # uses slots, so there is no per-instance __dict__ to memoize into.
        return _schema_for(self.input_model)


@dataclass(slots=True)
class ToolContext:
    """What a tool is allowed to touch.

    A tool never receives a database session, only a :class:`UnitOfWork`, so it
    cannot hold a transaction open across its own network calls. It receives the
    caller's tenant and principal, so a tool physically cannot query outside the
    caller's organization.
    """

    tenant: TenantContext
    principal: Principal
    uow: UnitOfWork
    providers: ProviderBundle
    trace: TraceContext
    #: Monotonic deadline for the whole agent run.
    deadline: float
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What comes back from a tool.

    ``content`` is what the model sees; ``data`` is the structured form the API
    returns to the client. Failures are represented here rather than raised, so
    the agent can hand them back to the model to correct.
    """

    ok: bool
    content: str
    data: dict[str, Any] | None = None
    citations: tuple[dict[str, Any], ...] = ()
    error_code: str | None = None
    latency_ms: float = 0.0
    truncated: bool = False

    @classmethod
    def success(
        cls,
        content: str,
        *,
        data: dict[str, Any] | None = None,
        citations: tuple[dict[str, Any], ...] = (),
    ) -> ToolResult:
        return cls(ok=True, content=content, data=data, citations=citations)

    @classmethod
    def failure(cls, code: str, message: str) -> ToolResult:
        return cls(ok=False, content=message, error_code=code)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "content": self.content,
            "data": self.data,
            "error_code": self.error_code,
            "latency_ms": round(self.latency_ms, 1),
            "truncated": self.truncated,
        }


class Tool(ABC):
    """One callable capability."""

    definition: ToolDefinition

    @abstractmethod
    async def execute(self, args: BaseModel, context: ToolContext) -> ToolResult: ...

    @property
    def name(self) -> str:
        return self.definition.name
