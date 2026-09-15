"""Document and version queries, and the atomic activation transaction.

Activation is the correctness core of versioning. Four mechanisms work together
and none of them is optional:

1. **Chunks are written inactive.** A worker that dies at 80% of EMBEDDING
   leaves rows behind that no search can see, because visibility is a column.
   There is no cleanup path that can be forgotten.
2. **The parent document row is locked** with ``SELECT ... FOR UPDATE``. Two
   concurrent activations of the same document serialize on it, under a single
   memorable rule: always take the document lock first.
3. **Demote before promote.** The partial unique index that guarantees at most
   one ACTIVE version per document is not deferrable, so the old version must
   leave the ACTIVE state before the new one enters it.
4. **The promote is conditional** on the version's current status. A redelivered
   job therefore matches zero rows and verifies instead of corrupting, and the
   same statement serves rollback with no second code path.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import SourceType, VersionStatus
from app.core.errors import ConflictError
from app.core.logging import get_logger
from app.core.tenancy import TenantContext
from app.models.document import Document, DocumentVersion
from app.repositories.base import assert_tenant, require

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


async def get_document(session: AsyncSession, tenant: TenantContext, document_id: UUID) -> Document:
    document = await session.get(Document, document_id)
    document = require(document, what="Document", identifier=document_id)
    assert_tenant(document, tenant)
    return document


async def list_documents(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    location_id: UUID | None = None,
    include_org_scope: bool = True,
    document_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> Sequence[Document]:
    stmt = select(Document).where(
        Document.organization_id == tenant.organization_id,
        Document.is_deleted.is_(False),
    )
    if location_id is not None:
        stmt = (
            stmt.where((Document.location_id == location_id) | (Document.location_id.is_(None)))
            if include_org_scope
            else stmt.where(Document.location_id == location_id)
        )
    if document_type:
        stmt = stmt.where(Document.document_type == document_type)

    result = await session.execute(
        stmt.order_by(Document.created_at.desc()).limit(limit).offset(offset)
    )
    return result.scalars().all()


async def count_documents(
    session: AsyncSession, tenant: TenantContext, *, location_id: UUID | None = None
) -> int:
    stmt = select(func.count(Document.id)).where(
        Document.organization_id == tenant.organization_id,
        Document.is_deleted.is_(False),
    )
    if location_id is not None:
        stmt = stmt.where((Document.location_id == location_id) | (Document.location_id.is_(None)))
    return int((await session.execute(stmt)).scalar_one())


async def create_document(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    title: str,
    slug: str,
    location_id: UUID | None,
    source_type: SourceType,
    document_type: str | None = None,
    language: str = "en",
    description: str | None = None,
    created_by: UUID | None = None,
) -> Document:
    document = Document(
        organization_id=tenant.organization_id,
        location_id=location_id,
        title=title,
        slug=slug,
        description=description,
        source_type=source_type,
        document_type=document_type,
        language=language,
        created_by=created_by,
    )
    session.add(document)
    await session.flush()
    return document


async def find_document_by_slug(
    session: AsyncSession, tenant: TenantContext, *, slug: str, location_id: UUID | None
) -> Document | None:
    stmt = select(Document).where(
        Document.organization_id == tenant.organization_id,
        Document.slug == slug,
        Document.is_deleted.is_(False),
    )
    stmt = (
        stmt.where(Document.location_id.is_(None))
        if location_id is None
        else stmt.where(Document.location_id == location_id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


async def get_version(
    session: AsyncSession, tenant: TenantContext, version_id: UUID
) -> DocumentVersion:
    version = await session.get(DocumentVersion, version_id)
    version = require(version, what="Document version", identifier=version_id)
    assert_tenant(version, tenant)
    return version


async def list_versions(
    session: AsyncSession, tenant: TenantContext, document_id: UUID
) -> Sequence[DocumentVersion]:
    result = await session.execute(
        select(DocumentVersion)
        .where(
            DocumentVersion.organization_id == tenant.organization_id,
            DocumentVersion.document_id == document_id,
        )
        .order_by(DocumentVersion.version_number.desc())
    )
    return result.scalars().all()


async def get_active_version(
    session: AsyncSession, tenant: TenantContext, document_id: UUID
) -> DocumentVersion | None:
    result = await session.execute(
        select(DocumentVersion).where(
            DocumentVersion.organization_id == tenant.organization_id,
            DocumentVersion.document_id == document_id,
            DocumentVersion.status == VersionStatus.ACTIVE,
        )
    )
    return result.scalar_one_or_none()


async def next_version_number(session: AsyncSession, document_id: UUID) -> int:
    result = await session.execute(
        select(func.coalesce(func.max(DocumentVersion.version_number), 0)).where(
            DocumentVersion.document_id == document_id
        )
    )
    return int(result.scalar_one()) + 1


async def create_version(
    session: AsyncSession,
    tenant: TenantContext,
    document: Document,
    *,
    storage_key: str,
    filename: str,
    content_type: str,
    size_bytes: int,
    checksum_sha256: str,
    uploaded_by: UUID | None = None,
) -> DocumentVersion:
    """Create the next version of a document, in PROCESSING state.

    The document row is locked first so two concurrent uploads cannot compute
    the same next version number and collide on the unique constraint.
    """
    await session.execute(select(Document.id).where(Document.id == document.id).with_for_update())

    version = DocumentVersion(
        document_id=document.id,
        organization_id=tenant.organization_id,
        location_id=document.location_id,
        version_number=await next_version_number(session, document.id),
        status=VersionStatus.PROCESSING,
        storage_key=storage_key,
        filename=filename,
        content_type=content_type,
        size_bytes=size_bytes,
        checksum_sha256=checksum_sha256,
        uploaded_by=uploaded_by,
    )
    session.add(version)
    await session.flush()
    return version


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------


async def activate_version(
    session: AsyncSession,
    tenant: TenantContext,
    version_id: UUID,
    *,
    chunk_count: int | None = None,
) -> tuple[DocumentVersion, DocumentVersion | None]:
    """Atomically make ``version_id`` the active version of its document.

    Returns the newly active version and the one it replaced (if any). Must be
    called inside a transaction; everything below commits or rolls back
    together, so a failure anywhere leaves the previous version serving.
    """
    version = await get_version(session, tenant, version_id)

    # 1. Lock the parent document. Every activation for this document now
    #    serializes here, which is what makes the read-then-write below safe.
    await session.execute(
        select(Document.id).where(Document.id == version.document_id).with_for_update()
    )

    if version.status is VersionStatus.ACTIVE:
        # Idempotent: a redelivered job finds the work already done.
        log.info("version_already_active", version_id=str(version_id))
        return version, None

    if version.status is VersionStatus.FAILED:
        raise ConflictError(
            f"Version {version.version_number} failed processing and cannot be activated"
        )

    now = datetime.now(UTC)

    # 2. Demote the incumbent. This must happen before the promote: the partial
    #    unique index on (document_id) WHERE status='ACTIVE' is not deferrable.
    previous = await get_active_version(session, tenant, version.document_id)
    if previous is not None and previous.id != version.id:
        await session.execute(
            update(DocumentVersion)
            .where(DocumentVersion.id == previous.id)
            .values(status=VersionStatus.INACTIVE, deactivated_at=now)
        )
        from app.repositories.chunk import set_chunks_active

        await set_chunks_active(session, previous.id, active=False)

    # 3. Promote, conditionally. Matching zero rows means someone else already
    #    moved this version, and that is not an error -- it is the retry path.
    result = await session.execute(
        update(DocumentVersion)
        .where(
            DocumentVersion.id == version.id,
            DocumentVersion.status.in_([VersionStatus.PROCESSING, VersionStatus.INACTIVE]),
        )
        .values(
            status=VersionStatus.ACTIVE,
            activated_at=now,
            error_message=None,
            **({"chunk_count": chunk_count} if chunk_count is not None else {}),
        )
    )
    if (result.rowcount or 0) == 0:
        await session.refresh(version)
        if version.status is not VersionStatus.ACTIVE:
            raise ConflictError(
                f"Version {version.version_number} could not be activated from "
                f"state {version.status.value}"
            )

    # 4. Make its chunks visible. Same transaction, so search sees the version
    #    and its chunks appear at exactly the same instant.
    from app.repositories.chunk import set_chunks_active

    await set_chunks_active(session, version.id, active=True)
    await session.refresh(version)

    log.info(
        "version_activated",
        document_id=str(version.document_id),
        version=version.version_number,
        replaced=previous.version_number if previous else None,
    )
    return version, previous


async def rollback_to_version(
    session: AsyncSession, tenant: TenantContext, version_id: UUID
) -> tuple[DocumentVersion, DocumentVersion | None]:
    """Re-activate a previously superseded version.

    Uses the identical path as a forward activation -- the conditional promote
    already accepts INACTIVE as a source state, so rollback needs no separate
    logic that could drift from the main one.
    """
    return await activate_version(session, tenant, version_id)


async def mark_version_failed(
    session: AsyncSession, tenant: TenantContext, version_id: UUID, error: str
) -> DocumentVersion:
    """Record a failure. Deliberately does not touch the active version."""
    version = await get_version(session, tenant, version_id)
    await session.execute(
        update(DocumentVersion)
        .where(DocumentVersion.id == version_id)
        .values(
            status=VersionStatus.FAILED,
            failed_at=datetime.now(UTC),
            error_message=error[:2000],
        )
    )
    await session.refresh(version)
    return version
