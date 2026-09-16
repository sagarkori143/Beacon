"""The ingestion pipeline.

PARSING -> OCR -> CLEANING -> CHUNKING -> EMBEDDING -> INDEXING -> VALIDATING
-> ACTIVATING.

Two properties hold throughout, and both exist because queue delivery is
at-least-once:

**Every stage is idempotent.** Chunk writes upsert on
``(document_version_id, ordinal)``; activation is conditional on the version's
current status. A redelivered message re-runs work rather than duplicating it.

**Nothing is visible until activation.** Chunks are written ``is_active=false``,
so a worker that dies at 80% of EMBEDDING leaves rows that no search can see.
The previous version keeps serving, untouched, and there is no cleanup step that
can be forgotten.

Expensive intermediate work is persisted where it is worth persisting:
``normalized_text`` lands on the version after CLEANING, so re-chunking with new
settings -- or resuming after a crash -- never re-parses and never re-OCRs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from app.core.config import Settings
from app.core.db import UnitOfWork
from app.core.enums import IngestionStage, SourceType, TextExtractionMode
from app.core.errors import IngestionError, ValidationGateFailed
from app.core.logging import get_logger
from app.core.tenancy import TenantContext
from app.core.tracing import TraceContext
from app.providers.progress.base import ProgressEvent, ProgressPublisher
from app.providers.registry import ProviderBundle
from app.providers.vector_store.base import ChunkRecord
from app.repositories import chunk as chunk_repo
from app.repositories import document as document_repo
from app.repositories import embedding_space as space_repo
from app.repositories import ingestion as job_repo
from app.services.ingestion.chunking.chunker import DraftChunk, chunk_document
from app.services.ingestion.chunking.sections import build_section_tree
from app.services.ingestion.ocr_gate import TextLayerAssessment, assess_text_layer
from app.services.ingestion.parsing.pdf import ParsedPDF, parse_pdf, parse_plain_text, render_pages
from app.services.ingestion.validation import run_gates

log = get_logger(__name__)


@dataclass(slots=True)
class PipelineState:
    """In-memory state carried between stages of one run."""

    version_id: UUID
    organization_id: UUID
    tenant: TenantContext
    source_type: SourceType
    source_name: str
    language: str = "en"

    raw: bytes | None = None
    parsed: ParsedPDF | None = None
    assessment: TextLayerAssessment | None = None
    page_texts: list[str] = field(default_factory=list)
    normalized_text: str = ""
    chunks: list[DraftChunk] = field(default_factory=list)
    embeddings: list[list[float]] = field(default_factory=list)
    embedding_space_id: UUID | None = None

    ocr_used: bool = False
    ocr_pages: int = 0
    ocr_confidence: float | None = None
    stage_timings: dict[str, float] = field(default_factory=dict)


class IngestionPipeline:
    def __init__(
        self,
        *,
        settings: Settings,
        providers: ProviderBundle,
        progress: ProgressPublisher | None = None,
    ) -> None:
        self.settings = settings
        self.providers = providers
        # Optional on purpose: nothing about ingestion depends on anyone
        # watching it, and a run with no publisher must behave identically.
        self.progress = progress

    async def _announce(
        self,
        job_id: UUID,
        stage: IngestionStage,
        *,
        status: str,
        message: str | None = None,
        duration_ms: float | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Mirror a stage transition to whoever is watching.

        Called right after the durable write, never instead of it. The record of
        what happened is the `ingestion_job_events` row; this is a copy for a
        live view, and losing it costs a frame, not a document.
        """
        if self.progress is None:
            return
        try:
            await self.progress.publish(
                ProgressEvent(
                    job_id=job_id,
                    stage=stage.value,
                    status=status,
                    progress=stage.progress,
                    message=message,
                    duration_ms=duration_ms,
                    detail=detail or {},
                )
            )
        except Exception as exc:  # noqa: BLE001 - see below
            # The interface asks implementations not to raise, and the shipped
            # one does not. Guarding anyway, because the cost of being wrong is
            # a document that ingested perfectly being marked failed -- and the
            # only thing that actually went wrong was that nobody could watch.
            log.warning(
                "progress_announce_failed",
                job_id=str(job_id),
                stage=stage.value,
                error=str(exc)[:200],
            )

    async def run(
        self,
        uow: UnitOfWork,
        job_id: UUID,
        *,
        tenant: TenantContext,
        trace: TraceContext,
        worker_id: str,
    ) -> None:
        """Run the pipeline for one job. Raises on failure, after recording it."""
        async with uow.begin() as session:
            job = await job_repo.get_job(session, tenant, job_id)
            version = await document_repo.get_version(session, tenant, job.document_version_id)
            document = await document_repo.get_document(session, tenant, version.document_id)
            await job_repo.start_job(session, job, worker_id=worker_id)

            state = PipelineState(
                version_id=version.id,
                organization_id=version.organization_id,
                tenant=tenant,
                source_type=document.source_type,
                source_name=version.filename,
                language=document.language,
            )
            storage_key = version.storage_key
            resume_text = version.normalized_text

        stages: list[tuple[IngestionStage, Any]] = [
            (IngestionStage.PARSING, self._parse),
            (IngestionStage.OCR, self._ocr),
            (IngestionStage.CLEANING, self._clean),
            (IngestionStage.CHUNKING, self._chunk),
            (IngestionStage.EMBEDDING, self._embed),
            (IngestionStage.INDEXING, self._index),
            (IngestionStage.VALIDATING, self._validate),
            (IngestionStage.ACTIVATING, self._activate),
        ]

        # Resuming a crashed run: the expensive front half is already on disk.
        if resume_text:
            state.normalized_text = resume_text
            stages = [s for s in stages if s[0].progress >= IngestionStage.CHUNKING.progress]
            log.info("pipeline_resumed", version_id=str(state.version_id), from_stage="CHUNKING")

        state.raw = await self.providers.require_storage().get(storage_key)

        for stage, handler in stages:
            started = time.perf_counter()
            try:
                with trace.span(f"ingest.{stage.value.lower()}"):
                    detail = await handler(uow, state, trace)
            except Exception as exc:
                duration = (time.perf_counter() - started) * 1000.0
                await self._record_failure(uow, tenant, job_id, state, stage, exc, duration)
                raise

            duration = (time.perf_counter() - started) * 1000.0
            state.stage_timings[stage.value] = duration

            async with uow.begin() as session:
                job = await job_repo.get_job(session, tenant, job_id)
                await job_repo.update_stage(session, job, stage)
                await job_repo.add_event(
                    session,
                    job,
                    stage=stage,
                    status="COMPLETED",
                    duration_ms=duration,
                    detail=detail or {},
                )
            await self._announce(
                job_id, stage, status="COMPLETED", duration_ms=duration, detail=detail or {}
            )

        async with uow.begin() as session:
            job = await job_repo.get_job(session, tenant, job_id)
            await job_repo.complete_job(session, job)
            await job_repo.add_event(
                session,
                job,
                stage=IngestionStage.COMPLETED,
                status="COMPLETED",
                message=f"{len(state.chunks)} chunks indexed and activated",
                detail={"timings_ms": state.stage_timings},
            )
        await self._announce(
            job_id,
            IngestionStage.COMPLETED,
            status="COMPLETED",
            message=f"{len(state.chunks)} chunks indexed and activated",
            detail={"timings_ms": state.stage_timings},
        )

        log.info(
            "ingestion_complete",
            version_id=str(state.version_id),
            chunks=len(state.chunks),
            ocr_used=state.ocr_used,
            total_ms=round(sum(state.stage_timings.values()), 1),
        )

    # -- stages --------------------------------------------------------------

    async def _parse(
        self, uow: UnitOfWork, state: PipelineState, trace: TraceContext
    ) -> dict[str, Any]:
        """Extract text and layout from the original file."""
        assert state.raw is not None

        if state.source_type is SourceType.PDF:
            state.parsed = parse_pdf(state.raw)
        else:
            state.parsed = parse_plain_text(state.raw)

        state.page_texts = list(state.parsed.page_texts)

        async with uow.begin() as session:
            version = await document_repo.get_version(session, state.tenant, state.version_id)
            version.page_count = state.parsed.page_count

        return {
            "pages": state.parsed.page_count,
            "layout_lines": len(state.parsed.lines),
            "body_font_size": round(state.parsed.stats.body_font_size, 1),
            "repeating_lines": len(state.parsed.stats.repeating_lines),
        }

    async def _ocr(
        self, uow: UnitOfWork, state: PipelineState, trace: TraceContext
    ) -> dict[str, Any]:
        """Decide whether OCR is needed, and run it only on the pages that need it."""
        assert state.parsed is not None

        if state.source_type is not SourceType.PDF:
            return {"skipped": "not a PDF"}

        assessment = assess_text_layer(
            state.page_texts,
            self.settings.ocr,
            image_area_ratios=state.parsed.image_area_ratios,
            font_flags=state.parsed.font_flags,
        )
        state.assessment = assessment

        if not assessment.needs_ocr:
            async with uow.begin() as session:
                version = await document_repo.get_version(session, state.tenant, state.version_id)
                version.extraction_mode = TextExtractionMode.NATIVE
                version.ocr_used = False
            return assessment.to_event_detail()

        assert state.raw is not None
        images = render_pages(state.raw, list(assessment.ocr_pages), dpi=self.settings.ocr.dpi)
        result = await self.providers.require_ocr().extract_text(
            images, languages=self.settings.ocr.languages
        )

        # Replace only the pages that were OCR'd; native text on the others is
        # better than OCR output and must not be thrown away.
        for page in result.pages:
            if 1 <= page.page_number <= len(state.page_texts) and page.text:
                state.page_texts[page.page_number - 1] = page.text

        state.ocr_used = True
        state.ocr_pages = result.page_count
        state.ocr_confidence = result.mean_confidence

        async with uow.begin() as session:
            version = await document_repo.get_version(session, state.tenant, state.version_id)
            version.extraction_mode = assessment.mode
            version.ocr_used = True
            version.ocr_page_count = result.page_count
            version.ocr_mean_confidence = result.mean_confidence

        return {
            **assessment.to_event_detail(),
            "engine": result.engine,
            "ocr_duration_ms": round(result.duration_ms, 1),
            "mean_confidence": round(result.mean_confidence, 3),
        }

    async def _clean(
        self, uow: UnitOfWork, state: PipelineState, trace: TraceContext
    ) -> dict[str, Any]:
        """Normalize whitespace and persist the result.

        Persisting here is what makes re-chunking cheap: changing chunk settings
        later, or resuming after a crash, starts from this text rather than from
        the original file.
        """
        cleaned = "\n\n".join(t.strip() for t in state.page_texts if t and t.strip())
        cleaned = cleaned.replace("\r\n", "\n").replace("­", "")
        state.normalized_text = cleaned

        async with uow.begin() as session:
            version = await document_repo.get_version(session, state.tenant, state.version_id)
            version.normalized_text = cleaned

        return {"characters": len(cleaned), "pages_with_text": len(state.page_texts)}

    async def _chunk(
        self, uow: UnitOfWork, state: PipelineState, trace: TraceContext
    ) -> dict[str, Any]:
        """Build the section tree and cut it into chunks."""
        if state.parsed is not None and state.parsed.lines:
            tree = build_section_tree(state.parsed.lines, state.parsed.stats)
        else:
            # Resumed run: rebuild structure from the persisted normalized text.
            rebuilt = parse_plain_text(state.normalized_text.encode("utf-8"))
            tree = build_section_tree(rebuilt.lines, rebuilt.stats)

        state.chunks = chunk_document(tree, self.settings.chunking, source_name=state.source_name)
        if not state.chunks:
            raise IngestionError(
                "No chunks were produced. The document appears to contain no extractable text.",
                stage=IngestionStage.CHUNKING.value,
                retryable=False,
            )

        return {
            "chunks": len(state.chunks),
            "mean_tokens": round(sum(c.token_count for c in state.chunks) / len(state.chunks), 1),
            "max_tokens": max(c.token_count for c in state.chunks),
            "sections": len({c.section_key for c in state.chunks if c.section_key}),
        }

    async def _embed(
        self, uow: UnitOfWork, state: PipelineState, trace: TraceContext
    ) -> dict[str, Any]:
        """Embed every chunk.

        This is a network call to a model server that may be on another machine.
        A failure here is marked retryable so the queue redelivers rather than
        failing the document outright -- an unreachable GPU box should delay
        ingestion, not reject the upload.
        """
        embeddings_provider = self.providers.require_embeddings()

        async with uow.begin() as session:
            # Stamp chunks with the space of the provider that is *actually*
            # producing these vectors, not merely with whatever space happens to
            # be current. Taking the current space on trust would label vectors
            # from one model as belonging to another -- which is exactly the
            # silent corruption the embedding-space mechanism exists to prevent,
            # and it would survive every validation gate.
            space = await space_repo.ensure_space(
                session,
                provider_type=embeddings_provider.provider_type,
                model=embeddings_provider.model,
                dimension=embeddings_provider.dimension,
                make_current=False,
            )
            current = await space_repo.get_current_space(session)

            if current is None:
                # First ingestion into an empty index: adopt this space.
                space = await space_repo.ensure_space(
                    session,
                    provider_type=embeddings_provider.provider_type,
                    model=embeddings_provider.model,
                    dimension=embeddings_provider.dimension,
                    make_current=True,
                )
            elif current.id != space.id:
                raise IngestionError(
                    f"Embedding provider is {space.describe()} but the index holds "
                    f"{current.describe()}. Writing these vectors would make them "
                    f"incomparable to everything already indexed. See the "
                    f"re-embedding procedure in docs/providers.md.",
                    stage=IngestionStage.EMBEDDING.value,
                    retryable=False,
                )

            state.embedding_space_id = space.id

        try:
            state.embeddings = await embeddings_provider.embed_documents(
                [c.content for c in state.chunks], trace=trace
            )
        except Exception as exc:
            raise IngestionError(
                f"Embedding failed: {exc}",
                stage=IngestionStage.EMBEDDING.value,
                retryable=True,
            ) from exc

        if len(state.embeddings) != len(state.chunks):
            raise IngestionError(
                f"Embedding provider returned {len(state.embeddings)} vectors for "
                f"{len(state.chunks)} chunks",
                stage=IngestionStage.EMBEDDING.value,
                retryable=True,
            )

        return {
            "vectors": len(state.embeddings),
            "model": embeddings_provider.model,
            "dimension": embeddings_provider.dimension,
        }

    async def _index(
        self, uow: UnitOfWork, state: PipelineState, trace: TraceContext
    ) -> dict[str, Any]:
        """Write chunks -- inactive -- replacing any left by a previous attempt."""
        assert state.embedding_space_id is not None
        store = self.providers.require_vector_store()

        async with uow.begin() as session:
            version = await document_repo.get_version(session, state.tenant, state.version_id)

            # A previous attempt may have written a different number of chunks;
            # the upsert alone would leave the surplus behind.
            await chunk_repo.delete_chunks_for_version(session, version.id)

            records = [
                ChunkRecord(
                    id=uuid4(),
                    organization_id=version.organization_id,
                    location_id=version.location_id,
                    document_id=version.document_id,
                    document_version_id=version.id,
                    document_version=version.version_number,
                    ordinal=draft.ordinal,
                    content=draft.content,
                    content_hash=draft.content_hash,
                    token_count=draft.token_count,
                    language=state.language,
                    source_type=state.source_type,
                    source_name=state.source_name,
                    embedding_space_id=state.embedding_space_id,
                    embedding=embedding,
                    heading=draft.heading,
                    section_path=list(draft.section_path),
                    section_key=draft.section_key,
                    topic_key=draft.topic_key,
                    kind=draft.kind,
                    page_from=draft.page_from,
                    page_to=draft.page_to,
                    meta={"ocr": state.ocr_used},
                )
                for draft, embedding in zip(state.chunks, state.embeddings, strict=True)
            ]
            written = await store.upsert(session, records)
            version.chunk_count = written

        return {"written": written, "active": False}

    async def _validate(
        self, uow: UnitOfWork, state: PipelineState, trace: TraceContext
    ) -> dict[str, Any]:
        """Run the gates. A failure here leaves the previous version serving."""
        async with uow.begin() as session:
            version = await document_repo.get_version(session, state.tenant, state.version_id)
            report = await run_gates(session, version, search=self.providers.search)

        if not report.passed:
            raise ValidationGateFailed(
                report.summary(),
                stage=IngestionStage.VALIDATING.value,
                retryable=False,
                details=report.to_detail(),
            )
        return report.to_detail()

    async def _activate(
        self, uow: UnitOfWork, state: PipelineState, trace: TraceContext
    ) -> dict[str, Any]:
        """Atomically make this version the active one.

        The gates run again here, inside the same transaction that holds the
        document's row lock. Re-running them is cheap and closes the only
        remaining window: between the VALIDATING stage's transaction and this
        one, nothing else could have changed these rows -- but proving that costs
        less than assuming it.
        """
        async with uow.begin() as session:
            version = await document_repo.get_version(session, state.tenant, state.version_id)

            report = await run_gates(session, version, search=self.providers.search)
            if not report.passed:
                raise ValidationGateFailed(
                    report.summary(),
                    stage=IngestionStage.ACTIVATING.value,
                    retryable=False,
                    details=report.to_detail(),
                )

            activated, previous = await document_repo.activate_version(
                session, state.tenant, version.id, chunk_count=len(state.chunks)
            )

        return {
            "activated_version": activated.version_number,
            "replaced_version": previous.version_number if previous else None,
            "chunks": len(state.chunks),
        }

    # -- failure -------------------------------------------------------------

    async def _record_failure(
        self,
        uow: UnitOfWork,
        tenant: TenantContext,
        job_id: UUID,
        state: PipelineState,
        stage: IngestionStage,
        exc: Exception,
        duration_ms: float,
    ) -> None:
        """Mark the job and version failed. The active version is not touched."""
        message = str(exc)[:1000]
        detail = getattr(exc, "details", {}) or {}

        async with uow.begin() as session:
            job = await job_repo.get_job(session, tenant, job_id)
            await job_repo.fail_job(session, job, stage=stage, error=message)
            await job_repo.add_event(
                session,
                job,
                stage=stage,
                status="FAILED",
                message=message,
                duration_ms=duration_ms,
                detail=detail,
            )
            await document_repo.mark_version_failed(
                session, tenant, state.version_id, f"{stage.value}: {message}"
            )

        await self._announce(
            job_id,
            stage,
            status="FAILED",
            message=message,
            duration_ms=duration_ms,
            detail=detail,
        )

        log.error(
            "ingestion_failed",
            version_id=str(state.version_id),
            stage=stage.value,
            error=message,
            retryable=getattr(exc, "retryable", False),
        )


def is_retryable(exc: BaseException) -> bool:
    """Whether the queue should redeliver after this failure.

    Transient infrastructure problems are retried; a document that cannot be
    parsed will not parse differently next time, and retrying it just burns the
    attempt budget before it reaches the dead-letter stream.
    """
    return bool(getattr(exc, "retryable", False))
