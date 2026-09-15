"""Document, version and ingestion-job models."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.core.enums import IngestionStage, JobStatus, VersionStatus
from app.schemas.common import ORMModel


class DocumentOut(ORMModel):
    id: UUID
    organization_id: UUID
    location_id: UUID | None
    title: str
    slug: str
    description: str | None
    source_type: str
    document_type: str | None
    language: str
    created_at: datetime

    @property
    def scope(self) -> str:
        return "ORGANIZATION" if self.location_id is None else "LOCATION"


class DocumentDetail(DocumentOut):
    versions: list[VersionOut] = Field(default_factory=list)
    active_version: int | None = None


class VersionOut(ORMModel):
    id: UUID
    document_id: UUID
    version_number: int
    status: VersionStatus
    filename: str
    content_type: str
    size_bytes: int
    checksum_sha256: str
    page_count: int | None
    chunk_count: int
    ocr_used: bool
    ocr_page_count: int
    extraction_mode: str | None
    error_message: str | None
    created_at: datetime
    activated_at: datetime | None


class UploadResponse(BaseModel):
    """Returned immediately; processing continues in a worker."""

    document_id: UUID
    version_id: UUID
    version_number: int
    job_id: UUID
    status: str = "QUEUED"
    message: str = "Upload accepted. Track progress with GET /ingestion/jobs/{job_id}."


class JobOut(ORMModel):
    id: UUID
    organization_id: UUID
    document_id: UUID
    document_version_id: UUID
    document_version: int
    status: JobStatus
    current_stage: IngestionStage
    progress: float
    attempts: int
    error_message: str | None
    error_stage: IngestionStage | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class JobEventOut(ORMModel):
    id: UUID
    stage: IngestionStage
    status: str
    message: str | None
    duration_ms: float | None
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


DocumentDetail.model_rebuild()
