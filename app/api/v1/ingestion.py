"""Ingestion job and progress endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query

from app.api.deps import CurrentPrincipal, Providers, Uow
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
async def queue_stats(principal: CurrentPrincipal, providers: Providers) -> dict:
    """Queue depth and staleness.

    Deliberately not tenant-filtered -- the queue is shared infrastructure and
    these are operational counters, not tenant data. No document or
    organization identifiers are exposed.
    """
    return await providers.require_queue().stats()
