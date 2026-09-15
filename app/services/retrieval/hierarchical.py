"""Merging knowledge across the tenant hierarchy.

An organization stores shared knowledge once; a location stores only what
differs. At answer time both are retrieved and merged so that location-specific
facts win on the subjects they cover, while unrelated organization knowledge is
preserved rather than thrown away.

The merge does three things, in order:

1. **Boost** location passages slightly, so they win ties and near-ties. The
   boost is deliberately small: fusion scores are compressed (with RRF, the gap
   between consecutive ranks is under 2%), so a large boost would push an
   irrelevant location passage above a relevant organization one. Preference is
   not the override mechanism; suppression is.
2. **Suppress** organization passages whose subject a location passage already
   covers -- this is what prevents "breakfast is 7-10" and "breakfast is 7-11"
   both reaching the model, where no amount of prompting reliably picks one.
3. **Keep** everything else.

Suppression is deliberately conservative. It fires on an exact topic-key match
or a strict subset relationship, and nothing looser. A missed override degrades
to "both passages present, location ranked first and labelled by scope", which
the answer prompt handles; a wrong suppression silently deletes a correct
answer. Those failure modes are not symmetric, so the matching is strict.

Nothing here is hardcoded to two levels: the merge takes an ordered list of
scoped result sets, so a region or brand tier drops in without changing it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from app.core.config import RetrievalSettings
from app.core.enums import KnowledgeScope
from app.core.logging import get_logger
from app.providers.search.base import SearchHit
from app.services.ingestion.chunking.headings import topic_tokens

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ScopedResults:
    """One level of the hierarchy's hits, with its precedence."""

    scope: KnowledgeScope
    hits: Sequence[SearchHit]
    #: Higher wins. Defaults to the scope's own ordering.
    precedence: int | None = None

    @property
    def rank(self) -> int:
        return self.scope.precedence if self.precedence is None else self.precedence


@dataclass(slots=True)
class MergedResults:
    hits: list[SearchHit]
    suppressed: list[SearchHit] = field(default_factory=list)
    #: topic key -> (winning scope, number of passages suppressed)
    overrides: dict[str, tuple[str, int]] = field(default_factory=dict)

    @property
    def suppressed_count(self) -> int:
        return len(self.suppressed)

    def trace(self) -> dict[str, object]:
        return {
            "kept": len(self.hits),
            "suppressed": len(self.suppressed),
            "overridden_topics": sorted(self.overrides),
        }


def _topics_of(hit: SearchHit) -> frozenset[str]:
    """Subject tokens for a hit, from its topic key or its heading."""
    if hit.topic_key:
        return frozenset(hit.topic_key.split("-"))
    if hit.heading:
        return topic_tokens(hit.heading)
    if hit.section_path:
        return topic_tokens(hit.section_path[-1])
    return frozenset()


def _same_subject(a: frozenset[str], b: frozenset[str]) -> bool:
    """Whether two topic token sets describe the same subject.

    Exact match, or one a strict subset of the other -- "breakfast" covers
    "breakfast weekend", so a location's weekend-breakfast section legitimately
    overrides the organization's general breakfast section. Partial overlap
    ("check-in" vs "check-out" share "check") is explicitly not enough.
    """
    if not a or not b:
        return False
    return a == b or a < b or b < a


def merge_scoped(
    scoped: Sequence[ScopedResults],
    settings: RetrievalSettings,
    *,
    top_k: int | None = None,
) -> MergedResults:
    """Merge results from several levels, applying precedence and suppression."""
    ordered = sorted(scoped, key=lambda s: s.rank, reverse=True)

    kept: list[SearchHit] = []
    suppressed: list[SearchHit] = []
    overrides: dict[str, tuple[str, int]] = {}
    seen_chunks: set[object] = set()
    # Subjects already claimed by a higher-precedence level.
    claimed: list[tuple[frozenset[str], str]] = []

    for level in ordered:
        level_claims: list[tuple[frozenset[str], str]] = []

        for hit in level.hits:
            if hit.chunk_id in seen_chunks:
                continue
            seen_chunks.add(hit.chunk_id)

            topics = _topics_of(hit)
            overriding = next(
                (
                    (claimed_topics, scope)
                    for claimed_topics, scope in claimed
                    if _same_subject(topics, claimed_topics)
                ),
                None,
            )
            if overriding is not None:
                suppressed.append(hit)
                key = "-".join(sorted(topics))
                winner, count = overrides.get(key, (overriding[1], 0))
                overrides[key] = (winner, count + 1)
                continue

            boost = settings.location_boost if level.scope is KnowledgeScope.LOCATION else 1.0
            kept.append(hit.with_score(hit.score * boost, boost=boost))
            if topics:
                level_claims.append((topics, level.scope.value))

        # Claims only take effect after the whole level is processed, so two
        # passages from the same level on the same subject never suppress each
        # other -- they are equally authoritative.
        claimed.extend(level_claims)

    kept.sort(key=lambda h: h.score, reverse=True)
    if top_k is not None:
        kept = kept[:top_k]

    if suppressed:
        log.info(
            "hierarchy_override_applied",
            suppressed=len(suppressed),
            topics=sorted(overrides),
        )

    return MergedResults(hits=kept, suppressed=suppressed, overrides=overrides)
