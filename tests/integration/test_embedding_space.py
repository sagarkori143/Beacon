"""Embedding-space integrity.

The failure this guards against has no error message. Point the deployment at a
different embedding model of the same dimension and every insert succeeds, every
query runs, and retrieval quality collapses. Nothing raises, nothing logs, and
the only symptom is that answers get worse.

So vectors are stamped with the space that produced them, and anything that would
mix two spaces is refused rather than written.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.config import EmbeddingProviderConfig, Settings
from app.core.errors import EmbeddingSpaceMismatch, IngestionError
from app.core.tracing import TraceContext
from app.providers.embeddings.fake import FakeEmbeddingProvider
from app.providers.registry import ProviderBundle
from app.repositories.embedding_space import (
    column_dimension,
    get_current_space,
    verify_embedding_space,
)
from app.services.ingestion.pipeline import IngestionPipeline

pytestmark = [pytest.mark.integration]

DOC = """# Handbook

## Breakfast Service

Breakfast is served from 7:00 AM to 10:00 AM in the main dining room daily.
"""


def other_model(dimension: int = 768) -> FakeEmbeddingProvider:
    """A different model at the *same* dimension -- the dangerous case.

    A dimension mismatch fails loudly on insert. Same-dimension drift is the one
    that writes cleanly and destroys retrieval silently.
    """
    return FakeEmbeddingProvider(
        EmbeddingProviderConfig(
            name="default", type="fake", model="some-other-embed", dimension=dimension
        )
    )


class TestBootGuard:
    async def test_the_column_dimension_is_readable(self, org_uow) -> None:
        async with org_uow.begin() as session:
            assert await column_dimension(session) == 768

    async def test_a_matching_provider_verifies(
        self, org_uow, fake_embeddings, fake_embedding_space
    ) -> None:
        async with org_uow.begin() as session:
            space = await verify_embedding_space(session, fake_embeddings, configured_dimension=768)
        assert space.model == fake_embeddings.model
        assert space.dimension == 768

    async def test_a_declared_dimension_mismatch_is_refused(self, org_uow) -> None:
        """The provider says 1024; the column is 768."""
        async with org_uow.begin() as session:
            with pytest.raises(EmbeddingSpaceMismatch, match="EMBEDDING_DIM"):
                await verify_embedding_space(session, other_model(1024), configured_dimension=1024)

    async def test_a_different_model_at_the_same_dimension_is_refused(
        self, org_uow, fake_embedding_space
    ) -> None:
        """The case with no error message. Same shape, incomparable vectors."""
        async with org_uow.begin() as session:
            with pytest.raises(EmbeddingSpaceMismatch, match="not comparable"):
                await verify_embedding_space(session, other_model(768), configured_dimension=768)


class TestPipelineStamping:
    async def test_chunks_carry_the_space_that_produced_them(
        self, ingest, org_uow, fake_embeddings
    ) -> None:
        result = await ingest(DOC, title="Handbook")

        async with org_uow.begin() as session:
            current = await get_current_space(session)
            spaces = (
                (
                    await session.execute(
                        text(
                            "SELECT DISTINCT embedding_space_id FROM chunks"
                            " WHERE document_version_id = :v"
                        ),
                        {"v": result.version_id},
                    )
                )
                .scalars()
                .all()
            )

        assert len(spaces) == 1
        assert spaces[0] == current.id
        assert current.model == fake_embeddings.model

    async def test_ingesting_with_a_foreign_provider_is_refused(
        self, settings: Settings, providers: ProviderBundle, ingest, org_uow, org_tenant
    ) -> None:
        """Taking the current space on trust would label these vectors as the
        other model's -- corruption that survives every validation gate, because
        every gate sees a well-formed 768-dimensional vector."""
        await ingest(DOC, title="Handbook")

        second = await ingest(DOC, title="Second Handbook", run_pipeline=False)
        providers.embeddings = other_model(768)
        pipeline = IngestionPipeline(settings=settings, providers=providers)

        with pytest.raises(IngestionError, match="incomparable|re-embedding"):
            await pipeline.run(
                org_uow,
                second.job_id,
                tenant=org_tenant,
                trace=TraceContext.new(),
                worker_id="test",
            )

    async def test_the_refusal_is_terminal_not_retryable(
        self, settings: Settings, providers: ProviderBundle, ingest, org_uow, org_tenant
    ) -> None:
        """Retrying will not change the answer; the operator needs to know now."""
        await ingest(DOC, title="Handbook")
        second = await ingest(DOC, title="Third Handbook", run_pipeline=False)
        providers.embeddings = other_model(768)
        pipeline = IngestionPipeline(settings=settings, providers=providers)

        with pytest.raises(IngestionError) as caught:
            await pipeline.run(
                org_uow,
                second.job_id,
                tenant=org_tenant,
                trace=TraceContext.new(),
                worker_id="test",
            )
        assert caught.value.retryable is False
