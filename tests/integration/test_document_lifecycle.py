"""Archiving, restoring and editing a document.

The test that matters here is the first one. "Archived" has to mean the content
stops answering questions, not merely that it leaves the library listing --
those two are easy to confuse and the difference is invisible until a guest is
told something the hotel withdrew last month.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.db import UnitOfWork
from app.core.enums import VersionStatus
from app.core.errors import ConflictError, NotFoundError
from app.repositories import document as document_repo

pytestmark = [pytest.mark.integration]

BREAKFAST = (
    "# Guest Handbook\n\n## Breakfast\n\n"
    "Breakfast is served from 7:00 AM to 10:00 AM. Ask for the kedgeree.\n"
)


@pytest.fixture
def retriever(settings, providers):
    from app.services.retrieval.service import Retriever

    return Retriever(
        settings=settings,
        search=providers.require_search(),
        embeddings=providers.require_embeddings(),
    )


@pytest.fixture
def ask(settings, db_engine, retriever, org_tenant):
    """Search the way a real caller does: no document_type filter."""

    async def _ask(query: str) -> list:
        maker = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)
        outcome = await retriever.retrieve(
            UnitOfWork(org_tenant, settings, sessionmaker=maker),
            org_tenant,
            queries=[query],
            top_k=20,
        )
        return outcome.hits

    return _ask


class TestArchiving:
    async def test_archived_content_stops_answering_questions(
        self, ingest, ask, org_uow, org_tenant
    ) -> None:
        """The whole point, and the half of it that is easy to miss.

        Deliberately queried **without** a document_type filter. The search SQL
        used to join `documents` -- and so check `is_deleted` -- only when such a
        filter was present, which is to say almost never. Setting the flag alone
        would have passed a filtered test and failed every real query.
        """
        document = await ingest(BREAKFAST, title="Guest Handbook")

        assert [h for h in await ask("breakfast kedgeree") if "kedgeree" in h.content.lower()], (
            "the document was not findable before archiving, so this proves nothing"
        )

        async with org_uow.begin() as session:
            _doc, withdrawn = await document_repo.archive_document(
                session, org_tenant, document.document_id
            )
        assert withdrawn > 0, "archiving withdrew no chunks at all"

        leaked = [h for h in await ask("breakfast kedgeree") if "kedgeree" in h.content.lower()]
        assert not leaked, "an archived document is still answering questions"

    async def test_archiving_deactivates_the_chunks_themselves(
        self, ingest, org_uow, org_tenant
    ) -> None:
        """Asserted separately from the behaviour above, on purpose.

        Two mechanisms produce "archived content does not answer": the chunks
        are deactivated, and the search SQL joins `documents`. Either alone is
        enough, so the behavioural test cannot tell which one broke. This one
        pins the chunk half -- which is also the half that keeps archived
        vectors out of the partial HNSW index rather than merely filtering them
        after the fact.
        """
        from sqlalchemy import text

        document = await ingest(BREAKFAST, title="Chunkless Handbook")

        async with org_uow.begin() as session:
            await document_repo.archive_document(session, org_tenant, document.document_id)
            active = (
                await session.execute(
                    text("SELECT count(*) FROM chunks WHERE document_id = :doc AND is_active"),
                    {"doc": document.document_id},
                )
            ).scalar_one()

        assert active == 0, f"{active} chunks are still live in the index"

    async def test_it_leaves_the_library(self, ingest, ask, org_uow, org_tenant) -> None:
        document = await ingest(BREAKFAST, title="Leaving Handbook")

        async with org_uow.begin() as session:
            await document_repo.archive_document(session, org_tenant, document.document_id)
            listed = await document_repo.list_documents(session, org_tenant, limit=200)

        assert document.document_id not in {d.id for d in listed}

    async def test_the_active_version_is_demoted(self, ingest, org_uow, org_tenant) -> None:
        """Otherwise the partial unique index still counts it as the live one."""
        document = await ingest(BREAKFAST, title="Demoted Handbook")

        async with org_uow.begin() as session:
            await document_repo.archive_document(session, org_tenant, document.document_id)
            versions = await document_repo.list_versions(session, org_tenant, document.document_id)
        assert not [v for v in versions if v.status is VersionStatus.ACTIVE]

    async def test_the_title_can_be_uploaded_again_afterwards(
        self, ingest, org_uow, org_tenant
    ) -> None:
        """Archiving releases the slug.

        `(organization_id, location_id, slug)` is unique and is not partial on
        `is_deleted`, while the slug lookup skips deleted rows. So without
        releasing it, re-uploading an archived title looks like a new document
        to the lookup and a duplicate to the database -- an unhandled
        IntegrityError, surfacing as a 500 on an ordinary upload.
        """
        first = await ingest(BREAKFAST, title="Recycled Handbook")

        async with org_uow.begin() as session:
            await document_repo.archive_document(session, org_tenant, first.document_id)

        second = await ingest(BREAKFAST, title="Recycled Handbook")
        assert second.document_id != first.document_id


class TestRestoring:
    async def test_restoring_brings_it_back_inactive(
        self, ingest, ask, org_uow, org_tenant
    ) -> None:
        """Restored, but not yet answering -- reactivating is a separate choice.

        Guessing which version should go live would skip the validation gates on
        the way back in, which is the one place they matter most.
        """
        document = await ingest(BREAKFAST, title="Restored Handbook")

        async with org_uow.begin() as session:
            await document_repo.archive_document(session, org_tenant, document.document_id)
        async with org_uow.begin() as session:
            restored = await document_repo.restore_document(
                session, org_tenant, document.document_id
            )
            versions = await document_repo.list_versions(session, org_tenant, document.document_id)

        assert restored.is_deleted is False
        assert ":archived:" not in restored.slug
        assert not [v for v in versions if v.status is VersionStatus.ACTIVE]
        assert not [h for h in await ask("breakfast kedgeree") if "kedgeree" in h.content.lower()]

    async def test_reactivating_makes_it_answer_again(
        self, ingest, ask, org_uow, org_tenant
    ) -> None:
        document = await ingest(BREAKFAST, title="Revived Handbook")

        async with org_uow.begin() as session:
            await document_repo.archive_document(session, org_tenant, document.document_id)
        async with org_uow.begin() as session:
            await document_repo.restore_document(session, org_tenant, document.document_id)
        async with org_uow.begin() as session:
            await document_repo.activate_version(session, org_tenant, document.version_id)

        assert [h for h in await ask("breakfast kedgeree") if "kedgeree" in h.content.lower()]

    async def test_restoring_onto_a_taken_name_is_refused(
        self, ingest, org_uow, org_tenant
    ) -> None:
        """Someone re-used the title while it was away. Say so rather than clash."""
        first = await ingest(BREAKFAST, title="Contested Handbook")

        async with org_uow.begin() as session:
            await document_repo.archive_document(session, org_tenant, first.document_id)

        await ingest(BREAKFAST, title="Contested Handbook")

        with pytest.raises(ConflictError):
            async with org_uow.begin() as session:
                await document_repo.restore_document(session, org_tenant, first.document_id)


class TestMetadata:
    async def test_editable_fields_change(self, ingest, org_uow, org_tenant) -> None:
        document = await ingest(BREAKFAST, title="Editable Handbook")

        async with org_uow.begin() as session:
            updated = await document_repo.update_document(
                session,
                org_tenant,
                document.document_id,
                {"title": "Renamed Handbook", "document_type": "faq"},
            )
        assert updated.title == "Renamed Handbook"
        assert updated.document_type == "faq"

    async def test_scope_is_not_editable(self, ingest, org_uow, org_tenant) -> None:
        """Moving a branch document to org-wide would need every chunk re-stamped."""
        document = await ingest(BREAKFAST, title="Fixed Scope Handbook")

        with pytest.raises(ValueError, match="Not updatable"):
            async with org_uow.begin() as session:
                await document_repo.update_document(
                    session, org_tenant, document.document_id, {"location_id": None}
                )

    async def test_another_organizations_document_is_not_found(self, org_uow, org_tenant) -> None:
        with pytest.raises(NotFoundError):
            async with org_uow.begin() as session:
                await document_repo.update_document(
                    session, org_tenant, uuid.uuid4(), {"title": "Hijacked"}
                )


class TestImageUploads:
    """A photograph of a notice is a document too.

    Tesseract already sat in the worker for scanned PDFs; the only thing missing
    was letting an image reach it. These pin the parts that are easy to get
    wrong: what the API accepts, and that the bytes are checked rather than the
    filename believed.
    """

    def test_images_are_accepted(self, settings) -> None:
        allowed = settings.storage.allowed_mime_types
        assert "image/png" in allowed
        assert "image/jpeg" in allowed

    def test_an_image_maps_to_its_own_source_type(self) -> None:
        from app.core.enums import SourceType
        from app.services.documents.service import _SOURCE_TYPES

        assert _SOURCE_TYPES["image/png"] is SourceType.IMAGE
        assert _SOURCE_TYPES["image/jpeg"] is SourceType.IMAGE

    def test_a_renamed_file_is_refused(self, settings, providers) -> None:
        """The declared type is a claim by the client; the bytes are not.

        These go straight to OCR, which hands them to a subprocess, so "it said
        it was a PNG" is not a good enough reason to pass them along.
        """
        from app.core.errors import UnsupportedFileType
        from app.services.documents.service import DocumentService

        service = DocumentService(settings, providers)
        with pytest.raises(UnsupportedFileType):
            service._validate_upload(
                b"this is plainly not a picture",
                content_type="image/png",
                filename="pretend.png",
            )

    def test_a_real_png_header_passes(self, settings, providers) -> None:
        from app.services.documents.service import DocumentService

        service = DocumentService(settings, providers)
        png = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 64
        service._validate_upload(png, content_type="image/png", filename="notice.png")

    def test_an_image_parses_to_an_empty_page_for_ocr_to_fill(self) -> None:
        """No text layer, and one page that is entirely picture.

        That is exactly what the OCR stage reads as "there is nothing here to
        weigh, read it", which is why images need no separate sufficiency check.
        """
        from app.services.ingestion.parsing.pdf import parse_image

        parsed = parse_image(b"irrelevant")
        assert parsed.page_count == 1
        assert parsed.page_texts == [""]
        assert parsed.image_area_ratios == [1.0]
        assert parsed.lines == []
