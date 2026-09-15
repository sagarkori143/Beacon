"""pgvector-backed chunk writes.

Two details here are deliberate and worth stating:

**Chunks are inserted with ``is_active = false``.** A worker that dies at 80% of
EMBEDDING leaves rows behind, and those rows are invisible to every search
because visibility is a column, not an absence. Activation flips them in the
same transaction that promotes the version. There is no cleanup path to forget
to run.

**Vectors are bound as text and cast twice.** ``CAST(:v AS vector)`` alone
makes asyncpg infer the parameter's type as ``vector``, which then requires
pgvector's codec to be registered on that connection and a Python list to be
passed. Going through ``text`` first pins the parameter type to something
asyncpg always knows how to send, so inserts work identically whether or not the
codec registered -- the alternative is an insert that fails with a type error
only on connections where registration happened to be skipped.

**The lexical vector is built in the INSERT**, not by a trigger and not by a
generated column. ``to_tsvector`` needs a text-search configuration, and the
right one depends on the document's language -- which makes the expression
non-immutable and therefore illegal in a generated column. Building it here
keeps per-language stemming while leaving the language choice in Python, next to
the search code that has to make the identical choice.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.providers.base import ProviderHealth
from app.providers.search.postgres_hybrid import ts_config_for
from app.providers.vector_store.base import (
    ChunkRecord,
    VectorStore,
    register_vector_store,
)

log = get_logger(__name__)

#: Rows per INSERT. Large enough to amortize round trips, small enough that one
#: statement's parameter list stays well inside PostgreSQL's 65535 limit
#: (19 parameters per row).
_INSERT_BATCH = 200

_INSERT_SQL = """
INSERT INTO chunks (
    id, organization_id, location_id, document_id, document_version_id,
    document_version, ordinal, source_type, source_name, page_from, page_to,
    heading, section_path, section_key, topic_key, kind,
    content, content_hash, token_count, language,
    embedding_space_id, embedding, search_vector, is_active, meta,
    created_at, updated_at
) VALUES (
    :id, :organization_id, :location_id, :document_id, :document_version_id,
    :document_version, :ordinal, :source_type, :source_name, :page_from, :page_to,
    :heading, CAST(:section_path AS jsonb), :section_key, :topic_key, :kind,
    :content, :content_hash, :token_count, :language,
    :embedding_space_id, CAST(CAST(:embedding AS text) AS vector),
    setweight(to_tsvector(CAST(:ts_config AS regconfig), COALESCE(:heading_text, '')), 'A')
      || setweight(to_tsvector(CAST(:ts_config AS regconfig), :content), 'B'),
    FALSE, CAST(:meta AS jsonb),
    now(), now()
)
ON CONFLICT (document_version_id, ordinal) DO UPDATE SET
    content        = EXCLUDED.content,
    content_hash   = EXCLUDED.content_hash,
    token_count    = EXCLUDED.token_count,
    embedding      = EXCLUDED.embedding,
    search_vector  = EXCLUDED.search_vector,
    heading        = EXCLUDED.heading,
    section_path   = EXCLUDED.section_path,
    section_key    = EXCLUDED.section_key,
    topic_key      = EXCLUDED.topic_key,
    meta           = EXCLUDED.meta,
    updated_at     = now()
"""


@register_vector_store("pgvector")
class PgVectorStore(VectorStore):
    name = "pgvector"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.default_ts_config = settings.retrieval.default_text_search_config

    async def upsert(self, session: AsyncSession, records: Sequence[ChunkRecord]) -> int:
        """Insert or replace chunks.

        The ``ON CONFLICT (document_version_id, ordinal)`` clause is what makes
        the INDEXING stage idempotent: a redelivered queue message re-runs the
        insert and lands on the same rows instead of duplicating the document.
        """
        if not records:
            return 0

        written = 0
        for start in range(0, len(records), _INSERT_BATCH):
            batch = records[start : start + _INSERT_BATCH]
            await session.execute(text(_INSERT_SQL), [self._params(record) for record in batch])
            written += len(batch)
        return written

    async def delete_by_version(self, session: AsyncSession, document_version_id: UUID) -> int:
        result = await session.execute(
            text("DELETE FROM chunks WHERE document_version_id = :vid"),
            {"vid": document_version_id},
        )
        return result.rowcount or 0

    async def set_active(
        self, session: AsyncSession, document_version_id: UUID, *, active: bool
    ) -> int:
        result = await session.execute(
            text(
                "UPDATE chunks SET is_active = :active, updated_at = now() "
                "WHERE document_version_id = :vid AND is_active <> :active"
            ),
            {"vid": document_version_id, "active": active},
        )
        return result.rowcount or 0

    async def count_for_version(
        self, session: AsyncSession, document_version_id: UUID, *, only_embedded: bool = False
    ) -> int:
        # The interpolated fragment is one of two literals chosen here; the
        # version id is a bound parameter.
        clause = " AND embedding IS NOT NULL" if only_embedded else ""
        result = await session.execute(
            text(f"SELECT count(*) FROM chunks WHERE document_version_id = :vid{clause}"),  # noqa: S608
            {"vid": document_version_id},
        )
        return int(result.scalar_one())

    async def health(self) -> ProviderHealth:
        started = time.perf_counter()
        return ProviderHealth(
            name=self.name,
            ok=True,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            extra={"dimension": self.settings.embedding_dim},
        )

    # -- helpers -------------------------------------------------------------

    def _params(self, record: ChunkRecord) -> dict[str, object]:
        import json

        return {
            "id": record.id,
            "organization_id": record.organization_id,
            "location_id": record.location_id,
            "document_id": record.document_id,
            "document_version_id": record.document_version_id,
            "document_version": record.document_version,
            "ordinal": record.ordinal,
            "source_type": record.source_type.value,
            "source_name": record.source_name,
            "page_from": record.page_from,
            "page_to": record.page_to,
            "heading": record.heading,
            # Also passed separately because the tsvector expression weights the
            # heading at 'A' and must not receive NULL.
            "heading_text": record.heading or "",
            "section_path": json.dumps(record.section_path),
            "section_key": record.section_key,
            "topic_key": record.topic_key,
            "kind": record.kind,
            "content": record.content,
            "content_hash": record.content_hash,
            "token_count": record.token_count,
            "language": record.language,
            "embedding_space_id": record.embedding_space_id,
            "embedding": "[" + ",".join(f"{v:.8g}" for v in record.embedding) + "]",
            "ts_config": ts_config_for(record.language, self.default_ts_config),
            "meta": json.dumps(record.meta),
        }
