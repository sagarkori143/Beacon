"""Fusion and hierarchical merging, with no database involved."""

from __future__ import annotations

import uuid

import pytest

from app.core.config import RetrievalSettings
from app.core.enums import FusionStrategy, KnowledgeScope
from app.providers.search.base import SearchHit
from app.providers.search.fusion import (
    ReciprocalRankFusion,
    WeightedScoreFusion,
    build_fusion,
)
from app.services.retrieval.hierarchical import ScopedResults, merge_scoped

pytestmark = pytest.mark.unit

ORG = uuid.uuid4()
LOCATION = uuid.uuid4()


def hit(
    *,
    heading: str,
    topic: str | None,
    score: float,
    location: uuid.UUID | None = None,
    content: str = "content",
) -> SearchHit:
    return SearchHit(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_version_id=uuid.uuid4(),
        document_version=1,
        organization_id=ORG,
        location_id=location,
        content=content,
        heading=heading,
        section_path=(heading,),
        section_key=None,
        topic_key=topic,
        source_name="doc.md",
        source_type="MARKDOWN",
        page_from=1,
        page_to=1,
        language="en",
        token_count=50,
        content_hash=uuid.uuid4().hex,
        score=score,
    )


class TestFusionStrategies:
    def test_rrf_rewards_agreement_between_arms(self) -> None:
        """A document both arms found should beat one only a single arm found."""
        fusion = ReciprocalRankFusion(k=60)
        both = hit(heading="Both", topic="both", score=0.0)
        vector_only = hit(heading="Vector", topic="v", score=0.0)
        keyword_only = hit(heading="Keyword", topic="k", score=0.0)

        fused = fusion.combine_lists([[vector_only, both], [keyword_only, both]])
        assert fused[0].chunk_id == both.chunk_id

    def test_rrf_survives_an_empty_arm(self) -> None:
        """Lexical search finds nothing for a purely semantic query.

        Weighted-score fusion would need special-casing here; RRF does not.
        """
        fusion = ReciprocalRankFusion(k=60)
        only = hit(heading="A", topic="a", score=0.5)
        assert fusion.combine_lists([[only], []])[0].chunk_id == only.chunk_id

    def test_weighted_fusion_max_pools_across_rewrites(self) -> None:
        """Three paraphrases of one question are not three pieces of evidence."""
        fusion = WeightedScoreFusion()
        low = hit(heading="A", topic="a", score=0.2)
        high = hit(heading="B", topic="b", score=0.9)
        fused = fusion.combine_lists([[low], [low], [high]])
        assert fused[0].chunk_id == high.chunk_id

    def test_build_fusion_honours_configuration(self) -> None:
        assert isinstance(
            build_fusion(RetrievalSettings(fusion=FusionStrategy.RRF)),
            ReciprocalRankFusion,
        )
        assert isinstance(
            build_fusion(RetrievalSettings(fusion=FusionStrategy.WEIGHTED)),
            WeightedScoreFusion,
        )

    def test_sql_expressions_reference_declared_parameters(self) -> None:
        """The SQL fragment and its bind parameters must agree."""
        for fusion in (ReciprocalRankFusion(), WeightedScoreFusion()):
            sql = fusion.sql_expression()
            for name in fusion.bind_params():
                assert f":{name}" in sql


