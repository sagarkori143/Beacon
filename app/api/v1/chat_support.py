"""Pieces shared by the signed-in chat endpoints and the public ones.

Both build the same agent request and shape the same response. Keeping that in
one place is the point: a public visitor and an organization's own user must get
answers through identical machinery, or the two will drift and only one of them
will stay correct.
"""

from __future__ import annotations

import uuid

from app.core.config import Settings
from app.core.db import UnitOfWork
from app.core.tenancy import Principal
from app.repositories.organization import get_location, get_organization
from app.schemas.chat import ChatRequest, ChatResponse, CitationOut, UsageOut
from app.services.agent.memory import ConversationMemory
from app.services.agent.runtime import AgentRequest, AgentResult
from app.services.rag.citations import used_citations

#: SSE headers that matter in production. Without X-Accel-Buffering, nginx
#: buffers the whole stream and the user sees nothing until it completes.
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


async def build_agent_request(
    payload: ChatRequest,
    principal: Principal,
    uow: UnitOfWork,
    settings: Settings,
    redis,
    *,
    stream: bool,
) -> tuple[AgentRequest, ConversationMemory, uuid.UUID]:
    """Assemble the agent request: identity, history and organization policy."""
    conversation_id = payload.conversation_id or uuid.uuid4()
    memory = ConversationMemory(
        redis,
        ttl_s=settings.agent.conversation_ttl_s,
        max_messages=settings.agent.max_history_messages,
    )
    history = await memory.load(principal.organization_id, conversation_id)

    async with uow.begin() as session:
        organization = await get_organization(session, principal.organization_id)
        location = (
            await get_location(session, principal.tenant, principal.location_id)
            if principal.location_id
            else None
        )
        model_pins = organization.model_pins
        allowed = organization.allowed_providers
        organization_name = organization.name
        location_name = location.name if location else None

    return (
        AgentRequest(
            query=payload.message,
            principal=principal,
            uow=uow,
            conversation_id=conversation_id,
            history=history,
            organization_name=organization_name,
            location_name=location_name,
            enabled_tools=tuple(payload.tools) if payload.tools else None,
            force_retrieval=payload.force_retrieval,
            prefer_model=payload.model,
            model_pins=model_pins,
            allowed_providers=tuple(allowed) if allowed else None,
            language=payload.language,
            stream_tokens=stream,
        ),
        memory,
        conversation_id,
    )


def to_chat_response(
    result: AgentResult, conversation_id: uuid.UUID, *, include_trace: bool
) -> ChatResponse:
    # Return only the sources the answer actually cited: listing six under an
    # answer that drew on two overstates the evidence behind it.
    citations = used_citations(result.answer, result.context.citations) if result.context else []
    return ChatResponse(
        answer=result.answer,
        conversation_id=conversation_id,
        citations=[CitationOut(**c.to_dict()) for c in citations],
        provider=result.routing.provider if result.routing else None,
        model=result.routing.model if result.routing else None,
        usage=UsageOut(
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            estimated_cost_usd=round(result.estimated_cost_usd, 6),
        ),
        grounding_ratio=round(result.grounding, 3),
        low_confidence=result.low_confidence,
        finish_reason=result.finish_reason,
        latency_ms=round(result.latency_ms, 1),
        trace=result.trace if include_trace else None,
    )
