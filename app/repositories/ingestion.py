"""Ingestion job and event queries."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import IngestionStage, JobStatus
from app.core.tenancy import TenantContext
from app.models.ingestion import IngestionJob, IngestionJobEvent
from app.repositories.base import assert_tenant, require


async def get_job(session: AsyncSession, tenant: TenantContext, job_id: UUID) -> IngestionJob:
    job = await session.get(IngestionJob, job_id)
    job = require(job, what="Ingestion job", identifier=job_id)
    assert_tenant(job, tenant)
    return job


async def get_job_for_version(
    session: AsyncSession, document_version_id: UUID
) -> IngestionJob | None:
    """Find the job for a version.

    Unique per version, which is what lets a redelivered queue message resume
    the existing job instead of starting a second pipeline over the same
    document.
    """
    result = await session.execute(
        select(IngestionJob).where(IngestionJob.document_version_id == document_version_id)
    )
    return result.scalar_one_or_none()


async def list_jobs(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    status: JobStatus | None = None,
    document_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> Sequence[IngestionJob]:
    stmt = select(IngestionJob).where(IngestionJob.organization_id == tenant.organization_id)
    if status is not None:
        stmt = stmt.where(IngestionJob.status == status)
    if document_id is not None:
        stmt = stmt.where(IngestionJob.document_id == document_id)
    result = await session.execute(
        stmt.order_by(IngestionJob.created_at.desc()).limit(limit).offset(offset)
    )
    return result.scalars().all()


async def create_job(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    document_id: UUID,
    document_version_id: UUID,
    document_version: int,
    location_id: UUID | None,
    trace_id: str | None = None,
) -> IngestionJob:
    job = IngestionJob(
        organization_id=tenant.organization_id,
        location_id=location_id,
        document_id=document_id,
        document_version_id=document_version_id,
        document_version=document_version,
        status=JobStatus.QUEUED,
        current_stage=IngestionStage.UPLOADED,
        progress=IngestionStage.UPLOADED.progress,
        trace_id=trace_id,
    )
    session.add(job)
    await session.flush()
    return job


async def start_job(session: AsyncSession, job: IngestionJob, *, worker_id: str) -> IngestionJob:
    job.status = JobStatus.RUNNING
    job.worker_id = worker_id
    job.attempts += 1
    if job.started_at is None:
        job.started_at = datetime.now(UTC)
    await session.flush()
    return job


async def update_stage(
    session: AsyncSession,
    job: IngestionJob,
    stage: IngestionStage,
    *,
    progress: float | None = None,
    state: dict[str, Any] | None = None,
) -> IngestionJob:
    job.current_stage = stage
    job.progress = stage.progress if progress is None else progress
    if state:
        # Merge rather than replace: each stage adds its own outputs, and a
        # resumed run needs the ones written before the crash.
        job.state = {**(job.state or {}), **state}
    await session.flush()
    return job


async def complete_job(session: AsyncSession, job: IngestionJob) -> IngestionJob:
    job.status = JobStatus.COMPLETED
    job.current_stage = IngestionStage.COMPLETED
    job.progress = 1.0
    job.completed_at = datetime.now(UTC)
    job.error_message = None
    await session.flush()
    return job


async def fail_job(
    session: AsyncSession, job: IngestionJob, *, stage: IngestionStage, error: str
) -> IngestionJob:
    job.status = JobStatus.FAILED
    job.error_stage = stage
    job.error_message = error[:2000]
    job.completed_at = datetime.now(UTC)
    await session.flush()
    return job


async def add_event(
    session: AsyncSession,
    job: IngestionJob,
    *,
    stage: IngestionStage,
    status: str,
    message: str | None = None,
    duration_ms: float | None = None,
    detail: dict[str, Any] | None = None,
) -> IngestionJobEvent:
    """Append a stage transition.

    This is the record that answers "why did OCR run on this document?" and
    "which stage was slow?" months later, so the detail payload is worth
    populating even when nothing went wrong.
    """
    event = IngestionJobEvent(
        job_id=job.id,
        organization_id=job.organization_id,
        stage=stage,
        status=status,
        message=message[:1000] if message else None,
        duration_ms=duration_ms,
        detail=detail or {},
    )
    session.add(event)
    await session.flush()
    return event


async def list_events(
    session: AsyncSession, tenant: TenantContext, job_id: UUID
) -> Sequence[IngestionJobEvent]:
    result = await session.execute(
        select(IngestionJobEvent)
        .where(
            IngestionJobEvent.organization_id == tenant.organization_id,
            IngestionJobEvent.job_id == job_id,
        )
        .order_by(IngestionJobEvent.created_at)
    )
    return result.scalars().all()
