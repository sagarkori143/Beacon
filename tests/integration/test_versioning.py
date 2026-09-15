"""Document versioning and atomic activation.

The spec names one of these as critical: **the old version must remain active
until the new one passes validation.** It is the guarantee that makes uploading
a new revision of a live policy safe, and it is tested here against the real
schema with the real partial unique index.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.enums import JobStatus, VersionStatus
from app.core.tracing import TraceContext
from app.repositories import document as document_repo
from app.repositories import ingestion as job_repo

pytestmark = [pytest.mark.integration]

V1 = """# Breakfast Policy

## Breakfast Service

Breakfast is served from 7:00 AM to 10:00 AM in the main dining room daily.
Children under six eat free of charge with a paying adult.
"""

V2 = """# Breakfast Policy

## Breakfast Service

Breakfast is served from 6:30 AM to 11:00 AM in the main dining room daily.
Children under twelve eat free of charge with a paying adult.
"""


async def active_version(uow, tenant, document_id):  # type: ignore[no-untyped-def]
    async with uow.begin() as session:
        return await document_repo.get_active_version(session, tenant, document_id)


class TestVersionLifecycle:
    async def test_first_version_activates(self, ingest, org_uow, org_tenant) -> None:
        doc = await ingest(V1, title="Breakfast Policy")
        version = await active_version(org_uow, org_tenant, doc.document_id)

        assert version is not None
        assert version.version_number == 1
        assert version.status is VersionStatus.ACTIVE
        assert version.chunk_count > 0

    async def test_same_title_creates_a_version_not_a_document(
        self, ingest, org_uow, org_tenant
    ) -> None:
        """Re-uploading a policy is a revision, not a second policy."""
        first = await ingest(V1, title="Breakfast Policy")
        second = await ingest(V2, title="Breakfast Policy")

        assert second.document_id == first.document_id
        assert second.version_number == 2

    async def test_activation_replaces_the_previous_version(
        self, ingest, org_uow, org_tenant, retriever
    ) -> None:
        document = await ingest(V1, title="Breakfast Policy")
        await ingest(V2, title="Breakfast Policy")

        version = await active_version(org_uow, org_tenant, document.document_id)
        assert version.version_number == 2

        async with org_uow.begin() as session:
            versions = await document_repo.list_versions(session, org_tenant, document.document_id)
        statuses = {v.version_number: v.status for v in versions}
        assert statuses[1] is VersionStatus.INACTIVE
        assert statuses[2] is VersionStatus.ACTIVE

    async def test_only_the_active_version_is_retrievable(
        self, ingest, org_uow, org_tenant, retriever
    ) -> None:
        """Superseded content must disappear from answers immediately."""
        await ingest(V1, title="Breakfast Policy")
        await ingest(V2, title="Breakfast Policy")

        outcome = await retriever.retrieve(
            org_uow, org_tenant, queries=["breakfast serving hours"], top_k=5
        )
        text_found = " ".join(h.content for h in outcome.hits)
        assert "6:30 AM to 11:00 AM" in text_found
        assert "7:00 AM to 10:00 AM" not in text_found
        assert all(h.document_version == 2 for h in outcome.hits)


class TestDatabaseInvariants:
    async def test_at_most_one_active_version_per_document(
        self, ingest, org_uow, org_tenant
    ) -> None:
        """Enforced by a partial unique index, not by application code.

        Attempting a second ACTIVE row must be rejected by the database itself.
        """
        document = await ingest(V1, title="Breakfast Policy")
        await ingest(V2, title="Breakfast Policy")

        async with org_uow.begin() as session:
            with pytest.raises(Exception, match="duplicate key|unique"):
                await session.execute(
                    text(
                        "UPDATE document_versions SET status = 'ACTIVE'"
                        " WHERE document_id = :doc AND version_number = 1"
                    ),
                    {"doc": document.document_id},
                )

    async def test_chunks_are_invisible_until_activation(
        self, ingest, org_uow, org_tenant, pipeline
    ) -> None:
        """A crash mid-pipeline must leave nothing searchable.

        Visibility is a column set at activation, not an absence of rows, so
        there is no cleanup path that can be forgotten.
        """
        doc = await ingest(V1, title="Draft Policy", run_pipeline=False)

        async with org_uow.begin() as session:
            visible = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM chunks WHERE document_version_id = :v AND is_active"
                    ),
                    {"v": doc.version_id},
                )
            ).scalar_one()
        assert visible == 0


class TestFailedActivation:
    async def test_a_failing_version_leaves_the_previous_one_serving(
        self, ingest, org_uow, org_tenant, pipeline, retriever, monkeypatch
    ) -> None:
        """The critical guarantee.

        v2 fails validation. v1 must still be ACTIVE, still retrievable, and
        still returning its own content -- not a mixture, and not nothing.
        """
        document = await ingest(V1, title="Breakfast Policy")

        # A version that yields no chunks: an empty OCR result, or a parse that
        # silently produced nothing. Activating it would replace a working
        # policy with one that answers nothing -- the worst possible outcome,
        # and exactly what the chunk-count gate exists to prevent.
        second = await ingest("   \n\n   \n", title="Breakfast Policy", run_pipeline=False)

        with pytest.raises(Exception):
            await pipeline.run(
                org_uow,
                second.job_id,
                tenant=org_tenant,
                trace=TraceContext.new(),
                worker_id="test",
            )

        still_active = await active_version(org_uow, org_tenant, document.document_id)
        assert still_active.version_number == 1
        assert still_active.status is VersionStatus.ACTIVE

        outcome = await retriever.retrieve(
            org_uow, org_tenant, queries=["breakfast serving hours"], top_k=5
        )
        assert outcome.hits, "the previously working version stopped answering"
        assert "7:00 AM to 10:00 AM" in " ".join(h.content for h in outcome.hits)

    async def test_the_failure_is_recorded_on_the_job(
        self, ingest, org_uow, org_tenant, pipeline
    ) -> None:
        """An operator must be able to see why, without reading logs."""
        doc = await ingest("   \n\n   \n", title="Empty Doc", run_pipeline=False)

        with pytest.raises(Exception):
            await pipeline.run(
                org_uow,
                doc.job_id,
                tenant=org_tenant,
                trace=TraceContext.new(),
                worker_id="test",
            )

        async with org_uow.begin() as session:
            job = await job_repo.get_job(session, org_tenant, doc.job_id)
            events = await job_repo.list_events(session, org_tenant, doc.job_id)

        assert job.status is JobStatus.FAILED
        assert job.error_message
        assert any(e.status == "FAILED" for e in events)


class TestRollback:
    async def test_a_superseded_version_can_be_reactivated(
        self, ingest, org_uow, org_tenant, retriever
    ) -> None:
        """Rollback uses the same transaction as a forward activation."""
        document = await ingest(V1, title="Breakfast Policy")
        first_version_id = document.version_id
        await ingest(V2, title="Breakfast Policy")

        async with org_uow.begin() as session:
            restored, replaced = await document_repo.rollback_to_version(
                session, org_tenant, first_version_id
            )

        assert restored.version_number == 1
        assert replaced.version_number == 2

        outcome = await retriever.retrieve(
            org_uow, org_tenant, queries=["breakfast serving hours"], top_k=5
        )
        joined = " ".join(h.content for h in outcome.hits)
        assert "7:00 AM to 10:00 AM" in joined
        assert "6:30 AM to 11:00 AM" not in joined

    async def test_activating_an_already_active_version_is_idempotent(
        self, ingest, org_uow, org_tenant
    ) -> None:
        """A redelivered queue message must not corrupt anything."""
        document = await ingest(V1, title="Breakfast Policy")

        async with org_uow.begin() as session:
            active = await document_repo.get_active_version(
                session, org_tenant, document.document_id
            )
            again, replaced = await document_repo.activate_version(session, org_tenant, active.id)

        assert again.version_number == 1
        assert replaced is None


class TestRedeliveryIdempotency:
    """At-least-once delivery means a completed job can run again."""

    async def test_reprocessing_an_active_version_keeps_it_searchable(
        self, ingest, org_uow, org_tenant, pipeline, retriever
    ) -> None:
        """The failure this guards against is silent and total.

        A redelivered message re-runs INDEXING, which deletes the chunks and
        rewrites them inactive. If ACTIVATING then short-circuits on "already
        active" without flipping them back, the version stays marked ACTIVE
        while its content is invisible to every search -- and every row involved
        looks correct on its own, so nothing reports an error.
        """
        document = await ingest(V1, title="Breakfast Policy")

        before = await retriever.retrieve(
            org_uow, org_tenant, queries=["breakfast serving hours"], top_k=5
        )
        assert before.hits

        # Exactly what a redelivered queue message does.
        await pipeline.run(
            org_uow,
            document.job_id,
            tenant=org_tenant,
            trace=TraceContext.new(),
            worker_id="second-worker",
        )

        after = await retriever.retrieve(
            org_uow, org_tenant, queries=["breakfast serving hours"], top_k=5
        )
        assert after.hits, "the document vanished from search after reprocessing"
        assert "7:00 AM to 10:00 AM" in " ".join(h.content for h in after.hits)

        version = await active_version(org_uow, org_tenant, document.document_id)
        assert version.status is VersionStatus.ACTIVE

    async def test_reprocessing_does_not_duplicate_chunks(
        self, ingest, org_uow, org_tenant, pipeline
    ) -> None:
        document = await ingest(V1, title="Breakfast Policy")

        async with org_uow.begin() as session:
            first = (
                await session.execute(
                    text("SELECT count(*) FROM chunks WHERE document_version_id = :v"),
                    {"v": document.version_id},
                )
            ).scalar_one()

        await pipeline.run(
            org_uow,
            document.job_id,
            tenant=org_tenant,
            trace=TraceContext.new(),
            worker_id="second-worker",
        )

        async with org_uow.begin() as session:
            second = (
                await session.execute(
                    text("SELECT count(*) FROM chunks WHERE document_version_id = :v"),
                    {"v": document.version_id},
                )
            ).scalar_one()

        assert second == first
