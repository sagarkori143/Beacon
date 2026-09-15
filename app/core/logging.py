"""Structured logging.

Logs are JSON in every environment except local development, and are correlated
by ``request_id`` / ``job_id`` / ``trace_id`` bound into contextvars so a handler
deep in the call stack does not have to thread them through by hand.

Deliberately *not* logged anywhere: prompt text, document content, retrieved
chunk bodies, credentials. Counts, ids, latencies and decisions only.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars, unbind_contextvars

#: Keys scrubbed from every event before it is emitted.
_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "api_key",
        "authorization",
        "token",
        "access_token",
        "refresh_token",
        "secret",
        "jwt_secret",
        "prompt",
        "messages",
        "content",
        "text",
        "embedding",
    }
)


def _scrub(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Redact sensitive values rather than dropping the key, so their presence
    is still visible when debugging."""
    for key in list(event_dict):
        if key.lower() in _SENSITIVE_KEYS:
            event_dict[key] = "[redacted]"
    return event_dict


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Install the structlog pipeline. Idempotent."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
        force=True,
    )
    # Uvicorn installs its own handlers; route them through structlog instead.
    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(noisy).handlers.clear()
        logging.getLogger(noisy).propagate = True
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _scrub,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)


@contextmanager
def log_context(**kwargs: Any) -> Iterator[None]:
    """Bind keys for the duration of a block, restoring the previous state."""
    bind_contextvars(**kwargs)
    try:
        yield
    finally:
        unbind_contextvars(*kwargs.keys())


__all__ = [
    "bind_contextvars",
    "clear_contextvars",
    "configure_logging",
    "get_logger",
    "log_context",
]
