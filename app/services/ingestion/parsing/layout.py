"""Layout primitives extracted from a PDF.

Chunking that respects document structure needs more than a flat string. These
types carry the geometric and typographic signals -- font size relative to body
text, boldness, indentation, vertical gaps, repetition across pages -- that make
it possible to tell a heading from a sentence without a model.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from statistics import median


@dataclass(frozen=True, slots=True)
class LayoutLine:
    """One visual line of text with its typography."""

    page_number: int
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    max_font_size: float
    mode_font_size: float
    bold_ratio: float
    italic_ratio: float
    space_above: float = 0.0

    @property
    def char_count(self) -> int:
        return len(self.text)

    @property
    def width(self) -> float:
        return self.x1 - self.x0


@dataclass(slots=True)
class LayoutStats:
    """Document-level typography, used to judge each line relative to the norm.

    Absolute font sizes are meaningless across documents -- 12pt is body text in
    one and a heading in another. Everything here exists so decisions can be
    made relative to what *this* document considers normal.
    """

    body_font_size: float
    median_line_gap: float
    left_margin: float
    page_width: float
    heading_size_levels: tuple[float, ...] = ()
    repeating_lines: frozenset[str] = field(default_factory=frozenset)

    def size_level(self, size: float) -> int:
        """Rank a font size among the document's heading sizes, 1 = largest."""
        for index, level_size in enumerate(self.heading_size_levels, start=1):
            if size >= level_size - 0.4:
                return index
        return len(self.heading_size_levels) + 1


def _cluster_sizes(sizes: list[float], tolerance: float = 0.4) -> tuple[float, ...]:
    """Collapse near-identical font sizes into distinct levels, largest first.

    PDF font sizes are floats: the same visual heading can be emitted as 18.0 on
    one page and 17.9997 on another. Without clustering those become two heading
    levels and the section tree is nonsense.
    """
    clusters: list[list[float]] = []
    for size in sorted(sizes, reverse=True):
        if clusters and abs(clusters[-1][0] - size) <= tolerance:
            clusters[-1].append(size)
        else:
            clusters.append([size])
    return tuple(sum(c) / len(c) for c in clusters)


def _find_repeating(lines: list[LayoutLine], page_count: int) -> frozenset[str]:
    """Identify running headers and footers.

    This is the single highest-value heuristic in the chunker. A running header
    is large, bold, short and at the top of the page -- everything that says
    "heading" -- so without removing it, every page starts a new top-level
    section and the document's real structure disappears.
    """
    if page_count < 3:
        return frozenset()

    seen: dict[str, set[int]] = {}
    for line in lines:
        key = " ".join(line.text.split()).casefold()
        if not key or len(key) > 120:
            continue
        seen.setdefault(key, set()).add(line.page_number)

    threshold = max(3, int(page_count * 0.6))
    return frozenset(text for text, pages in seen.items() if len(pages) >= threshold)


def analyze_layout(lines: list[LayoutLine], page_count: int, page_width: float) -> LayoutStats:
    """Derive document-level norms from its lines."""
    if not lines:
        return LayoutStats(
            body_font_size=10.0, median_line_gap=4.0, left_margin=0.0, page_width=page_width
        )

    # Weight by characters, not by line count: a document with many short
    # headings must not have its "body size" pulled toward the heading size.
    weighted: Counter[float] = Counter()
    for line in lines:
        weighted[round(line.mode_font_size, 1)] += max(1, line.char_count)
    body_size = weighted.most_common(1)[0][0]

    gaps = [line.space_above for line in lines if line.space_above > 0]
    left_edges = [line.x0 for line in lines]

    heading_sizes = _cluster_sizes(
        [round(line.max_font_size, 1) for line in lines if line.max_font_size >= body_size * 1.15]
    )

    return LayoutStats(
        body_font_size=body_size,
        median_line_gap=median(gaps) if gaps else 4.0,
        left_margin=min(left_edges) if left_edges else 0.0,
        page_width=page_width,
        heading_size_levels=heading_sizes,
        repeating_lines=_find_repeating(lines, page_count),
    )
