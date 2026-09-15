"""Document upload and version management.

Upload returns as soon as the file is stored and the job is queued -- parsing,
OCR and embedding happen in a worker. A synchronous upload endpoint would hold
an HTTP connection open for the length of an OCR run.

Uploaded files are untrusted input, so validation happens before anything is
stored: the declared content type is checked against the file's actual magic
bytes, size is capped, and parsing never happens in this process.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from uuid import UUID

from app.core.config import Settings
from app.core.db import UnitOfWork
from app.core.enums import AuditAction, SourceType
from app.core.errors import (
    FileTooLarge,
    TenantScopeError,
    UnsupportedFileType,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.tenancy import Principal, TenantContext
from app.core.tracing import TraceContext
from app.models.document import Document, DocumentVersion
from app.providers.queue.base import QueueMessage
from app.providers.registry import ProviderBundle
from app.repositories import audit as audit_repo
from app.repositories import document as document_repo
from app.repositories import ingestion as job_repo
from app.repositories import organization as org_repo

log = get_logger(__name__)

#: Magic byte signatures, checked against the declared content type. A browser
#: will happily label anything application/pdf; the first bytes will not.
_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "application/pdf": (b"%PDF-",),
}

_SOURCE_TYPES: dict[str, SourceType] = {
    "application/pdf": SourceType.PDF,
    "text/plain": SourceType.TEXT,
    "text/markdown": SourceType.MARKDOWN,
}

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class UploadResult:
    document: Document
    version: DocumentVersion
    job_id: UUID
    created_document: bool


class DocumentService:
    def __init__(self, settings: Settings, providers: ProviderBundle) -> None:
        self.settings = settings
        self.providers = providers

    async def upload(
        self,
        uow: UnitOfWork,
        principal: Principal,
        *,
        data: bytes,
        filename: str,
        content_type: str,
        title: str | None = None,
        location_id: UUID | None = None,
        document_type: str | None = None,
        language: str = "en",
        description: str | None = None,
        trace: TraceContext | None = None,
        enqueue: bool = True,
    ) -> UploadResult:
        """Store a file, create or version a document, and queue processing.

        ``enqueue=False`` records the job without publishing it, for callers that
        drive the pipeline themselves. Doing both would have two runs processing
        the same version concurrently -- which the pipeline is built to survive,
        but which wastes the work and makes the outcome depend on their timing.
        """
        principal.require_admin()

        self._validate_upload(data, content_type=content_type, filename=filename)
        tenant = self._resolve_scope(principal, location_id)

        checksum = hashlib.sha256(data).hexdigest()
        source_type = _SOURCE_TYPES.get(content_type, SourceType.TEXT)
        resolved_title = (title or filename).strip()[:500]
        slug = slugify(resolved_title)

        async with uow.begin() as session:
            if tenant.location_id is not None:
                # Confirms the location exists and belongs to this organization.
                await org_repo.get_location(session, tenant, tenant.location_id)

            document = await document_repo.find_document_by_slug(
                session, tenant, slug=slug, location_id=tenant.location_id
            )
            created_document = document is None
            if document is None:
                document = await document_repo.create_document(
                    session,
                    tenant,
                    title=resolved_title,
                    slug=slug,
                    location_id=tenant.location_id,
                    source_type=source_type,
                    document_type=document_type,
                    language=language,
                    description=description,
                    created_by=principal.user_id,
                )

            version = await document_repo.create_version(
                session,
                tenant,
                document,
                storage_key="",  # set below, once the version number is known
                filename=filename[:512],
                content_type=content_type,
                size_bytes=len(data),
                checksum_sha256=checksum,
                uploaded_by=principal.user_id,
            )

            storage = self.providers.require_storage()
            version.storage_key = storage.key_for(
                organization_id=tenant.organization_id,
                document_id=document.id,
                version_number=version.version_number,
                filename=filename,
                checksum=checksum,
            )

            job = await job_repo.create_job(
                session,
                tenant,
                document_id=document.id,
                document_version_id=version.id,
                document_version=version.version_number,
                location_id=tenant.location_id,
                trace_id=trace.trace_id if trace else None,
            )

            await audit_repo.record(
                session,
                organization_id=tenant.organization_id,
                action=AuditAction.DOCUMENT_UPLOAD,
                actor_user_id=principal.user_id,
                location_id=tenant.location_id,
                resource_type="document_version",
                resource_id=version.id,
                trace_id=trace.trace_id if trace else None,
                detail={
                    "filename": filename[:200],
                    "size_bytes": len(data),
                    "version": version.version_number,
                },
            )

            storage_key = version.storage_key
            job_id = job.id
            version_id = version.id
            document_id = document.id

        # Store the file only after the transaction commits. If storage fails
        # the job is already recorded and will fail visibly with a clear reason,
        # which is better than an orphaned object with no row pointing at it.
        await self.providers.require_storage().put(
            storage_key,
            data,
            content_type=content_type,
            metadata={"organization": str(tenant.organization_id), "checksum": checksum},
        )

        if enqueue:
            await self.providers.require_queue().enqueue(
                QueueMessage(
                    job_id=job_id,
                    organization_id=tenant.organization_id,
                    document_id=document_id,
                    document_version_id=version_id,
                    trace_id=trace.trace_id if trace else None,
                )
            )

        log.info(
            "document_uploaded",
            document_id=str(document_id),
            version_id=str(version_id),
            job_id=str(job_id),
            new_document=created_document,
        )
        return UploadResult(
            document=document,
            version=version,
            job_id=job_id,
            created_document=created_document,
        )

    async def rollback(
        self,
        uow: UnitOfWork,
        principal: Principal,
        *,
        version_id: UUID,
        trace: TraceContext | None = None,
    ) -> DocumentVersion:
        """Re-activate a superseded version.

        Uses the same transaction as a forward activation, so rollback cannot
        develop behaviour of its own that diverges from the tested path.
        """
        principal.require_admin()
        tenant = principal.tenant

        async with uow.begin() as session:
            activated, previous = await document_repo.rollback_to_version(
                session, tenant, version_id
            )
            await audit_repo.record(
                session,
                organization_id=tenant.organization_id,
                action=AuditAction.VERSION_ROLLBACK,
                actor_user_id=principal.user_id,
                resource_type="document_version",
                resource_id=version_id,
                trace_id=trace.trace_id if trace else None,
                detail={
                    "activated": activated.version_number,
                    "replaced": previous.version_number if previous else None,
                },
            )
            return activated

    # -- validation ----------------------------------------------------------

    def _validate_upload(self, data: bytes, *, content_type: str, filename: str) -> None:
        if not data:
            raise ValidationError("Uploaded file is empty")

        limit = self.settings.storage.max_upload_bytes
        if len(data) > limit:
            raise FileTooLarge(
                f"File is {len(data):,} bytes; the limit is {limit:,} bytes",
                details={"size_bytes": len(data), "limit_bytes": limit},
            )

        normalized = (content_type or "").split(";")[0].strip().lower()
        if normalized not in self.settings.storage.allowed_mime_types:
            raise UnsupportedFileType(
                f"'{normalized or 'unknown'}' is not an accepted file type. "
                f"Accepted: {', '.join(self.settings.storage.allowed_mime_types)}",
                details={"content_type": normalized},
            )

        # A declared content type is a claim by the client; the bytes are not.
        signatures = _SIGNATURES.get(normalized)
        if signatures and not any(data.startswith(sig) for sig in signatures):
            raise UnsupportedFileType(
                f"File content does not match the declared type '{normalized}'.",
                details={"declared": normalized, "filename": filename[:100]},
            )

        if normalized.startswith("text/"):
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise UnsupportedFileType("Text uploads must be valid UTF-8.") from exc

    @staticmethod
    def _resolve_scope(principal: Principal, location_id: UUID | None) -> TenantContext:
        """Decide the scope of an upload.

        An admin pinned to a location may only upload for that location; an
        unpinned admin may target any location in their organization, or none at
        all for organization-wide knowledge. The organization always comes from
        the token.
        """
        if location_id is None:
            return TenantContext(organization_id=principal.organization_id)
        if principal.location_id is not None and principal.location_id != location_id:
            raise TenantScopeError("You may only upload documents for your own location")
        return TenantContext(organization_id=principal.organization_id, location_id=location_id)


def slugify(value: str, *, max_len: int = 200) -> str:
    """Stable identifier for a document title.

    Uploading a new file with the same title creates a new *version* rather than
    a second document, which is how "upload v4 of the handbook" works without
    asking the user to find the document id first.
    """
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    slug = _SLUG_STRIP.sub("-", normalized.lower()).strip("-")
    return (slug or "document")[:max_len]
