"""Chunks and embedding spaces -- the retrieval substrate.

``Chunk`` is the hardest table in the system to change later, because its column
set determines whether filtered ANN search, lexical search, hierarchical
override and deduplication all work. Two decisions are load-bearing:

* ``organization_id`` / ``location_id`` are **denormalized** onto the row, so
  every tenant filter is one indexed predicate rather than a join.
* ``is_active`` gates visibility. Chunks are written ``false`` during ingestion
  and flipped in the same transaction that activates their version, so a worker
  that crashes mid-pipeline leaves nothing visible -- by construction, not by
  cleanup.
"""

from __future__ import annotations

import uuid
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import get_settings
from app.core.enums import SourceType
from app.models.base import Base, TimestampMixin, UUIDMixin

# The vector column's dimension is fixed at migration time from configuration.
# It is not free to drift: `EmbeddingSpace` plus the startup guard in
# `app.providers.registry` refuse to run if the live provider disagrees.
EMBEDDING_DIM = get_settings().embedding_dim


class EmbeddingSpace(UUIDMixin, TimestampMixin, Base):
    """A (model, dimension) pair that vectors are only comparable within.

    Without this table, changing ``EMBEDDING_MODEL`` to another model of the same
    dimension writes vectors from an incompatible space into the same column and
    silently destroys retrieval quality -- no error, no crash, just worse
    answers. Every chunk is stamped with its space, every search filters on the
    current one, and the boot guard compares all three of: the column's declared
    dimension, the current space, and what the live provider actually returns.
    """

    __tablename__ = "embedding_spaces"
    __table_args__ = (
        UniqueConstraint("provider_type", "model", "dimension"),
        # At most one current space at a time.
        Index(
            "uq_embedding_spaces_current",
            "is_current",
            unique=True,
            postgresql_where=text("is_current"),
        ),
    )

    provider_type: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    normalized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    def describe(self) -> str:
        return f"{self.provider_type}:{self.model}({self.dimension}d)"


class Chunk(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("document_version_id", "ordinal"),
        # Hot path: tenant + visibility + scope, in the order the planner wants.
        Index("ix_chunks_tenant_active", "organization_id", "is_active", "location_id"),
        Index("ix_chunks_version", "document_version_id"),
        Index("ix_chunks_topic", "organization_id", "topic_key"),
        # Lexical arm. GIN pre-filters correctly, which makes it the recall floor
        # when the ANN arm loses rows to tenant post-filtering.
        Index(
            "ix_chunks_search_vector",
            "search_vector",
            postgresql_using="gin",
        ),
        # Semantic arm. Partial on is_active so the graph holds only live rows:
        # a smaller graph both speeds traversal and reduces how much of it is
        # discarded by the tenant predicate afterwards.
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_where=text("is_active"),
        ),
    )

    # --- tenancy (denormalized on purpose) ---------------------------------
    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("locations.id", ondelete="CASCADE"), nullable=True
    )

    # --- provenance --------------------------------------------------------
    document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    source_type: Mapped[SourceType] = mapped_column(
        Enum(SourceType, name="source_type", native_enum=False, length=20), nullable=False
    )
    source_name: Mapped[str] = mapped_column(String(512), nullable=False)
    page_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_to: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- structure ---------------------------------------------------------
    heading: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Ordered breadcrumb, e.g. ["Front Desk SOP", "Check-in", "Late arrival"].
    section_path: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    #: Stable hash of the breadcrumb; identifies "the same section".
    section_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Normalized subject used to detect that a location chunk overrides an
    #: organization chunk, without asking an LLM. See services/retrieval.
    topic_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="text")

    # --- content -----------------------------------------------------------
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    language: Mapped[str] = mapped_column(String(16), nullable=False, default="en")

    # --- retrieval ---------------------------------------------------------
    embedding_space_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("embedding_spaces.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    #: Built as setweight(heading,'A') || setweight(content,'B') with the
    #: language's text-search configuration, at INDEXING time.
    search_vector: Mapped[Any | None] = mapped_column(TSVECTOR, nullable=True)

    #: False until the owning version is activated. See module docstring.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    @property
    def breadcrumb(self) -> str:
        return " > ".join(self.section_path) if self.section_path else ""

    @property
    def citation_label(self) -> str:
        pages = ""
        if self.page_from is not None:
            pages = (
                f" p.{self.page_from}"
                if self.page_to in (None, self.page_from)
                else f" pp.{self.page_from}-{self.page_to}"
            )
        return f"{self.source_name}{pages}"
