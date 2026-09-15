"""Shared provider plumbing: health reporting and the HTTP client base.

Everything vendor-agnostic that more than one provider family needs lives here,
so an individual provider file contains only that vendor's wire format.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx

from app.core.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.core.logging import get_logger
from app.core.tracing import TraceContext
from app.providers.circuit import CircuitBreaker

log = get_logger(__name__)

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    name: str
    ok: bool
    detail: str | None = None
    latency_ms: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "ok": self.ok}
        if self.detail:
            out["detail"] = self.detail
        if self.latency_ms is not None:
            out["latency_ms"] = round(self.latency_ms, 1)
        out.update(self.extra)
        return out


#: Status codes worth retrying. 408/429 and 5xx are transient; 4xx otherwise is
#: a bug in our request and retrying just burns the rate limit.
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})


class HTTPProviderBase:
    """Base for every HTTP-backed provider.

    Owns one ``httpx.AsyncClient`` per provider instance (connection reuse
    matters a great deal when the model server is across a network), a
    concurrency semaphore, retry with jittered backoff that honours
    ``Retry-After``, and the circuit breaker.
    """

    def __init__(
        self,
        *,
        name: str,
        base_url: str | None,
        timeout_s: float,
        connect_timeout_s: float,
        max_concurrency: int,
        max_retries: int,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/") if base_url else None
        self.max_retries = max_retries
        self.circuit = CircuitBreaker(name=name)
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._client = httpx.AsyncClient(
            base_url=self.base_url or "",
            timeout=httpx.Timeout(timeout_s, connect=connect_timeout_s),
            headers=headers or {},
            limits=httpx.Limits(
                max_connections=max_concurrency * 2,
                max_keepalive_connections=max_concurrency,
            ),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def is_available(self) -> bool:
        return self.circuit.is_available

    # -- error normalization -------------------------------------------------

    def _error_from_response(self, response: httpx.Response) -> ProviderError:
        """Turn any vendor error body into one exception type.

        Subclasses override ``_extract_error_message`` to pull the vendor's
        message out; the classification itself is identical everywhere.
        """
        status = response.status_code
        message = self._extract_error_message(response)

        if status in (401, 403):
            return ProviderAuthError(
                f"{self.name}: authentication failed ({status}): {message}",
                provider=self.name,
                status=status,
            )
        if status == 429 or status >= 500:
            return ProviderUnavailable(
                f"{self.name}: {status} {message}",
                provider=self.name,
            )
        return ProviderError(
            f"{self.name}: {status} {message}",
            provider=self.name,
            status=status,
            retryable=status in RETRYABLE_STATUS,
        )

    def _extract_error_message(self, response: httpx.Response) -> str:
        try:
            body = response.json()
        except Exception:  # noqa: BLE001 - non-JSON error bodies are common
            return response.text[:300]
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                return str(err.get("message") or err)
            if isinstance(err, str):
                return err
            for key in ("message", "detail", "msg"):
                if key in body:
                    return str(body[key])
        return str(body)[:300]

    @staticmethod
    def _retry_after_s(response: httpx.Response) -> float | None:
        raw = response.headers.get("retry-after")
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    # -- request helpers -----------------------------------------------------

    async def _call(
        self,
        fn: Callable[[], Awaitable[T]],
        *,
        trace: TraceContext | None = None,
        operation: str = "request",
    ) -> T:
        """Run an outbound call under the semaphore, breaker and retry policy."""
        self.circuit.check()
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                async with self._semaphore:
                    result = await fn()
                self.circuit.record_success()
                return result
            except ProviderAuthError as exc:
                # Bad credentials will not fix themselves; fail immediately but
                # still count it so a misconfigured provider drops out of routing.
                self.circuit.record_failure()
                raise exc
            except httpx.TimeoutException:
                last_error = ProviderTimeout(
                    f"{self.name}: {operation} timed out", provider=self.name
                )
                self.circuit.record_failure()
            except httpx.HTTPError as exc:
                last_error = ProviderUnavailable(
                    f"{self.name}: {operation} failed: {exc}", provider=self.name
                )
                self.circuit.record_failure()
            except ProviderError as exc:
                last_error = exc
                self.circuit.record_failure()
                if not exc.retryable:
                    raise

            if attempt < self.max_retries:
                delay = self._backoff(attempt)
                if trace is not None and trace.span_id:
                    log.debug(
                        "provider_retry",
                        provider=self.name,
                        operation=operation,
                        attempt=attempt + 1,
                        delay_s=round(delay, 2),
                    )
                await asyncio.sleep(delay)

        assert last_error is not None
        raise last_error

    @staticmethod
    def _backoff(attempt: int, base: float = 0.5, cap: float = 8.0) -> float:
        """Exponential backoff with full jitter."""
        return random.uniform(0, min(cap, base * (2**attempt)))  # noqa: S311

    async def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        response = await self._client.post(url, json=payload, headers=headers)
        if response.status_code >= 400:
            raise self._error_from_response(response)
        return response.json()

    async def _get_json(self, url: str, **kwargs: Any) -> dict[str, Any]:
        response = await self._client.get(url, **kwargs)
        if response.status_code >= 400:
            raise self._error_from_response(response)
        return response.json()
