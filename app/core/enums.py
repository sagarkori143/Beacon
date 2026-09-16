"""Enumerations shared across the domain.

These are the vocabulary of the system. They live in `core` rather than `models`
so that services and providers can depend on them without importing SQLAlchemy.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    """Authorization role carried on the JWT."""

    USER = "USER"
    ADMIN = "ADMIN"


class KnowledgeScope(StrEnum):
    """Which level of the tenant hierarchy a piece of knowledge belongs to.

    Ordered from most general to most specific; `precedence` reflects that so
    additional levels (region, brand, ...) can be inserted later without
    touching the merge algorithm in `services.retrieval.hierarchical`.
    """

    ORGANIZATION = "ORGANIZATION"
    LOCATION = "LOCATION"

    @property
    def precedence(self) -> int:
        return _SCOPE_PRECEDENCE[self]


_SCOPE_PRECEDENCE: dict[KnowledgeScope, int] = {
    KnowledgeScope.ORGANIZATION: 0,
    KnowledgeScope.LOCATION: 100,
}


class VersionStatus(StrEnum):
    """Lifecycle of a single document version.

    Exactly one version per document may be ACTIVE; that is enforced by a
    partial unique index, not by application code.
    """

    PROCESSING = "PROCESSING"
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    FAILED = "FAILED"


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class IngestionStage(StrEnum):
    """Stages of the ingestion pipeline, in execution order."""

    UPLOADED = "UPLOADED"
    PARSING = "PARSING"
    OCR = "OCR"
    CLEANING = "CLEANING"
    CHUNKING = "CHUNKING"
    EMBEDDING = "EMBEDDING"
    INDEXING = "INDEXING"
    VALIDATING = "VALIDATING"
    ACTIVATING = "ACTIVATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

    @property
    def progress(self) -> float:
        """Fraction of the pipeline complete once this stage finishes."""
        return _STAGE_PROGRESS[self]


_STAGE_PROGRESS: dict[IngestionStage, float] = {
    IngestionStage.UPLOADED: 0.05,
    IngestionStage.PARSING: 0.20,
    IngestionStage.OCR: 0.35,
    IngestionStage.CLEANING: 0.45,
    IngestionStage.CHUNKING: 0.55,
    IngestionStage.EMBEDDING: 0.80,
    IngestionStage.INDEXING: 0.90,
    IngestionStage.VALIDATING: 0.95,
    IngestionStage.ACTIVATING: 0.99,
    IngestionStage.COMPLETED: 1.0,
    IngestionStage.FAILED: 1.0,
}

#: Stages the worker runs, in order. UPLOADED is set by the API; the terminal
#: states are not "run" so they are excluded.
PIPELINE_STAGES: tuple[IngestionStage, ...] = (
    IngestionStage.PARSING,
    IngestionStage.OCR,
    IngestionStage.CLEANING,
    IngestionStage.CHUNKING,
    IngestionStage.EMBEDDING,
    IngestionStage.INDEXING,
    IngestionStage.VALIDATING,
    IngestionStage.ACTIVATING,
)


class SourceType(StrEnum):
    """Where a chunk's text originally came from. Extensible to web/csv/api."""

    PDF = "PDF"
    IMAGE = "IMAGE"
    TEXT = "TEXT"
    MARKDOWN = "MARKDOWN"
    HTML = "HTML"
    CSV = "CSV"
    WEB = "WEB"
    API = "API"


class TextExtractionMode(StrEnum):
    """Outcome of the "does this PDF already have usable text?" assessment."""

    NATIVE = "NATIVE"
    OCR = "OCR"
    HYBRID = "HYBRID"


class ModelTier(StrEnum):
    """Coarse quality/cost band a model sits in. Declared per model in config."""

    FAST = "fast"
    BALANCED = "balanced"
    QUALITY = "quality"


class Privacy(StrEnum):
    """Where inference physically happens. Used by routing policy, never inferred."""

    LOCAL = "local"
    CLOUD = "cloud"


class TaskKind(StrEnum):
    """What the agent is asking a model to do. Drives routing."""

    PLAN = "plan"
    CLASSIFY = "classify"
    REWRITE = "rewrite"
    ANSWER = "answer"
    SUMMARIZE = "summarize"
    TOOL_TURN = "tool_turn"
    TITLE = "title"


class FusionStrategy(StrEnum):
    RRF = "rrf"
    WEIGHTED = "weighted"


class AuditAction(StrEnum):
    LOGIN_SUCCESS = "LOGIN_SUCCESS"
    LOGIN_FAILURE = "LOGIN_FAILURE"
    DOCUMENT_UPLOAD = "DOCUMENT_UPLOAD"
    DOCUMENT_DELETE = "DOCUMENT_DELETE"
    VERSION_ACTIVATE = "VERSION_ACTIVATE"
    VERSION_ROLLBACK = "VERSION_ROLLBACK"
    VERSION_FAIL = "VERSION_FAIL"
    TOOL_DENIED = "TOOL_DENIED"
    CHAT = "CHAT"
    # Platform-operator actions. Recorded against the organization they touch,
    # so a tenant's audit trail shows provisioning done on their behalf.
    ORGANIZATION_CREATE = "ORGANIZATION_CREATE"
    ORGANIZATION_UPDATE = "ORGANIZATION_UPDATE"
    LOCATION_CREATE = "LOCATION_CREATE"
    USER_CREATE = "USER_CREATE"
    USER_DISABLE = "USER_DISABLE"
    USER_ENABLE = "USER_ENABLE"
    USER_UPDATE = "USER_UPDATE"
    USER_PASSWORD_RESET = "USER_PASSWORD_RESET"
    USER_PASSWORD_CHANGE = "USER_PASSWORD_CHANGE"
    LOCATION_UPDATE = "LOCATION_UPDATE"
    LOCATION_DISABLE = "LOCATION_DISABLE"
    LOCATION_ENABLE = "LOCATION_ENABLE"
    DOCUMENT_UPDATE = "DOCUMENT_UPDATE"
    DOCUMENT_RESTORE = "DOCUMENT_RESTORE"
