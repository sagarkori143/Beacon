"""Request correlation and access logging.

Every request gets an id (honouring an inbound ``X-Request-ID`` so a gateway's
id survives) and a trace. Both are bound into the logging context, so a log line
written five layers deep carries them without being passed the values.

Ingestion jobs carry the same trace id through the queue, which is what lets you
follow an upload from the HTTP request through to the worker that processed it.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import bind_contextvars, clear_contextvars, get_logger
from app.core.tracing import TraceContext

log = get_logger(__name__)

#: Paths whose access logs would be pure noise.
_QUIET_PATHS = frozenset({"/health", "/health/ready", "/metrics", "/favicon.ico"})


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        trace = TraceContext.new(request_id=request_id)

        request.state.request_id = request_id
        request.state.trace = trace

        bind_contextvars(request_id=request_id, trace_id=trace.trace_id)
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            # The handler in api.errors produces the response; this only records
            # the timing, since call_next re-raises before one exists.
            duration = (time.perf_counter() - started) * 1000.0
            log.warning(
                "request_errored",
                method=request.method,
                path=request.url.path,
                duration_ms=round(duration, 1),
            )
            clear_contextvars()
            raise

        duration = (time.perf_counter() - started) * 1000.0
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Trace-ID"] = trace.trace_id

        if request.url.path not in _QUIET_PATHS:
            log.info(
                "request_completed",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=round(duration, 1),
            )

        clear_contextvars()
        return response
