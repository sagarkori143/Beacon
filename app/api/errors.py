"""Exception handlers.

Every error leaves the API as an RFC 9457 problem document with a stable
``type`` a client can branch on. Internal exception text never reaches the
response: it is logged with the request id, and the client is given that id.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import AppError, NoModelAvailable, RateLimitError
from app.core.logging import get_logger

log = get_logger(__name__)

PROBLEM_CONTENT_TYPE = "application/problem+json"


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        body = exc.to_problem()
        body["instance"] = str(request.url.path)
        if request_id := _request_id(request):
            body["request_id"] = request_id

        headers: dict[str, str] = {}
        # Tell a client when to come back rather than making it guess.
        if isinstance(exc, RateLimitError | NoModelAvailable):
            headers["Retry-After"] = str(exc.details.get("retry_after_s", 30))

        if exc.status_code >= 500:
            log.error(
                "request_failed",
                code=exc.code,
                path=request.url.path,
                error=exc.message[:300],
            )
        return JSONResponse(
            status_code=exc.status_code,
            content=body,
            headers=headers,
            media_type=PROBLEM_CONTENT_TYPE,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "type": "about:blank#validation_error",
                "title": "validation error",
                "status": 422,
                "detail": "The request body or parameters are not valid.",
                "instance": str(request.url.path),
                "errors": [
                    {
                        "field": ".".join(str(p) for p in error["loc"][1:]) or "body",
                        "message": error["msg"],
                    }
                    for error in exc.errors()[:20]
                ],
                "request_id": _request_id(request),
            },
            media_type=PROBLEM_CONTENT_TYPE,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "type": f"about:blank#http_{exc.status_code}",
                "title": "http error",
                "status": exc.status_code,
                "detail": str(exc.detail),
                "instance": str(request.url.path),
                "request_id": _request_id(request),
            },
            headers=getattr(exc, "headers", None),
            media_type=PROBLEM_CONTENT_TYPE,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        """Last resort.

        The exception is logged with its traceback and correlated by request id;
        the client gets that id and nothing else. An internal message in a
        response body is an information leak and a support burden.
        """
        request_id = _request_id(request)
        log.exception(
            "unhandled_exception",
            path=request.url.path,
            request_id=request_id,
            error=str(exc)[:300],
        )
        return JSONResponse(
            status_code=500,
            content={
                "type": "about:blank#internal_error",
                "title": "internal error",
                "status": 500,
                "detail": "An unexpected error occurred.",
                "instance": str(request.url.path),
                "request_id": request_id,
            },
            media_type=PROBLEM_CONTENT_TYPE,
        )
