"""Heading detection.

There is no heading tag in a PDF -- only text that happens to be bigger, bolder,
shorter and further from its neighbours. This module turns those signals into a
score, and the score into a level.

Two of the rules carry most of the weight. The repeating-line penalty removes
running headers, which otherwise look exactly like headings and destroy the
section tree. And clustered font sizes stop 17.9997pt and 18.0pt becoming two
separate heading levels.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

from app.services.ingestion.parsing.layout import LayoutLine, LayoutStats

#: Numbered-heading patterns: "3.2.1", "IV.", "Article 5", CJK article markers.
NUMBERING_RE = re.compile(
    r"^\s*(?:"
    r"\d+(?:\.\d+){0,3}[.)]?"
    r"|[IVXLC]+[.)]"
    r"|[A-Z][.)]"
    r"|(?:Chapter|Section|Article|Appendix|Part)\s+[\dIVXLC]+"
    r"|第\s*\d+\s*[条章節項]"
    r")\s+\S",
    re.IGNORECASE,
)

_SENTENCE_ENDINGS = (".", "。", ",", ";", "、", ":", "،")
HEADING_THRESHOLD = 3.0

#: Words that carry no topical meaning when deriving an override key.
#:
#: Two groups. Ordinary function words, and -- more importantly -- the generic
#: section nouns that documents attach to a subject almost arbitrarily. Without
#: the second group, "Breakfast Service" and "Breakfast Hours" reduce to
#: different keys and a location override goes undetected, which is the exact
#: case this mechanism exists for.
#:
#: Directional prepositions (in/out/from/to) are deliberately NOT stopwords:
#: they are what distinguishes "Check-in Time" from "Check-out Time", and
#: collapsing those two would suppress genuinely different content.
_TOPIC_STOPWORDS = frozenset(
    {
        # function words
        "a",
        "an",
        "and",
        "the",
        "of",
        "for",
        "our",
        "your",
        "is",
        "are",
        "all",
        # generic section nouns
        "policy",
        "policies",
        "information",
        "general",
        "section",
        "chapter",
        "overview",
        "about",
        "note",
        "notes",
        "detail",
        "details",
        "service",
        "services",
        "hour",
        "hours",
        "time",
        "times",
        "schedule",
        "schedules",
        "rule",
        "rules",
        "guideline",
        "guidelines",
        "procedure",
        "procedures",
        "requirement",
        "requirements",
        "fee",
        "fees",
        "charge",
        "charges",
        "terms",
        "condition",
        "conditions",
        "faq",
        "faqs",
        "guest",
        "guests",
    }
)


def heading_score(line: LayoutLine, stats: LayoutStats) -> float:
    """How strongly this line looks like a heading. Higher is more heading-like."""
    score = 0.0

    if line.max_font_size >= stats.body_font_size * 1.15:
        score += 2.0
    if line.max_font_size >= stats.body_font_size * 1.35:
        score += 1.0
    if line.bold_ratio > 0.6:
        score += 1.0
    if NUMBERING_RE.match(line.text):
        score += 1.5
    if line.char_count <= 120 and not line.text.rstrip().endswith(_SENTENCE_ENDINGS):
        score += 1.0
    if line.text.isupper() and line.char_count <= 80:
        score += 0.5
    if line.space_above > stats.median_line_gap * 1.6:
        score += 0.75
    if abs(line.x0 - stats.left_margin) < 2.0:
        score += 0.25
    if _is_centered(line, stats):
        score += 0.25

    # A running header is big, bold and short -- everything above rewards. This
    # penalty has to be large enough to override all of it.
    if " ".join(line.text.split()).casefold() in stats.repeating_lines:
        score -= 4.0
    # Long lines are paragraphs, whatever their typography.
    if line.char_count > 200:
        score -= 3.0

    return score


def is_heading(line: LayoutLine, stats: LayoutStats) -> bool:
    return heading_score(line, stats) >= HEADING_THRESHOLD


def _is_centered(line: LayoutLine, stats: LayoutStats) -> bool:
    if stats.page_width <= 0:
        return False
    center = (line.x0 + line.x1) / 2
    return abs(center - stats.page_width / 2) < stats.page_width * 0.06


def assign_level(line: LayoutLine, stats: LayoutStats) -> int:
    """Heading depth, 1 = top level.

    Explicit numbering wins when present: "3.2.1" is unambiguously depth 3, and
    that is more reliable than inferring depth from a font size that a designer
    may have chosen inconsistently.
    """
    match = NUMBERING_RE.match(line.text)
    if match:
        numeric = re.match(r"^\s*(\d+(?:\.\d+){0,3})", line.text)
        if numeric:
            return min(4, numeric.group(1).count(".") + 1)
    return min(6, stats.size_level(line.max_font_size))


def normalize_heading(heading: str) -> str:
    """Strip numbering and punctuation to leave the heading's words."""
    text = unicodedata.normalize("NFKC", heading).strip()
    text = re.sub(r"^\s*(?:\d+(?:\.\d+)*|[IVXLC]+|[A-Z])[.)]\s*", "", text)
    text = re.sub(
        r"^\s*(?:Chapter|Section|Article|Appendix|Part)\s+[\dIVXLC]+[:.\s-]*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", text).strip(" :-–—.").strip()


def section_key(breadcrumb: tuple[str, ...]) -> str:
    """Stable identifier for "the same section of the same document"."""
    joined = " > ".join(normalize_heading(part).casefold() for part in breadcrumb)
    return hashlib.blake2b(joined.encode("utf-8"), digest_size=8).hexdigest()


def topic_tokens(heading: str) -> frozenset[str]:
    """Meaningful words of a heading, normalized for comparison."""
    words = re.findall(r"[a-z0-9]+", normalize_heading(heading).casefold())
    # Crude singularization: enough to align "policies"/"policy" and
    # "pets"/"pet" without dragging in a stemmer.
    return frozenset(
        w.rstrip("s") if len(w) > 3 and w.endswith("s") else w
        for w in words
        if w not in _TOPIC_STOPWORDS
    )


def topic_key(breadcrumb: tuple[str, ...]) -> str | None:
    """A normalized subject used to detect that one chunk overrides another.

    This is what lets a location's "Breakfast Hours" section suppress the
    organization's "Breakfast Service" section without asking a model: both
    reduce to ``breakfast``.

    Only the deepest heading contributes. Including ancestors would make
    "Ginza Supplement > Dining > Breakfast" and "Handbook > Dining > Breakfast"
    different topics, which is exactly the pairing this has to catch.

    The normalization is deliberately coarse but the *matching* is not: see
    ``services.retrieval.hierarchical``, which requires an exact key match or a
    strict subset relationship before it suppresses anything. Coarse keys plus
    strict matching means a missed override degrades to "both passages present,
    with the location one ranked first and labelled", which the answer prompt
    handles -- rather than to a wrongly discarded passage.
    """
    if not breadcrumb:
        return None
    tokens = topic_tokens(breadcrumb[-1])
    if not tokens:
        return None
    return "-".join(sorted(tokens))[:64]
