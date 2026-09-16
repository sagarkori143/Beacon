"""Admission checks and rate limiting.

Two different concerns, both applied before any expensive work:

**Resource protection.** A per-user token bucket in Redis, and a hard cap on
input length. Both are per-organization namespaced so one tenant cannot consume
another's budget.

**Prompt-injection posture.** The defence that actually works is structural and
lives elsewhere: retrieved text is delimited and escaped in the context builder,
tools validate their own arguments, and tenant scope comes from the token rather
than from anything a model produces. What is here is a thin screen for the
blatant cases plus, more usefully, *detection* -- an injection attempt in an
uploaded document is worth logging even when it was already going to fail.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from uuid import UUID

from redis.asyncio import Redis

from app.core.errors import RateLimitError, ValidationError
from app.core.logging import get_logger
from app.core.redis import public_rate_limit_key, rate_limit_key

log = get_logger(__name__)

#: Phrasings that only appear when someone is trying to restructure the prompt.
#: Matching one is logged and stripped; it is not treated as proof of intent,
#: because legitimate documents do occasionally quote this language.
_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bignore\s+(?:all\s+|any\s+)?(?:previous|prior|above)\s+instructions?\b", re.I),
    re.compile(r"\bdisregard\s+(?:the\s+)?(?:system|previous)\s+prompt\b", re.I),
    re.compile(r"\byou\s+are\s+now\s+(?:a|an|in)\b.{0,40}\bmode\b", re.I),
    re.compile(r"\breveal\s+(?:your\s+)?(?:system\s+prompt|instructions)\b", re.I),
    re.compile(r"</?(?:system|instructions|sources?)\s*>", re.I),
)


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    query: str
    flagged_patterns: tuple[str, ...] = ()

    @property
    def suspicious(self) -> bool:
        return bool(self.flagged_patterns)


def screen_input(query: str, *, max_chars: int) -> AdmissionResult:
    """Validate and lightly sanitize a user query."""
    cleaned = (query or "").strip()
    if not cleaned:
        raise ValidationError("Query cannot be empty")
    if len(cleaned) > max_chars:
        raise ValidationError(
            f"Query is too long ({len(cleaned)} characters); the limit is {max_chars}"
        )

    flagged: list[str] = []
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(cleaned):
            flagged.append(pattern.pattern[:40])
            cleaned = pattern.sub(" ", cleaned)

    if flagged:
        # The query text itself is deliberately not logged.
        log.warning("prompt_injection_screened", patterns=flagged)

    return AdmissionResult(query=cleaned.strip(), flagged_patterns=tuple(flagged))


async def check_public_rate_limit(
    redis: Redis,
    *,
    organization_id: UUID,
    client: str,
    limit_per_minute: int,
) -> None:
    """The same fixed window, for callers identified by address rather than id.

    Public chat has no account behind it, and every question costs a model call.
    Without a limit here one script keeps the model server busy indefinitely and
    nobody else gets an answer.
    """
    if limit_per_minute <= 0:
        return

    window = int(time.time() // 60)
    key = public_rate_limit_key(organization_id, client, window)
    await _consume(redis, key, limit_per_minute)


async def _consume(redis: Redis, key: str, limit_per_minute: int) -> None:
    try:
        pipeline = redis.pipeline()
        pipeline.incr(key)
        pipeline.expire(key, 120)
        count, _ = await pipeline.execute()
    except Exception as exc:  # noqa: BLE001 - never fail a request on Redis
        log.warning("rate_limit_unavailable", error=str(exc)[:200])
        return

    if int(count) > limit_per_minute:
        raise RateLimitError(
            f"Rate limit of {limit_per_minute} requests per minute exceeded",
            retry_after_s=60 - int(time.time() % 60),
        )


async def check_rate_limit(
    redis: Redis,
    *,
    organization_id: UUID,
    user_id: UUID,
    limit_per_minute: int,
) -> None:
    """Fixed-window counter, one key per user per minute.

    A fixed window can allow up to 2x the limit across a window boundary. That
    is an accepted tradeoff: it costs one Redis round trip instead of the
    several a sliding window needs, and the purpose here is protecting the model
    server from a runaway client, not precise quota accounting.
    """
    if limit_per_minute <= 0:
        return

    window = int(time.time() // 60)
    await _consume(redis, rate_limit_key(organization_id, user_id, window), limit_per_minute)
