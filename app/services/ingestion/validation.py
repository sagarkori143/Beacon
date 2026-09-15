"""Pre-activation validation gates.

Every gate runs inside the activation transaction, holding the document's row
lock. That placement is the point: a version that cannot be retrieved can never
replace one that could, and there is no window between "validated" and
"activated" in which something could change.

The gates are ordered cheapest-first so an obviously broken version fails
without running a retrieval smoke test, and each returns a reason rather than a
boolean so the failure lands on the job in a form an operator can act on.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.document import DocumentVersion
from app.providers.search.base import ScopeMode, SearchFilters, SearchProvider
from app.repositories.chunk import foreign_tenant_rows, validation_stats

log = get_logger(__name__)


@dataclass(slots=True)
class GateOutcome:
    name: str
    passed: bool
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ValidationReport:
    outcomes: list[GateOutcome] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(o.passed for o in self.outcomes)

    @property
    def failures(self) -> list[GateOutcome]:
        return [o for o in self.outcomes if not o.passed]

    def summary(self) -> str:
        if self.passed:
            return f"all {len(self.outcomes)} validation gates passed"
        return "; ".join(f"{o.name}: {o.detail}" for o in self.failures)

    def to_detail(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "gates": [
                {"name": o.name, "passed": o.passed, "detail": o.detail, **o.data}
                for o in self.outcomes
            ],
        }


class Gate(ABC):
    name: str

    @abstractmethod
    async def check(self, session: AsyncSession, version: DocumentVersion) -> GateOutcome: ...


class ChunkCountGate(Gate):
    """The version produced at least one chunk.

    A document that yields zero chunks is a failed parse or an empty OCR result,
    not a document with nothing to say. Activating it would silently replace a
    working version with one that answers nothing -- the single worst outcome
    the versioning design exists to prevent.
    """

    name = "chunk_count"

    def __init__(self, minimum: int = 1) -> None:
        self.minimum = minimum

    async def check(self, session: AsyncSession, version: DocumentVersion) -> GateOutcome:
        stats = await validation_stats(session, version.id)
        total = stats["total"]
        return GateOutcome(
            name=self.name,
            passed=total >= self.minimum,
            detail=f"{total} chunks (minimum {self.minimum})",
            data={"chunk_count": total},
        )


class EmbeddingGate(Gate):
    """Every chunk has an embedding, and they all share one embedding space."""

    name = "embeddings"

    async def check(self, session: AsyncSession, version: DocumentVersion) -> GateOutcome:
        stats = await validation_stats(session, version.id)
        missing = stats["missing_embedding"]
        spaces = stats["distinct_spaces"]

        if missing:
            return GateOutcome(
                self.name, False, f"{missing} chunk(s) have no embedding", {"missing": missing}
            )
        if spaces > 1:
            return GateOutcome(
                self.name,
                False,
                f"chunks span {spaces} embedding spaces; they are not comparable",
                {"distinct_spaces": spaces},
            )
        return GateOutcome(self.name, True, "all chunks embedded in one space")


class LexicalIndexGate(Gate):
    """Every chunk has a populated tsvector.

    Without it the lexical arm silently returns nothing for this document, and
    hybrid search quietly degrades to vector-only -- which looks like "retrieval
    got worse" rather than like a bug.
    """

    name = "lexical_index"

    async def check(self, session: AsyncSession, version: DocumentVersion) -> GateOutcome:
        stats = await validation_stats(session, version.id)
        missing = stats["missing_search_vector"]
        return GateOutcome(
            self.name,
            missing == 0,
            f"{missing} chunk(s) have no lexical index entry",
            {"missing": missing},
        )


class ContentGate(Gate):
    """No empty chunks."""

    name = "content"

    async def check(self, session: AsyncSession, version: DocumentVersion) -> GateOutcome:
        stats = await validation_stats(session, version.id)
        empty = stats["empty_content"]
        return GateOutcome(
            self.name, empty == 0, f"{empty} chunk(s) have empty content", {"empty": empty}
        )


class TenantIsolationGate(Gate):
    """No chunk of this version belongs to another organization.

    This number should be structurally impossible to make non-zero. It is
    checked before every activation anyway, because it is the one value whose
    being wrong means a cross-tenant leak, and a cheap query is a small price
    for catching that before the data becomes visible.
    """

    name = "tenant_isolation"

    async def check(self, session: AsyncSession, version: DocumentVersion) -> GateOutcome:
        foreign = await foreign_tenant_rows(session, version.id, version.organization_id)
        stats = await validation_stats(session, version.id)
        distinct = stats["distinct_orgs"]

        passed = foreign == 0 and distinct <= 1
        if not passed:
            log.error(
                "tenant_isolation_gate_failed",
                version_id=str(version.id),
                organization_id=str(version.organization_id),
                foreign_rows=foreign,
                distinct_orgs=distinct,
            )
        return GateOutcome(
            self.name,
            passed,
            f"{foreign} chunk(s) belong to another organization",
            {"foreign_rows": foreign, "distinct_orgs": distinct},
        )


class ScopeConsistencyGate(Gate):
    """Chunks carry the same location scope as the version that owns them."""

    name = "scope_consistency"

    async def check(self, session: AsyncSession, version: DocumentVersion) -> GateOutcome:
        from sqlalchemy import func, select

        from app.models.chunk import Chunk

        expected = version.location_id
        condition = (
            Chunk.location_id.is_not(None) if expected is None else Chunk.location_id != expected
        )
        result = await session.execute(
            select(func.count(Chunk.id)).where(Chunk.document_version_id == version.id, condition)
        )
        mismatched = int(result.scalar_one())
        return GateOutcome(
            self.name,
            mismatched == 0,
            f"{mismatched} chunk(s) have the wrong location scope",
            {"mismatched": mismatched},
        )


class SmokeRetrievalGate(Gate):
    """The new version's chunks are actually retrievable.

    The only gate that exercises the real read path, and the only one that can
    catch a tsvector built with the wrong text-search configuration or an index
    that exists but matches nothing. The counting gates would pass happily in
    both cases.

    It goes through ``SearchProvider.search`` rather than its own SQL, for two
    reasons. The gate then tests the code that will actually serve queries, not
    a parallel implementation that could drift from it -- and the validation
    logic stays free of any one search backend's dialect.

    Lexical only: passing ``embedding=None`` means no call to the model server,
    so a GPU box that went down mid-pipeline cannot block the activation of work
    that already succeeded.
    """

    name = "smoke_retrieval"

    def __init__(self, search: SearchProvider | None = None) -> None:
        self.search = search

    async def check(self, session: AsyncSession, version: DocumentVersion) -> GateOutcome:
        if self.search is None:
            return GateOutcome(self.name, True, "skipped: no search provider supplied")

        probe = await self._probe_text(session, version)
        if probe is None:
            return GateOutcome(self.name, False, "no searchable chunk to probe with")

        result = await self.search.search(
            session,
            query=probe,
            embedding=None,
            filters=SearchFilters(
                organization_id=version.organization_id,
                location_id=version.location_id,
                scope_mode=(
                    ScopeMode.ORG_ONLY if version.location_id is None else ScopeMode.LOCATION_ONLY
                ),
                document_version_id=version.id,
                # The version is not active yet -- that is the whole point.
                include_inactive=True,
            ),
            top_k=3,
        )
        return GateOutcome(
            self.name,
            len(result.hits) > 0,
            f"probe query matched {len(result.hits)} chunk(s)",
            {"matches": len(result.hits), "probe_terms": len(probe.split())},
        )

    @staticmethod
    async def _probe_text(session: AsyncSession, version: DocumentVersion) -> str | None:
        """Take a few words from the version's longest chunk.

        The longest chunk is the one most likely to contain distinctive terms;
        a heading-only chunk can consist entirely of stopwords and match
        nothing even when indexing worked perfectly.
        """
        from sqlalchemy import select

        from app.models.chunk import Chunk

        result = await session.execute(
            select(Chunk.content)
            .where(Chunk.document_version_id == version.id, Chunk.content != "")
            .order_by(Chunk.token_count.desc())
            .limit(1)
        )
        content = result.scalar_one_or_none()
        if not content:
            return None
        # Skip the breadcrumb prefix: it repeats across every chunk and would
        # make the probe pass even if the body text were never indexed.
        body = content.split("\n\n", 1)[-1]
        words = [w for w in body.split() if len(w) > 3][:6]
        return " ".join(words) or None


def default_gates(search: SearchProvider | None = None) -> tuple[Gate, ...]:
    """The standard gate set, cheapest first.

    Ordering is deliberate: a version with no chunks fails before anything
    queries text, and the retrieval probe -- the only gate that runs a real
    query -- is last.
    """
    return (
        ChunkCountGate(),
        ContentGate(),
        EmbeddingGate(),
        LexicalIndexGate(),
        TenantIsolationGate(),
        ScopeConsistencyGate(),
        SmokeRetrievalGate(search),
    )


async def run_gates(
    session: AsyncSession,
    version: DocumentVersion,
    gates: tuple[Gate, ...] | None = None,
    *,
    search: SearchProvider | None = None,
) -> ValidationReport:
    """Run every gate, stopping at the first failure.

    Stopping early is right here: the gates are ordered so a later one's result
    is uninformative once an earlier one failed, and the job only needs the
    first real reason.
    """
    report = ValidationReport()
    for gate in gates or default_gates(search):
        outcome = await gate.check(session, version)
        report.outcomes.append(outcome)
        if not outcome.passed:
            log.warning(
                "validation_gate_failed",
                gate=gate.name,
                version_id=str(version.id),
                detail=outcome.detail,
            )
            break
    return report
