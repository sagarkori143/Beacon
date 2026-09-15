"""Embedding space registry and the startup guard.

An embedding space is a ``(provider type, model, dimension)`` triple. Vectors
are only comparable within one. Every chunk is stamped with its space and every
search filters on the current one.

The guard this module provides exists because of a failure mode with no error
message: swap the embedding model on the model server for a different one of the
same dimension, and every insert succeeds, every query runs, and retrieval
quality collapses. Nothing raises. So the process refuses to start when the
declared dimension, the current space and what the live provider actually
returns are not all in agreement.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import EmbeddingSpaceMismatch
from app.core.logging import get_logger
from app.models.chunk import EmbeddingSpace
from app.providers.embeddings.base import EmbeddingProvider

log = get_logger(__name__)


async def get_current_space(session: AsyncSession) -> EmbeddingSpace | None:
    result = await session.execute(
        select(EmbeddingSpace).where(EmbeddingSpace.is_current.is_(True))
    )
    return result.scalar_one_or_none()


async def get_space(session: AsyncSession, space_id: UUID) -> EmbeddingSpace | None:
    return await session.get(EmbeddingSpace, space_id)


async def ensure_space(
    session: AsyncSession,
    *,
    provider_type: str,
    model: str,
    dimension: int,
    normalized: bool = True,
    make_current: bool = True,
) -> EmbeddingSpace:
    """Find or create the space for a model, optionally making it current."""
    result = await session.execute(
        select(EmbeddingSpace).where(
            EmbeddingSpace.provider_type == provider_type,
            EmbeddingSpace.model == model,
            EmbeddingSpace.dimension == dimension,
        )
    )
    space = result.scalar_one_or_none()

    if space is None:
        space = EmbeddingSpace(
            provider_type=provider_type,
            model=model,
            dimension=dimension,
            normalized=normalized,
            is_current=False,
        )
        session.add(space)
        await session.flush()
        log.info("embedding_space_created", space=space.describe())

    if make_current and not space.is_current:
        # Demote first: the partial unique index on is_current is not deferrable,
        # so promoting before demoting would violate it mid-transaction.
        await session.execute(
            update(EmbeddingSpace)
            .where(EmbeddingSpace.is_current.is_(True))
            .values(is_current=False)
        )
        space.is_current = True
        await session.flush()
        log.info("embedding_space_activated", space=space.describe())

    return space


async def column_dimension(session: AsyncSession) -> int | None:
    """Read the declared dimension of ``chunks.embedding`` from the catalog.

    ``atttypmod`` holds the vector's dimension, which is the authoritative
    answer for what the schema will actually accept.
    """
    result = await session.execute(
        text(
            "SELECT atttypmod FROM pg_attribute "
            "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
        )
    )
    value = result.scalar_one_or_none()
    return int(value) if value and value > 0 else None


async def verify_embedding_space(
    session: AsyncSession,
    provider: EmbeddingProvider,
    *,
    configured_dimension: int,
    probe: bool = True,
) -> EmbeddingSpace:
    """Boot guard. Raises :class:`EmbeddingSpaceMismatch` rather than starting.

    Three sources must agree:

    1. ``EMBEDDING_DIM`` and the vector column built from it at migration time.
    2. The provider's declared dimension.
    3. What the provider's model actually returns, if it is reachable.

    When the model server is unreachable the probe is skipped and the process
    still starts -- an unreachable GPU box degrades chat, it does not justify
    refusing to serve document management or search over already-indexed data.
    """
    if provider.dimension != configured_dimension:
        raise EmbeddingSpaceMismatch(
            f"Embedding provider '{provider.name}' declares {provider.dimension} dimensions "
            f"but EMBEDDING_DIM is {configured_dimension}. These must match; the vector "
            f"column was created with the latter.",
            details={"provider": provider.dimension, "configured": configured_dimension},
        )

    declared = await column_dimension(session)
    if declared is not None and declared != configured_dimension:
        raise EmbeddingSpaceMismatch(
            f"chunks.embedding is vector({declared}) but EMBEDDING_DIM is "
            f"{configured_dimension}. Changing the embedding model requires the "
            f"re-embedding procedure in docs/providers.md, not an env edit.",
            details={"column": declared, "configured": configured_dimension},
        )

    if probe:
        health = await provider.health()
        actual = health.extra.get("dimension")
        if health.ok and isinstance(actual, int) and actual != configured_dimension:
            raise EmbeddingSpaceMismatch(
                f"Model '{provider.model}' on the model server returns {actual} dimensions, "
                f"but this deployment is built for {configured_dimension}. Writing these "
                f"vectors would silently corrupt retrieval quality.",
                details={"actual": actual, "configured": configured_dimension},
            )
        if not health.ok:
            log.warning(
                "embedding_probe_skipped",
                reason=health.detail,
                hint="model server unreachable; dimension not verified against live model",
            )

    space = await ensure_space(
        session,
        provider_type=provider.provider_type,
        model=provider.model,
        dimension=provider.dimension,
        normalized=getattr(provider, "normalize", True),
        make_current=False,
    )

    current = await get_current_space(session)
    if current is None:
        # First boot: adopt the configured provider's space.
        await ensure_space(
            session,
            provider_type=provider.provider_type,
            model=provider.model,
            dimension=provider.dimension,
            normalized=getattr(provider, "normalize", True),
            make_current=True,
        )
        return space

    if current.id != space.id:
        raise EmbeddingSpaceMismatch(
            f"Indexed vectors belong to embedding space {current.describe()}, but the "
            f"configured provider is {space.describe()}. The two are not comparable. "
            f"Follow the re-embedding procedure in docs/providers.md.",
            details={"indexed": current.describe(), "configured": space.describe()},
        )
    return space