class TestHierarchicalMerge:
    def test_location_passage_suppresses_the_org_passage_on_the_same_topic(self) -> None:
        settings = RetrievalSettings()
        merged = merge_scoped(
            [
                ScopedResults(
                    KnowledgeScope.LOCATION,
                    [
                        hit(
                            heading="Breakfast Hours",
                            topic="breakfast",
                            score=0.5,
                            location=LOCATION,
                            content="7:00 AM to 11:00 AM",
                        )
                    ],
                ),
                ScopedResults(
                    KnowledgeScope.ORGANIZATION,
                    [
                        hit(
                            heading="Breakfast Service",
                            topic="breakfast",
                            score=0.9,
                            content="7:00 AM to 10:00 AM",
                        )
                    ],
                ),
            ],
            settings,
        )
        assert len(merged.hits) == 1
        assert "11:00 AM" in merged.hits[0].content
        assert merged.suppressed_count == 1
        assert "breakfast" in merged.overrides

    def test_unrelated_org_knowledge_is_kept(self) -> None:
        """An override on one subject must not discard the rest of the handbook."""
        merged = merge_scoped(
            [
                ScopedResults(
                    KnowledgeScope.LOCATION,
                    [hit(heading="Breakfast", topic="breakfast", score=0.5, location=LOCATION)],
                ),
                ScopedResults(
                    KnowledgeScope.ORGANIZATION,
                    [
                        hit(heading="Breakfast", topic="breakfast", score=0.4),
                        hit(heading="Cancellation", topic="cancellation", score=0.4),
                        hit(heading="Smoking", topic="smoking", score=0.3),
                    ],
                ),
            ],
            RetrievalSettings(),
        )
        kept = {h.heading for h in merged.hits}
        assert kept == {"Breakfast", "Cancellation", "Smoking"}
        assert merged.suppressed_count == 1

    def test_partial_topic_overlap_does_not_suppress(self) -> None:
        """check-in and check-out share a word but are different subjects."""
        merged = merge_scoped(
            [
                ScopedResults(
                    KnowledgeScope.LOCATION,
                    [hit(heading="Check-in", topic="check-in", score=0.5, location=LOCATION)],
                ),
                ScopedResults(
                    KnowledgeScope.ORGANIZATION,
                    [hit(heading="Check-out", topic="check-out", score=0.5)],
                ),
            ],
            RetrievalSettings(),
        )
        assert merged.suppressed_count == 0
        assert len(merged.hits) == 2

    def test_subset_topics_are_treated_as_the_same_subject(self) -> None:
        """'breakfast weekend' is covered by 'breakfast'."""
        merged = merge_scoped(
            [
                ScopedResults(
                    KnowledgeScope.LOCATION,
                    [
                        hit(
                            heading="Weekend Breakfast",
                            topic="breakfast-weekend",
                            score=0.5,
                            location=LOCATION,
                        )
                    ],
                ),
                ScopedResults(
                    KnowledgeScope.ORGANIZATION,
                    [hit(heading="Breakfast", topic="breakfast", score=0.9)],
                ),
            ],
            RetrievalSettings(),
        )
        assert merged.suppressed_count == 1

    def test_two_passages_at_the_same_level_never_suppress_each_other(self) -> None:
        """Both halves of one section are equally authoritative."""
        merged = merge_scoped(
            [
                ScopedResults(
                    KnowledgeScope.ORGANIZATION,
                    [
                        hit(heading="Breakfast", topic="breakfast", score=0.9),
                        hit(heading="Breakfast", topic="breakfast", score=0.8),
                    ],
                )
            ],
            RetrievalSettings(),
        )
        assert len(merged.hits) == 2
        assert merged.suppressed_count == 0

    @pytest.mark.parametrize(
        ("location_rank", "expected_first"),
        [
            (2, "Parking"),  # a near-tie: preference applies
            (4, "Breakfast"),  # clearly less relevant: preference must not apply
            (8, "Breakfast"),
        ],
    )
    def test_location_boost_only_wins_near_ties(
        self, location_rank: int, expected_first: str
    ) -> None:
        """RRF scores are compressed, so the boost has to be small.

        At k=60 a hit at rank r scores 1/(60+r). The default boost is sized so
        that only a rank-2 passage can overtake rank 1 -- otherwise an
        irrelevant location passage buries relevant organization content.
        """
        settings = RetrievalSettings()
        merged = merge_scoped(
            [
                ScopedResults(
                    KnowledgeScope.LOCATION,
                    [
                        hit(
                            heading="Parking",
                            topic="parking",
                            score=1 / (60 + location_rank),
                            location=LOCATION,
                        )
                    ],
                ),
                ScopedResults(
                    KnowledgeScope.ORGANIZATION,
                    [hit(heading="Breakfast", topic="breakfast", score=1 / 61)],
                ),
            ],
            settings,
        )
        assert merged.hits[0].heading == expected_first

    def test_top_k_is_applied_after_suppression(self) -> None:
        merged = merge_scoped(
            [
                ScopedResults(
                    KnowledgeScope.ORGANIZATION,
                    [hit(heading=f"H{i}", topic=f"t{i}", score=1.0 - i / 100) for i in range(20)],
                )
            ],
            RetrievalSettings(),
            top_k=5,
        )
        assert len(merged.hits) == 5
