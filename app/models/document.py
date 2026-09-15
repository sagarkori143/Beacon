"""Documents and their versions.

A ``Document`` is the stable identity of a piece of knowledge ("Breakfast
Policy"). A ``DocumentVersion`` is one concrete upload of it. Exactly one version
per document may be ACTIVE, and that invariant is held by a partial unique index
rather than by application code.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import KnowledgeScope, SourceType, TextExtractionMode, VersionStatus
from app.models.base import Base, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    pass


class Document(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_org_location", "organization_id", "location_id"),
        UniqueConstraint("organization_id", "location_id", "slug"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # NULL means organization-wide knowledge shared by every location.
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("locations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    slug: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_type: Mapped[SourceType] = mapped_column(
        Enum(SourceType, name="source_type", native_enum=False, length=20),
        nullable=False,
        default=SourceType.PDF,
    )
    # Free-form classification used by routing ("legal", "hr", "safety") and by
    # metadata filters.
    document_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    language: Mapped[str] = mapped_column(String(16), nullable=False, default="en")
    tags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)

    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    versions: Mapped[list[DocumentVersion]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        lazy="raise",
        order_by="DocumentVersion.version_number",
    )

    @property
    def scope(self) -> KnowledgeScope:
        return KnowledgeScope.ORGANIZATION if self.location_id is None else KnowledgeScope.LOCATION


class DocumentVersion(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_number"),
        # At most one ACTIVE version per document, enforced by the database.
        # This is why activation demotes before it promotes: the index is not
        # deferrable, so the ordering matters.
        Index(
            "uq_document_versions_one_active",
            "document_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        Index("ix_document_versions_org_status", "organization_id", "status"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
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

    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[VersionStatus] = mapped_column(
        Enum(VersionStatus, name="version_status", native_enum=False, length=20),
        nullable=False,
        default=VersionStatus.PROCESSING,
        index=True,
    )

    # --- original file -----------------------------------------------------
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # --- extraction outcome ------------------------------------------------
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extraction_mode: Mapped[TextExtractionMode | None] = mapped_column(
        Enum(TextExtractionMode, name="text_extraction_mode", native_enum=False, length=16),
        nullable=True,
    )
    ocr_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ocr_page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ocr_mean_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Cleaned, structure-normalized text. Persisted so re-chunking with new
    # settings never has to re-parse or (far more expensively) re-OCR.
    normalized_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    document: Mapped[Document] = relationship(back_populates="versions", lazy="raise")

    @property
    def is_active(self) -> bool:
        return self.status is VersionStatus.ACTIVE
