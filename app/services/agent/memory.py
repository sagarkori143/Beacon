"""Short-term conversation state.

Held in Redis with a TTL, namespaced by organization. This is working memory for
a conversation in progress -- not organizational knowledge. It is never indexed,
never retrieved, and expires on its own.

That distinction matters beyond tidiness: a user's speculative question is not a
fact about the business, and letting conversational text leak into the knowledge
base is how a RAG system starts confidently repeating its own guesses back.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from redis.asyncio import Redis

from app.core.logging import get_logger
from app.core.redis import conversation_key
from app.providers.llm.base import Message

log = get_logger(__name__)


class ConversationMemory:
    def __init__(self, redis: Redis, *, ttl_s: int, max_messages: int) -> None:
        self.redis = redis
        self.ttl_s = ttl_s
        self.max_messages = max_messages

    async def load(self, organization_id: UUID, conversation_id: UUID) -> tuple[Message, ...]:
        """Recent turns, oldest first. Returns empty on any failure.

        Redis being unavailable degrades a conversation to single-turn; it does
        not fail the request.
        """
        key = conversation_key(organization_id, conversation_id)
        try:
            raw = await self.redis.lrange(key, -self.max_messages, -1)
        except Exception as exc:  # noqa: BLE001
            log.warning("memory_unavailable", error=str(exc)[:200])
            return ()

        messages: list[Message] = []
        for item in raw:
            try:
                payload = json.loads(item)
            except json.JSONDecodeError:
                continue
            role = payload.get("role")
            if role in ("user", "assistant") and payload.get("content"):
                messages.append(Message(role=role, content=payload["content"]))
        return tuple(messages)

    async def append(
        self,
        organization_id: UUID,
        conversation_id: UUID,
        *,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        key = conversation_key(organization_id, conversation_id)
        payload = json.dumps(
            {"role": role, "content": content, **(metadata or {})}, ensure_ascii=False
        )
        try:
            pipeline = self.redis.pipeline()
            pipeline.rpush(key, payload)
            # Trim on every write so a long conversation cannot grow unbounded.
            pipeline.ltrim(key, -self.max_messages * 2, -1)
            pipeline.expire(key, self.ttl_s)
            await pipeline.execute()
        except Exception as exc:  # noqa: BLE001
            log.warning("memory_write_failed", error=str(exc)[:200])

    async def clear(self, organization_id: UUID, conversation_id: UUID) -> None:
        try:
            await self.redis.delete(conversation_key(organization_id, conversation_id))
        except Exception as exc:  # noqa: BLE001
            log.warning("memory_clear_failed", error=str(exc)[:200])
