"""Chunking: structure preservation, token limits, and overlap."""

from __future__ import annotations

import pytest

from app.core.config import ChunkingSettings
from app.services.ingestion.chunking.chunker import chunk_document
from app.services.ingestion.chunking.headings import topic_key, topic_tokens
from app.services.ingestion.chunking.sections import build_section_tree
from app.services.ingestion.parsing.layout import LayoutLine, analyze_layout
from app.services.ingestion.parsing.pdf import parse_plain_text

pytestmark = pytest.mark.unit


def chunks_for(text: str, **overrides: object):
    parsed = parse_plain_text(text.encode("utf-8"))
    tree = build_section_tree(parsed.lines, parsed.stats)
    settings = ChunkingSettings(**overrides) if overrides else ChunkingSettings()
    return chunk_document(tree, settings, source_name="test.md")


class TestSectionPreservation:
    def test_distinct_sections_do_not_merge(self, sample_markdown: str) -> None:
        """Pet and smoking policies must never share a chunk.

        Conflating them is what breaks the location-override logic downstream,
        which identifies overriding content by subject.
        """
        chunks = chunks_for(sample_markdown)
        for chunk in chunks:
            content = chunk.content.lower()
            assert not ("assistance dogs" in content and "non-smoking" in content)

    def test_every_chunk_carries_its_breadcrumb(self, sample_markdown: str) -> None:
        for chunk in chunks_for(sample_markdown):
            assert chunk.section_path, "chunk lost its section path"
            assert chunk.content.startswith(" > ".join(chunk.section_path))

    def test_headings_become_section_paths(self, sample_markdown: str) -> None:
        paths = {" > ".join(c.section_path) for c in chunks_for(sample_markdown)}
        assert "Operations Handbook > Breakfast Service" in paths
        assert "Operations Handbook > Pet Policy" in paths

    def test_empty_document_yields_no_chunks(self) -> None:
        assert chunks_for("   \n\n   ") == []


class TestTokenLimits:
    def test_chunks_respect_the_hard_cap(self) -> None:
        body = "This is a sentence about hotel operations. " * 200
        chunks = chunks_for(
            f"# Handbook\n\n## Long Section\n\n{body}",
            target_tokens=100,
            max_tokens=150,
            min_tokens=20,
            overlap_tokens=20,
        )
        assert len(chunks) > 1
        # Allow headroom for the breadcrumb prefix, which is added after
        # windowing and is bounded by the heading length.
        assert all(c.token_count <= 200 for c in chunks)

    def test_overlap_reuses_whole_sentences(self) -> None:
        """Overlap should read as a lead-in, not a fragment."""
        sentences = " ".join(
            f"Statement number {i} concerns operational policy." for i in range(60)
        )
        chunks = chunks_for(
            f"# Handbook\n\n## Section\n\n{sentences}",
            target_tokens=80,
            max_tokens=120,
            min_tokens=20,
            overlap_tokens=24,
        )
        assert len(chunks) >= 2
        body = chunks[1].content.split("\n\n", 1)[-1]
        assert not body.startswith(("number", "concerns", "policy."))

    def test_oversized_paragraph_is_split_not_dropped(self) -> None:
        words = " ".join("alpha" for _ in range(800))
        chunks = chunks_for(
            f"# H\n\n## S\n\n{words}",
            target_tokens=50,
            max_tokens=80,
            min_tokens=10,
            overlap_tokens=10,
        )
        assert len(chunks) > 1
        # Overlap duplicates some words, so the total can exceed the original.
        assert sum(c.content.count("alpha") for c in chunks) >= 800


class TestRunningHeaders:
    def test_repeating_lines_are_not_treated_as_headings(self) -> None:
        """A running header is big, bold and short -- exactly like a heading.

        Without the repetition penalty every page starts a new top-level
        section and the document's real structure disappears.
        """
        lines = []
        for page in range(1, 7):
            lines.append(
                LayoutLine(
                    page_number=page,
                    text="SAGAR HOTELS CONFIDENTIAL",
                    x0=0,
                    x1=200,
                    top=0,
                    bottom=12,
                    max_font_size=16.0,
                    mode_font_size=16.0,
                    bold_ratio=1.0,
                    italic_ratio=0.0,
                    space_above=20.0,
                )
            )
            lines.append(
                LayoutLine(
                    page_number=page,
                    text=f"Body text for page {page} describing ordinary policy detail.",
                    x0=0,
                    x1=400,
                    top=20,
                    bottom=32,
                    max_font_size=10.0,
                    mode_font_size=10.0,
                    bold_ratio=0.0,
                    italic_ratio=0.0,
                    space_above=4.0,
                )
            )

        stats = analyze_layout(lines, page_count=6, page_width=612.0)
        assert "sagar hotels confidential" in stats.repeating_lines

        tree = build_section_tree(lines, stats)
        chunks = chunk_document(tree, ChunkingSettings(min_tokens=5), source_name="t.pdf")
        assert chunks, "content was lost along with the running header"
        assert all("CONFIDENTIAL" not in c.content for c in chunks)

    def test_near_identical_font_sizes_collapse_to_one_level(self) -> None:
        """17.9997pt and 18.0pt are the same heading level, not two."""
        lines = [
            LayoutLine(1, "Section One", 0, 100, 0, 12, 18.0, 18.0, 1.0, 0.0, 20.0),
            LayoutLine(1, "Body text here.", 0, 400, 20, 32, 10.0, 10.0, 0.0, 0.0, 4.0),
            LayoutLine(1, "Section Two", 0, 100, 40, 52, 17.9997, 17.9997, 1.0, 0.0, 20.0),
            LayoutLine(1, "More body text.", 0, 400, 60, 72, 10.0, 10.0, 0.0, 0.0, 4.0),
        ]
        stats = analyze_layout(lines, page_count=1, page_width=612.0)
        assert len(stats.heading_size_levels) == 1
        assert stats.size_level(18.0) == stats.size_level(17.9997)


class TestTopicKeys:
    """Topic keys drive location override, so their behaviour is load-bearing."""

    def test_generic_nouns_do_not_prevent_a_match(self) -> None:
        assert topic_key(("Org", "Breakfast Service")) == topic_key(("Ginza", "Breakfast Hours"))

    def test_different_subjects_stay_distinct(self) -> None:
        assert topic_key(("H", "Pet Policy")) != topic_key(("H", "Smoking Policy"))

    def test_check_in_and_check_out_are_not_conflated(self) -> None:
        """Directional words must survive normalization.

        Collapsing these would let a location's check-in section suppress the
        organization's check-out policy, deleting a correct answer.
        """
        assert topic_key(("H", "Check-in Time")) != topic_key(("H", "Check-out Time"))

    def test_only_the_deepest_heading_contributes(self) -> None:
        assert topic_key(("Handbook", "Dining", "Breakfast")) == topic_key(
            ("Ginza Guide", "Food", "Breakfast")
        )

    def test_plural_and_singular_align(self) -> None:
        assert topic_tokens("Pet Policy") == topic_tokens("Pets Policies")
