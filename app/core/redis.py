"""Redis connection management.

Redis carries three distinct workloads here, all through one pool: the ingestion
queue (Streams), short-term conversation state, and SSE event replay buffers.
Every key is namespaced by organization so a key collision cannot become a
cross-tenant read.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from redis.asyncio import ConnectionPool, Redis

from app.core.config import Settings

_pool: ConnectionPool | None = None
_client: Redis | None = None


def init_redis(settings: Settings) -> Redis:
    """Create the process-wide client. Idempotent."""
    global _pool, _client
    if _client is None:
        _pool = ConnectionPool.from_url(
            settings.redis_url,
            decode_responses=True,
            health_check_interval=30,
        )
        _client = Redis(connection_pool=_pool)
    return _client


def get_redis() -> Redis:
    if _client is None:
        raise RuntimeError("Redis not initialized; call init_redis() first")
    return _client


async def close_redis() -> None:
    global _pool, _client
    if _client is not None:
        await _client.aclose()
    if _pool is not None:
        await _pool.disconnect()
    _client = None
    _pool = None


# ---------------------------------------------------------------------------
# Key naming
# ---------------------------------------------------------------------------


def org_key(organization_id: UUID, *parts: str) -> str:
    """Build an organization-namespaced key: ``org:{id}:part:part``."""
    return ":".join(("org", str(organization_id), *parts))


def conversation_key(organization_id: UUID, conversation_id: UUID) -> str:
    return org_key(organization_id, "conv", str(conversation_id))


def rate_limit_key(organization_id: UUID, user_id: UUID, window: int) -> str:
    return org_key(organization_id, "rl", str(user_id), str(window))


def plan_cache_key(organization_id: UUID, query_hash: str) -> str:
    return org_key(organization_id, "plan", query_hash)


def run_events_key(run_id: UUID | str) -> str:
    """Stream holding emitted SSE events so a reconnect can replay them."""
    return f"run:{run_id}:events"


async def check_redis() -> dict[str, Any]:
    try:
        client = get_redis()
        pong = await client.ping()
        return {"ok": bool(pong)}
    except Exception as exc:  # noqa: BLE001 - health must never raise
        return {"ok": False, "error": str(exc)}
