"""Document upload and version endpoints.

Upload is asynchronous by design: the file is stored, a job is queued, and the
response returns. Parsing, OCR and embedding happen in a worker, and the caller
follows progress through the ingestion endpoints.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status
from fastapi.responses import StreamingResponse

from app.api.deps import (
    CurrentAdmin,
    CurrentPrincipal,
    Providers,
    Trace,
    Uow,
    get_document_service,
)
from app.core.enums import AuditAction, VersionStatus
from app.core.errors import FileTooLarge, ValidationError
from app.repositories import audit as audit_repo
from app.repositories import document as document_repo
from app.schemas.common import Page
from app.schemas.document import (
    ArchiveResult,
    DocumentDetail,
    DocumentOut,
    DocumentUpdate,
    UploadResponse,
    VersionOut,
)
from app.services.documents.service import DocumentService

router = APIRouter(prefix="/documents", tags=["documents"])


@router.post("", response_model=UploadResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    admin: CurrentAdmin,
    uow: Uow,
    trace: Trace,
    file: UploadFile = File(description="PDF, plain text or markdown."),
    title: str | None = Form(default=None),
    location_id: UUID | None = Form(
        default=None,
        description="Scope to one location. Omit for organization-wide knowledge.",
    ),
    document_type: str | None = Form(default=None),
    language: str = Form(default="en"),
    description: str | None = Form(default=None),
    service: DocumentService = Depends(get_document_service),
) -> UploadResponse:
    """Upload a document, or a new version of an existing one.

    A file whose title slug matches an existing document becomes the next
    version of it, rather than a second document. The previous version keeps
    serving every query until the new one passes validation.
    """
    data = await _read_upload(file, limit=service.settings.storage.max_upload_bytes)

    result = await service.upload(
        uow,
        admin,
        data=data,
        filename=file.filename or "upload",
        content_type=file.content_type or "application/octet-stream",
        title=title,
        location_id=location_id,
        document_type=document_type,
        language=language,
        description=description,
        trace=trace,
    )
    return UploadResponse(
        document_id=result.document.id,
        version_id=result.version.id,
        version_number=result.version.version_number,
        job_id=result.job_id,
    )


@router.get("", response_model=Page[DocumentOut])
async def list_documents(
    principal: CurrentPrincipal,
    uow: Uow,
    location_id: UUID | None = None,
    document_type: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[DocumentOut]:
    """The knowledge this caller can see.

    Each document carries a `scope` of ORGANIZATION or LOCATION, which is the
    difference between "every branch" and "this one".
    """
    tenant = principal.tenant.narrowed_to(location_id)
    async with uow.begin() as session:
        documents = await document_repo.list_documents(
            session,
            tenant,
            location_id=tenant.location_id,
            document_type=document_type,
            limit=limit,
            offset=offset,
        )
        total = await document_repo.count_documents(
            session,
            tenant,
            location_id=tenant.location_id,
            document_type=document_type,
        )
    return Page[DocumentOut](
        items=[DocumentOut.model_validate(doc) for doc in documents],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{document_id}", response_model=DocumentDetail)
async def get_document(document_id: UUID, principal: CurrentPrincipal, uow: Uow) -> DocumentDetail:
    async with uow.begin() as session:
        document = await document_repo.get_document(session, principal.tenant, document_id)
        versions = await document_repo.list_versions(session, principal.tenant, document_id)

        detail = DocumentDetail.model_validate(document)
        detail.versions = [VersionOut.model_validate(v) for v in versions]
        detail.active_version = next(
            (v.version_number for v in versions if v.status is VersionStatus.ACTIVE), None
        )
        return detail


@router.get("/{document_id}/versions", response_model=list[VersionOut])
async def list_versions(
    document_id: UUID, principal: CurrentPrincipal, uow: Uow
) -> list[VersionOut]:
    async with uow.begin() as session:
        await document_repo.get_document(session, principal.tenant, document_id)
        versions = await document_repo.list_versions(session, principal.tenant, document_id)
        return [VersionOut.model_validate(v) for v in versions]


@router.post(
    "/{document_id}/versions",
    response_model=UploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_version(
    document_id: UUID,
    admin: CurrentAdmin,
    uow: Uow,
    trace: Trace,
    file: UploadFile = File(...),
    service: DocumentService = Depends(get_document_service),
) -> UploadResponse:
    """Upload a new version of a specific document."""
    async with uow.begin() as session:
        document = await document_repo.get_document(session, admin.tenant, document_id)
        title, location_id = document.title, document.location_id
        document_type, language = document.document_type, document.language

    data = await _read_upload(file, limit=service.settings.storage.max_upload_bytes)
    result = await service.upload(
        uow,
        admin,
        data=data,
        filename=file.filename or "upload",
        content_type=file.content_type or "application/octet-stream",
        title=title,
        location_id=location_id,
        document_type=document_type,
        language=language,
        trace=trace,
    )
    return UploadResponse(
        document_id=result.document.id,
        version_id=result.version.id,
        version_number=result.version.version_number,
        job_id=result.job_id,
    )


@router.post("/versions/{version_id}/activate", response_model=VersionOut)
async def rollback_to_version(
    version_id: UUID,
    admin: CurrentAdmin,
    uow: Uow,
    trace: Trace,
    service: DocumentService = Depends(get_document_service),
) -> VersionOut:
    """Re-activate a previously superseded version.

    Runs the same atomic activation as the pipeline, so a rollback is exactly as
    safe as a forward activation.
    """
    version = await service.rollback(uow, admin, version_id=version_id, trace=trace)
    return VersionOut.model_validate(version)


async def _read_upload(file: UploadFile, *, limit: int) -> bytes:
    """Read an upload, refusing anything over the limit.

    Read in bounded chunks and stop at the cap: reading the whole body first and
    checking the size afterwards is how a single request exhausts the process's
    memory.
    """
    if not file.filename:
        raise ValidationError("No file was provided")

    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(1 << 20):
        total += len(chunk)
        if total > limit:
            raise FileTooLarge(
                f"File exceeds the {limit:,} byte limit",
                details={"limit_bytes": limit},
            )
        chunks.append(chunk)
    await file.close()
    return b"".join(chunks)


@router.patch("/{document_id}", response_model=DocumentOut)
async def update_document(
    document_id: UUID, payload: DocumentUpdate, admin: CurrentAdmin, uow: Uow
) -> DocumentOut:
    """Edit a document's title, description, type or language.

    Its branch is fixed at upload. Scope is stamped on every chunk so filtering
    never needs a join, which makes moving a document between branches a
    re-ingest rather than an edit -- upload it again under the right scope.
    """
    from app.repositories.document import update_document as repo_update

    changes = payload.model_dump(exclude_unset=True)
    async with uow.begin() as session:
        document = await repo_update(session, admin.tenant, document_id, changes)
        if changes:
            await audit_repo.record(
                session,
                organization_id=admin.organization_id,
                action=AuditAction.DOCUMENT_UPDATE,
                actor_user_id=admin.user_id,
                resource_type="document",
                resource_id=document.id,
                message=f"{', '.join(sorted(changes))} changed",
            )
        return DocumentOut.model_validate(document)


@router.post("/{document_id}/archive", response_model=ArchiveResult)
async def archive_document(document_id: UUID, admin: CurrentAdmin, uow: Uow) -> ArchiveResult:
    """Take a document out of service, reversibly.

    It leaves the library and, more importantly, **stops answering questions**:
    every chunk of every version is deactivated in the same transaction. Nothing
    is destroyed -- versions, chunks and the stored original all remain, and
    `restore` brings the document back.
    """
    from app.repositories.document import archive_document as repo_archive

    async with uow.begin() as session:
        document, withdrawn = await repo_archive(session, admin.tenant, document_id)
        await audit_repo.record(
            session,
            organization_id=admin.organization_id,
            action=AuditAction.DOCUMENT_DELETE,
            actor_user_id=admin.user_id,
            resource_type="document",
            resource_id=document.id,
            message=f"archived; {withdrawn} chunks withdrawn",
        )
        return ArchiveResult(
            document=DocumentOut.model_validate(document), chunks_withdrawn=withdrawn
        )


@router.post("/{document_id}/restore", response_model=DocumentOut)
async def restore_document(document_id: UUID, admin: CurrentAdmin, uow: Uow) -> DocumentOut:
    """Bring an archived document back into the library.

    Every version comes back **inactive**. Choosing which one should answer
    questions is a separate, deliberate act -- activate it and the validation
    gates run, the same as for any other version.
    """
    from app.repositories.document import restore_document as repo_restore

    async with uow.begin() as session:
        document = await repo_restore(session, admin.tenant, document_id)
        await audit_repo.record(
            session,
            organization_id=admin.organization_id,
            action=AuditAction.DOCUMENT_RESTORE,
            actor_user_id=admin.user_id,
            resource_type="document",
            resource_id=document.id,
            message="restored; all versions inactive",
        )
        return DocumentOut.model_validate(document)


@router.get("/versions/{version_id}/download")
async def download_version(
    version_id: UUID, principal: CurrentPrincipal, uow: Uow, providers: Providers
) -> StreamingResponse:
    """The original file, as uploaded.

    Addressed by version id rather than by storage key. A key-addressed
    endpoint would have to parse the tenant back out of the path and trust it;
    the version row already knows which organization it belongs to, and
    `get_version` checks it.
    """
    async with uow.begin() as session:
        version = await document_repo.get_version(session, principal.tenant, version_id)
        filename, content_type, key = version.filename, version.content_type, version.storage_key

    storage = providers.require_storage()
    safe = filename.replace('"', "").replace("\n", "")
    return StreamingResponse(
        storage.stream(key),
        media_type=content_type or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{safe}"'},
    )
