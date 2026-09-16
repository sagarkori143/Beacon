"""Ingestion job and progress endpoints."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from app.api.deps import CurrentAdmin, CurrentPrincipal, Progress, Providers, Uow
from app.api.v1.chat_support import SSE_HEADERS
from app.core.enums import JobStatus
from app.repositories import ingestion as job_repo
from app.schemas.document import JobEventOut, JobOut

router = APIRouter(prefix="/ingestion", tags=["ingestion"])


@router.get("/jobs", response_model=list[JobOut])
async def list_jobs(
    principal: CurrentPrincipal,
    uow: Uow,
    status: JobStatus | None = Query(default=None),
    document_id: UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[JobOut]:
    async with uow.begin() as session:
        jobs = await job_repo.list_jobs(
            session,
            principal.tenant,
            status=status,
            document_id=document_id,
            limit=limit,
            offset=offset,
        )
        return [JobOut.model_validate(job) for job in jobs]


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(job_id: UUID, principal: CurrentPrincipal, uow: Uow) -> JobOut:
    """Current stage and progress for one ingestion job."""
    async with uow.begin() as session:
        job = await job_repo.get_job(session, principal.tenant, job_id)
        return JobOut.model_validate(job)


@router.get("/jobs/{job_id}/events", response_model=list[JobEventOut])
async def get_job_events(job_id: UUID, principal: CurrentPrincipal, uow: Uow) -> list[JobEventOut]:
    """Stage-by-stage history, with timings and per-stage detail.

    This is where the OCR decision is recorded: which pages were sent to OCR,
    and the measured reasons why. Answering "why did this document take four
    minutes?" is meant to be a single request, not an investigation.
    """
    async with uow.begin() as session:
        await job_repo.get_job(session, principal.tenant, job_id)
        events = await job_repo.list_events(session, principal.tenant, job_id)
        return [JobEventOut.model_validate(event) for event in events]


@router.get("/queue")
async def queue_stats(admin: CurrentAdmin, providers: Providers) -> dict:
    """Queue depth and staleness.

    Deliberately not tenant-filtered -- the queue is shared infrastructure and
    these are operational counters, not tenant data. Admin-only all the same:
    backlog depth and staleness say something about the deployment, and there is
    no reason for that to be readable without a credential. No document or
    organization identifiers are exposed.
    """
    return await providers.require_queue().stats()


@router.get("/jobs/{job_id}/stream")
async def stream_job_progress(
    job_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    uow: Uow,
    progress: Progress,
) -> StreamingResponse:
    """Follow a job's stages as they happen.

    Sends everything already recorded in Postgres **first**, then follows the
    live feed. Without that catch-up a watcher who opens the page a second after
    uploading -- which is everyone -- silently misses PARSING and OCR, the two
    stages they most want to see.

    The tenant check happens once, up front, before a single byte is streamed:
    a job belonging to another organization is not found, and no stream opens.
    """
    async with uow.begin() as session:
        job = await job_repo.get_job(session, principal.tenant, job_id)
        history = await job_repo.list_events(session, principal.tenant, job_id)
        terminal = job.is_terminal

    async def event_stream() -> AsyncIterator[str]:
        for event in history:
            yield _sse(
                "stage",
                {
                    "stage": event.stage.value,
                    "status": event.status,
                    "progress": event.stage.progress,
                    "message": event.message,
                    "duration_ms": event.duration_ms,
                    "detail": event.detail,
                    "at": event.created_at.isoformat(),
                    "replay": True,
                },
            )

        # A job that already finished has nothing left to say. Closing rather
        # than holding the connection open means the client's `onerror` is not
        # the thing that tells it the job is done.
        if terminal:
            yield _sse("done", {"job_id": str(job_id), "status": "terminal"})
            return

        async for _entry_id, payload in progress.follow(job_id):
            if await request.is_disconnected():
                return
            if not payload:
                # A quiet stage. Keep the connection warm through proxies that
                # would otherwise time it out.
                yield ": keep-alive\n\n"
                continue

            yield _sse("stage", payload)
            if payload.get("stage") in {"COMPLETED", "FAILED"}:
                yield _sse("done", {"job_id": str(job_id), "status": payload.get("status")})
                return

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=SSE_HEADERS)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
