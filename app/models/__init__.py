"""SQLAlchemy models.

Imported as a package so Alembic autogenerate and `Base.metadata` see every
table. Import order matters only for relationship resolution, which SQLAlchemy
defers until first use.
"""

from app.models.audit import AuditLog
from app.models.base import Base
from app.models.chunk import Chunk, EmbeddingSpace
from app.models.conversation import Conversation, ConversationMessage
from app.models.document import Document, DocumentVersion
from app.models.ingestion import IngestionJob, IngestionJobEvent
from app.models.organization import Location, Organization
from app.models.user import User, UserDirectory

__all__ = [
    "AuditLog",
    "Base",
    "Chunk",
    "Conversation",
    "ConversationMessage",
    "Document",
    "DocumentVersion",
    "EmbeddingSpace",
    "IngestionJob",
    "IngestionJobEvent",
    "Location",
    "Organization",
    "User",
    "UserDirectory",
]

#: Tables that carry tenant data and therefore must have RLS enabled+forced.
#: `tests/integration/test_rls.py` enumerates this list rather than spot-checking.
TENANT_TABLES: tuple[str, ...] = (
    "locations",
    "users",
    "documents",
    "document_versions",
    "chunks",
    "ingestion_jobs",
    "ingestion_job_events",
    "audit_log",
    "conversations",
    "conversation_messages",
)

#: Tables deliberately outside tenant scoping, with the reason.
GLOBAL_TABLES: dict[str, str] = {
    "organizations": "the tenant roots themselves; policy restricts to own row",
    "embedding_spaces": "vector-space registry, contains no tenant data",
    "user_directory": "minimal email -> organization map needed to resolve login",
    "alembic_version": "migration bookkeeping",
}
