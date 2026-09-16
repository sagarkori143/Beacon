"""Ingestion jobs and their stage history."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import IngestionStage, JobStatus
from app.models.base import Base, TimestampMixin, UUIDMixin


class IngestionJob(UUIDMixin, TimestampMixin, Base):
    """One run of the pipeline for one document version.

    Unique on ``document_version_id`` so a redelivered queue message finds the
    existing job and resumes rather than starting a duplicate pipeline.
    """

    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        UniqueConstraint("document_version_id"),
        Index("ix_ingestion_jobs_org_status", "organization_id", "status"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("locations.id", ondelete="CASCADE"), nullable=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status", native_enum=False, length=20),
        nullable=False,
        default=JobStatus.QUEUED,
        index=True,
    )
    current_stage: Mapped[IngestionStage] = mapped_column(
        Enum(IngestionStage, name="ingestion_stage", native_enum=False, length=20),
        nullable=False,
        default=IngestionStage.UPLOADED,
    )
    progress: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_stage: Mapped[IngestionStage | None] = mapped_column(
        Enum(IngestionStage, name="ingestion_stage", native_enum=False, length=20), nullable=True
    )

    # Correlates the ingestion trace back to the upload request that created it.
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Stage outputs carried between stages on resume (extraction decision,
    # chunk counts, timings). Never raw document text.
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    events: Mapped[list[IngestionJobEvent]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        lazy="raise",
        order_by="IngestionJobEvent.created_at",
    )

    @property
    def is_terminal(self) -> bool:
        return self.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)


class IngestionJobEvent(UUIDMixin, Base):
    """One stage transition. The audit trail for "why did this take so long?"
    and "why did OCR run on this document?"."""

    __tablename__ = "ingestion_job_events"
    __table_args__ = (Index("ix_ingestion_job_events_job", "job_id", "created_at"),)

    job_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("ingestion_jobs.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    stage: Mapped[IngestionStage] = mapped_column(
        Enum(IngestionStage, name="ingestion_stage", native_enum=False, length=20), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    job: Mapped[IngestionJob] = relationship(back_populates="events", lazy="raise")
