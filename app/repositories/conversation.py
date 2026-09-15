"""Conversation persistence.

Durable history for audit and evaluation. Working state lives in Redis with a
TTL; these rows are never fed back into the retrieval index, because a user's
question is not organizational knowledge.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import TenantContext
from app.models.conversation import Conversation, ConversationMessage
from app.repositories.base import assert_tenant, require


async def get_or_create(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    conversation_id: UUID | None,
    user_id: UUID,
    location_id: UUID | None = None,
    title: str | None = None,
) -> Conversation:
    if conversation_id is not None:
        existing = await session.get(Conversation, conversation_id)
        if existing is not None:
            assert_tenant(existing, tenant)
            return existing

    conversation = Conversation(
        id=conversation_id,
        organization_id=tenant.organization_id,
        location_id=location_id,
        user_id=user_id,
        title=title[:300] if title else None,
    )
    session.add(conversation)
    await session.flush()
    return conversation


async def get(session: AsyncSession, tenant: TenantContext, conversation_id: UUID) -> Conversation:
    conversation = await session.get(Conversation, conversation_id)
    conversation = require(conversation, what="Conversation", identifier=conversation_id)
    assert_tenant(conversation, tenant)
    return conversation


async def add_message(
    session: AsyncSession,
    conversation: Conversation,
    *,
    role: str,
    content: str,
    provider: str | None = None,
    model: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    estimated_cost_usd: float | None = None,
    latency_ms: float | None = None,
    grounding_ratio: float | None = None,
    trace: dict[str, Any] | None = None,
    citations: list[dict[str, Any]] | None = None,
) -> ConversationMessage:
    message = ConversationMessage(
        conversation_id=conversation.id,
        organization_id=conversation.organization_id,
        role=role,
        content=content,
        provider=provider,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        estimated_cost_usd=estimated_cost_usd,
        latency_ms=latency_ms,
        grounding_ratio=grounding_ratio,
        trace=trace or {},
        citations=citations or [],
    )
    session.add(message)
    await session.flush()
    return message


async def recent_messages(
    session: AsyncSession, tenant: TenantContext, conversation_id: UUID, *, limit: int = 20
) -> Sequence[ConversationMessage]:
    result = await session.execute(
        select(ConversationMessage)
        .where(
            ConversationMessage.organization_id == tenant.organization_id,
            ConversationMessage.conversation_id == conversation_id,
        )
        .order_by(ConversationMessage.created_at.desc())
        .limit(limit)
    )
    return list(reversed(result.scalars().all()))
