"""Circuit breaker shared by every provider.

A remote model server on someone's GPU box, or a cloud vendor having a bad day,
must degrade the system rather than hang it. When a provider crosses the failure
threshold its circuit opens and calls fail fast with
:class:`ProviderUnavailable`, which the router treats as "skip this candidate"
rather than "the request failed".
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

from app.core.errors import ProviderUnavailable
from app.core.logging import get_logger

log = get_logger(__name__)


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(slots=True)
class CircuitBreaker:
    """Failure-count breaker with a half-open probe.

    Deliberately simple: no sliding windows or percentiles. The failure modes it
    guards against (a dead host, an expired key, a rate-limit wall) are
    unambiguous, and a complicated breaker is harder to reason about during the
    incident it is supposed to help with.
    """

    name: str
    failure_threshold: int = 5
    #: Failures older than this are forgotten, so isolated blips never trip it.
    window_s: float = 30.0
    #: How long the circuit stays open before a single probe is allowed.
    reset_timeout_s: float = 20.0

    _failures: list[float] = field(default_factory=list, init=False)
    _opened_at: float | None = field(default=None, init=False)
    _half_open_in_flight: bool = field(default=False, init=False)

    @property
    def state(self) -> CircuitState:
        if self._opened_at is None:
            return CircuitState.CLOSED
        if time.monotonic() - self._opened_at >= self.reset_timeout_s:
            return CircuitState.HALF_OPEN
        return CircuitState.OPEN

    @property
    def is_available(self) -> bool:
        """Whether the router should consider this provider at all."""
        return self.state is not CircuitState.OPEN

    def check(self) -> None:
        """Raise if the circuit is open. Called before every outbound request."""
        state = self.state
        if state is CircuitState.OPEN:
            remaining = self.reset_timeout_s - (time.monotonic() - (self._opened_at or 0))
            raise ProviderUnavailable(
                f"Circuit open for provider '{self.name}'; retrying in {remaining:.0f}s",
                provider=self.name,
            )
        if state is CircuitState.HALF_OPEN:
            # Let exactly one probe through; everyone else keeps failing fast.
            if self._half_open_in_flight:
                raise ProviderUnavailable(
                    f"Circuit half-open for provider '{self.name}'; probe in flight",
                    provider=self.name,
                )
            self._half_open_in_flight = True

    def record_success(self) -> None:
        if self._opened_at is not None:
            log.info("circuit_closed", provider=self.name)
        self._failures.clear()
        self._opened_at = None
        self._half_open_in_flight = False

    def record_failure(self) -> None:
        now = time.monotonic()
        self._half_open_in_flight = False

        if self._opened_at is not None:
            # Probe failed: restart the open period.
            self._opened_at = now
            return

        cutoff = now - self.window_s
        self._failures = [t for t in self._failures if t >= cutoff]
        self._failures.append(now)

        if len(self._failures) >= self.failure_threshold:
            self._opened_at = now
            log.warning(
                "circuit_opened",
                provider=self.name,
                failures=len(self._failures),
                reset_in_s=self.reset_timeout_s,
            )

    def snapshot(self) -> dict[str, object]:
        return {
            "name": self.name,
            "state": self.state.value,
            "recent_failures": len(self._failures),
        }
