"""Chat endpoints: one blocking, one streamed.

Both drive the same agent run. ``/chat`` consumes the event stream to
completion; ``/chat/stream`` forwards it as Server-Sent Events. There is no
second implementation that could behave differently.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.api.deps import (
    AppSettings,
    CurrentPrincipal,
    Trace,
    Uow,
    get_agent,
    get_redis_client,
)
from app.api.v1.chat_support import SSE_HEADERS, build_agent_request, to_chat_response
from app.core.logging import get_logger
from app.core.tracing import TraceContext
from app.repositories import conversation as conversation_repo
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.agent.memory import ConversationMemory
from app.services.agent.runtime import AgentResult, AgentRuntime

log = get_logger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    principal: CurrentPrincipal,
    uow: Uow,
    trace: Trace,
    settings: AppSettings,
    agent: AgentRuntime = Depends(get_agent),
    redis=Depends(get_redis_client),
) -> ChatResponse:
    """Ask a question and wait for the complete answer."""
    request, memory, conversation_id = await build_agent_request(
        payload, principal, uow, settings, redis, stream=False
    )
    result = await agent.run(request, trace=trace)

    await _persist(uow, memory, principal, conversation_id, payload.message, result, trace)
    return to_chat_response(result, conversation_id, include_trace=payload.include_trace)


@router.post("/stream")
async def chat_stream(
    payload: ChatRequest,
    request: Request,
    principal: CurrentPrincipal,
    uow: Uow,
    trace: Trace,
    settings: AppSettings,
    agent: AgentRuntime = Depends(get_agent),
    redis=Depends(get_redis_client),
) -> StreamingResponse:
    """Ask a question and receive progress plus tokens as they happen.

    Events are semantic, not raw model output: ``stage``, ``search``,
    ``conflict``, ``tool_call``, ``tool_result``, ``citation``, then ``token``
    for the answer itself, and finally ``usage`` and ``done``.
    """
    agent_request, memory, conversation_id = await build_agent_request(
        payload, principal, uow, settings, redis, stream=True
    )
    result = AgentResult(answer="")

    async def event_stream() -> AsyncIterator[str]:
        try:
            async for event in agent.run_stream(agent_request, trace=trace, sink=result):
                # Stop the model mid-generation when the client goes away, so a
                # closed browser tab does not hold a model server slot.
                if await request.is_disconnected():
                    log.info("chat_stream_client_disconnected", trace_id=trace.trace_id)
                    return

                if event.type == "done":
                    event.data["conversation_id"] = (
                        str(conversation_id) if conversation_id else None
                    )
                yield event.to_sse()
        finally:
            if result.answer:
                await _persist(
                    uow,
                    memory,
                    principal,
                    conversation_id,
                    payload.message,
                    result,
                    trace,
                )

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=SSE_HEADERS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _persist(
    uow: Uow,
    memory: ConversationMemory,
    principal: CurrentPrincipal,
    conversation_id: uuid.UUID,
    question: str,
    result: AgentResult,
    trace: TraceContext,
) -> None:
    """Write the turn to Redis (working state) and Postgres (durable record)."""
    await memory.append(principal.organization_id, conversation_id, role="user", content=question)
    await memory.append(
        principal.organization_id, conversation_id, role="assistant", content=result.answer
    )

    async with uow.begin() as session:
        conversation = await conversation_repo.get_or_create(
            session,
            principal.tenant,
            conversation_id=conversation_id,
            user_id=principal.user_id,
            location_id=principal.location_id,
            title=question[:120],
        )
        await conversation_repo.add_message(session, conversation, role="user", content=question)
        await conversation_repo.add_message(
            session,
            conversation,
            role="assistant",
            content=result.answer,
            provider=result.routing.provider if result.routing else None,
            model=result.routing.model if result.routing else None,
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            estimated_cost_usd=result.estimated_cost_usd,
            latency_ms=result.latency_ms,
            grounding_ratio=result.grounding,
            trace=result.trace,
            citations=result.citations,
        )
