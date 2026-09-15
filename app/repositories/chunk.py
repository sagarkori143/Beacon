"""Chunk queries used outside the search path."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import TenantContext
from app.models.chunk import Chunk


async def set_chunks_active(
    session: AsyncSession, document_version_id: UUID, *, active: bool
) -> int:
    """Flip visibility for every chunk of a version in one statement."""
    result = await session.execute(
        update(Chunk)
        .where(Chunk.document_version_id == document_version_id, Chunk.is_active != active)
        .values(is_active=active)
    )
    return result.rowcount or 0


async def delete_chunks_for_version(session: AsyncSession, document_version_id: UUID) -> int:
    result = await session.execute(
        text("DELETE FROM chunks WHERE document_version_id = :vid"),
        {"vid": document_version_id},
    )
    return result.rowcount or 0


async def count_for_version(session: AsyncSession, document_version_id: UUID) -> int:
    result = await session.execute(
        select(func.count(Chunk.id)).where(Chunk.document_version_id == document_version_id)
    )
    return int(result.scalar_one())


async def get_chunks(
    session: AsyncSession, tenant: TenantContext, chunk_ids: Sequence[UUID]
) -> list[Chunk]:
    """Fetch chunks by id, scoped to the tenant.

    Used by the ``document_lookup`` tool and by citation rendering, both of which
    receive ids that originated from a model's output -- so the tenant predicate
    here is load-bearing, not decorative.
    """
    if not chunk_ids:
        return []
    result = await session.execute(
        select(Chunk).where(
            Chunk.organization_id == tenant.organization_id,
            Chunk.id.in_(list(chunk_ids)),
        )
    )
    return list(result.scalars().all())


async def get_section(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    document_id: UUID,
    section_key: str,
    active_only: bool = True,
) -> list[Chunk]:
    """Every chunk of one section of a document, in document order."""
    stmt = select(Chunk).where(
        Chunk.organization_id == tenant.organization_id,
        Chunk.document_id == document_id,
        Chunk.section_key == section_key,
    )
    if active_only:
        stmt = stmt.where(Chunk.is_active.is_(True))
    result = await session.execute(stmt.order_by(Chunk.ordinal))
    return list(result.scalars().all())


async def validation_stats(session: AsyncSession, document_version_id: UUID) -> dict[str, int]:
    """Counts the validation gate needs, in one round trip.

    Gathering these separately would be several queries against the same rows at
    the exact moment the pipeline is trying to finish quickly.
    """
    result = await session.execute(
        text(
            """
            SELECT
                count(*)                                          AS total,
                count(*) FILTER (WHERE embedding IS NULL)         AS missing_embedding,
                count(*) FILTER (WHERE search_vector IS NULL)     AS missing_search_vector,
                count(*) FILTER (WHERE content = '')              AS empty_content,
                count(DISTINCT organization_id)                   AS distinct_orgs,
                count(DISTINCT embedding_space_id)                AS distinct_spaces
            FROM chunks
            WHERE document_version_id = :vid
            """
        ),
        {"vid": document_version_id},
    )
    row = result.mappings().one()
    return {key: int(value) for key, value in row.items()}


async def foreign_tenant_rows(
    session: AsyncSession, document_version_id: UUID, organization_id: UUID
) -> int:
    """Chunks of this version that claim a different organization.

    Should always be zero. It is checked before every activation anyway, because
    this is the one number whose being non-zero means a cross-tenant leak.
    """
    result = await session.execute(
        select(func.count(Chunk.id)).where(
            Chunk.document_version_id == document_version_id,
            Chunk.organization_id != organization_id,
        )
    )
    return int(result.scalar_one())
