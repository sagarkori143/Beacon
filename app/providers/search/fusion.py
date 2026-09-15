"""Score fusion for hybrid search.

Two arms -- dense vector similarity and lexical ``ts_rank_cd`` -- produce scores
on completely different and unstable scales. Combining them needs a policy, and
the policy is configurable because no single weighting is right for every corpus.

**Reciprocal Rank Fusion is the default.** It consumes ranks rather than scores,
so it is immune to the failure that makes naive weighted-sum fusion unreliable:
when one arm returns a tight cluster of near-identical scores (very common for
cosine similarity over a small tenant), min-max normalization stretches
meaningless differences across the full 0-1 range and that arm dominates. RRF
cannot do that. It also degrades gracefully when one arm returns nothing at all.

``WeightedScoreFusion`` is available for corpora where the score magnitudes
genuinely carry signal and an operator wants to tune the balance directly.

Both are expressed as SQL so fusion happens in the same round trip as retrieval,
and both are also available as pure functions for merging results across several
query rewrites, which happens outside SQL.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

from app.core.config import RetrievalSettings
from app.core.enums import FusionStrategy as FusionStrategyName
from app.providers.search.base import SearchHit


class Fusion(ABC):
    """Combines per-arm ranks/scores into one ordering."""

    name: str

    @property
    @abstractmethod
    def needs_normalized_scores(self) -> bool:
        """Whether the arm CTEs must also compute min-max normalized scores."""

    @abstractmethod
    def sql_expression(self) -> str:
        """SQL computing the combined score from the fused CTE's columns."""

    @abstractmethod
    def bind_params(self) -> dict[str, Any]:
        """Bind parameters referenced by :meth:`sql_expression`."""

    @abstractmethod
    def combine_lists(self, ranked_lists: Sequence[Sequence[SearchHit]]) -> list[SearchHit]:
        """Fuse several already-ranked result lists (e.g. one per query rewrite)."""


class ReciprocalRankFusion(Fusion):
    name = "rrf"

    def __init__(self, *, k: int = 60, vector_weight: float = 1.0, keyword_weight: float = 1.0):
        # k dampens the head of the distribution: with k=60 the difference
        # between rank 1 and rank 2 is small enough that a document found by
        # both arms outranks one found first by a single arm.
        self.k = k
        self.vector_weight = vector_weight
        self.keyword_weight = keyword_weight

    @property
    def needs_normalized_scores(self) -> bool:
        return False

    def sql_expression(self) -> str:
        return (
            "(:vector_weight * COALESCE(1.0 / (:rrf_k + f.vector_rank), 0.0)"
            " + :keyword_weight * COALESCE(1.0 / (:rrf_k + f.keyword_rank), 0.0))"
        )

    def bind_params(self) -> dict[str, Any]:
        return {
            "rrf_k": self.k,
            "vector_weight": self.vector_weight,
            "keyword_weight": self.keyword_weight,
        }

    def combine_lists(self, ranked_lists: Sequence[Sequence[SearchHit]]) -> list[SearchHit]:
        scores: dict[Any, float] = {}
        best: dict[Any, SearchHit] = {}

        for hits in ranked_lists:
            for rank, hit in enumerate(hits, start=1):
                scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (self.k + rank)
                # Keep the instance from the list where it scored best, so its
                # per-arm diagnostics survive into the trace.
                if hit.chunk_id not in best or hit.score > best[hit.chunk_id].score:
                    best[hit.chunk_id] = hit

        return sorted(
            (best[cid].with_score(score) for cid, score in scores.items()),
            key=lambda h: h.score,
            reverse=True,
        )


class WeightedScoreFusion(Fusion):
    name = "weighted"

    def __init__(self, *, vector_weight: float = 0.6, keyword_weight: float = 0.4):
        total = vector_weight + keyword_weight
        # Normalize the weights so the combined score stays in 0-1 regardless of
        # what the operator configured.
        self.vector_weight = vector_weight / total
        self.keyword_weight = keyword_weight / total

    @property
    def needs_normalized_scores(self) -> bool:
        return True

    def sql_expression(self) -> str:
        return (
            "(:vector_weight * COALESCE(f.vector_norm, 0.0)"
            " + :keyword_weight * COALESCE(f.keyword_norm, 0.0))"
        )

    def bind_params(self) -> dict[str, Any]:
        return {
            "vector_weight": self.vector_weight,
            "keyword_weight": self.keyword_weight,
        }

    def combine_lists(self, ranked_lists: Sequence[Sequence[SearchHit]]) -> list[SearchHit]:
        """Max-pool across lists.

        Summing would reward a chunk merely for appearing in several rewrites of
        the same question, which is not evidence of relevance -- the rewrites are
        paraphrases of one another.
        """
        best: dict[Any, SearchHit] = {}
        for hits in ranked_lists:
            for hit in hits:
                current = best.get(hit.chunk_id)
                if current is None or hit.score > current.score:
                    best[hit.chunk_id] = hit
        return sorted(best.values(), key=lambda h: h.score, reverse=True)


def build_fusion(settings: RetrievalSettings) -> Fusion:
    if settings.fusion is FusionStrategyName.WEIGHTED:
        return WeightedScoreFusion(
            vector_weight=settings.vector_weight,
            keyword_weight=settings.keyword_weight,
        )
    return ReciprocalRankFusion(
        k=settings.rrf_k,
        vector_weight=settings.vector_weight,
        keyword_weight=settings.keyword_weight,
    )
