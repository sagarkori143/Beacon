"""Section-aware chunking.

Splitting a document by character count destroys exactly the thing retrieval
depends on: a chunk that says "after 23:00, guests should use the side entrance"
is useless without the heading "Check-in > Late arrival" attached to it, and a
chunk that spans the end of the pet policy and the start of the smoking policy
answers neither question.

So chunks are built *within* sections:

* Never merge across a top-level section. Conflating "Pet Policy" with "Smoking
  Policy" is precisely what breaks the location-override logic downstream, which
  identifies overriding content by topic.
* Undersized sibling sections merge forward, because forty one-line subsections
  produce forty chunks that all rank badly.
* Oversized sections split at the most natural boundary available -- paragraph,
  then sentence, then line -- never mid-word.
* Overlap is whole trailing sentences rather than a raw token slice, so it reads
  as a lead-in instead of a fragment.
* Every chunk's text is prefixed with its heading breadcrumb, which is both
  embedded and stored. Short chunks gain the context they need, and the lexical
  index gets the heading weighted separately at index time.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from app.core.config import ChunkingSettings
from app.core.logging import get_logger
from app.services.ingestion.chunking.headings import section_key, topic_key
from app.services.ingestion.chunking.sections import Block, SectionNode
from app.services.rag.token_budget import count_tokens

log = get_logger(__name__)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。！？])\s+")
_WHITESPACE = re.compile(r"[ \t]+")


@dataclass(slots=True)
class DraftChunk:
    """A chunk before it has an embedding."""

    ordinal: int
    content: str
    token_count: int
    page_from: int
    page_to: int
    heading: str | None = None
    section_path: tuple[str, ...] = ()
    section_key: str | None = None
    topic_key: str | None = None
    kind: str = "text"

    @property
    def content_hash(self) -> str:
        """Hash of the normalized text, used for near-duplicate detection."""
        normalized = _WHITESPACE.sub(" ", self.content).strip().casefold()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def chunk_document(
    root: SectionNode, settings: ChunkingSettings, *, source_name: str = ""
) -> list[DraftChunk]:
    """Turn a section tree into chunks."""
    leaves = [leaf for leaf in root.leaves() if leaf.text.strip()]
    if not leaves:
        return []

    merged = _merge_undersized(leaves, settings)
    chunks: list[DraftChunk] = []

    for section in merged:
        breadcrumb = section.breadcrumb
        prefix = _breadcrumb_prefix(breadcrumb) if settings.prepend_breadcrumb else ""
        prefix_tokens = count_tokens(prefix)
        budget = max(settings.min_tokens, settings.target_tokens - prefix_tokens)
        hard_cap = max(budget, settings.max_tokens - prefix_tokens)

        for window, pages, kind in _windows(section.blocks, budget, hard_cap, settings):
            content = prefix + window
            chunks.append(
                DraftChunk(
                    ordinal=len(chunks),
                    content=content,
                    token_count=count_tokens(content),
                    page_from=pages[0],
                    page_to=pages[1],
                    heading=section.heading,
                    section_path=breadcrumb,
                    section_key=section_key(breadcrumb) if breadcrumb else None,
                    topic_key=topic_key(breadcrumb),
                    kind=kind,
                )
            )

    log.info(
        "document_chunked",
        source=source_name,
        sections=len(merged),
        chunks=len(chunks),
        mean_tokens=round(sum(c.token_count for c in chunks) / len(chunks), 1) if chunks else 0,
    )
    return chunks


# ---------------------------------------------------------------------------
# Section merging
# ---------------------------------------------------------------------------


def _merge_undersized(sections: list[SectionNode], settings: ChunkingSettings) -> list[SectionNode]:
    """Fold tiny sections into the next sibling at the same level.

    Never across a hard boundary: two adjacent top-level sections stay separate
    even when both are a single line, because they are different subjects and
    the override logic keys on subject.
    """
    out: list[SectionNode] = []

    for section in sections:
        tokens = count_tokens(section.text)
        if not out or tokens >= settings.min_tokens:
            out.append(_copy_section(section))
            continue

        previous = out[-1]
        crosses_boundary = (
            section.level <= settings.hard_boundary_level
            or previous.level <= settings.hard_boundary_level
        ) and section.breadcrumb[: settings.hard_boundary_level] != previous.breadcrumb[
            : settings.hard_boundary_level
        ]
        if crosses_boundary:
            out.append(_copy_section(section))
            continue

        # Carry the merged section's heading into the text so it is not lost.
        if section.heading:
            previous.blocks.append(
                Block(
                    text=f"{section.heading}",
                    page_from=section.page_from,
                    page_to=section.page_from,
                    kind="heading",
                )
            )
        previous.blocks.extend(section.blocks)
        previous.page_to = max(previous.page_to, section.page_to)

    return out


def _copy_section(section: SectionNode) -> SectionNode:
    return SectionNode(
        level=section.level,
        heading=section.heading,
        breadcrumb=section.breadcrumb,
        blocks=list(section.blocks),
        children=[],
        page_from=section.page_from,
        page_to=section.page_to,
    )


# ---------------------------------------------------------------------------
# Windowing
# ---------------------------------------------------------------------------


def _windows(
    blocks: list[Block],
    budget: int,
    hard_cap: int,
    settings: ChunkingSettings,
) -> list[tuple[str, tuple[int, int], str]]:
    """Pack blocks into token-bounded windows with sentence-level overlap."""
    windows: list[tuple[str, tuple[int, int], str]] = []
    current: list[str] = []
    current_tokens = 0
    page_from: int | None = None
    page_to = 1

    def emit() -> None:
        nonlocal current, current_tokens, page_from, page_to
        text = "\n\n".join(current).strip()
        if text:
            windows.append((text, (page_from or 1, page_to), "text"))
        current = []
        current_tokens = 0
        page_from = None

    for block in blocks:
        # A table is a unit: splitting it mid-row makes both halves unreadable.
        if block.kind == "table":
            emit()
            for part, _ in _split_text(block.text, hard_cap, settings):
                windows.append((part, (block.page_from, block.page_to), "table"))
            continue

        for piece, tokens in _split_text(block.text, hard_cap, settings):
            if current and current_tokens + tokens > budget:
                emit()
                overlap = _overlap_tail(windows[-1][0], settings.overlap_tokens) if windows else ""
                if overlap:
                    current.append(overlap)
                    current_tokens = count_tokens(overlap)

            current.append(piece)
            current_tokens += tokens
            page_from = block.page_from if page_from is None else min(page_from, block.page_from)
            page_to = max(page_to, block.page_to)

            if current_tokens >= hard_cap:
                emit()

    emit()
    return windows


def _split_text(text: str, hard_cap: int, settings: ChunkingSettings) -> list[tuple[str, int]]:
    """Break text that exceeds the cap at the best boundary available.

    Priority is paragraph, then sentence, then line, then whitespace. Falling all
    the way to whitespace is rare and means a single unbroken run of text longer
    than the cap.
    """
    tokens = count_tokens(text)
    if tokens <= hard_cap:
        return [(text, tokens)]

    for splitter in (lambda t: t.split("\n\n"), _SENTENCE_SPLIT.split, lambda t: t.split("\n")):
        parts = [p.strip() for p in splitter(text) if p.strip()]
        if len(parts) > 1:
            out: list[tuple[str, int]] = []
            for part in parts:
                out.extend(_split_text(part, hard_cap, settings))
            return out

    # One enormous unbroken run: cut on word boundaries.
    words = text.split(" ")
    out = []
    buffer: list[str] = []
    for word in words:
        buffer.append(word)
        if count_tokens(" ".join(buffer)) >= hard_cap:
            joined = " ".join(buffer)
            out.append((joined, count_tokens(joined)))
            buffer = []
    if buffer:
        joined = " ".join(buffer)
        out.append((joined, count_tokens(joined)))
    return out


def _overlap_tail(text: str, overlap_tokens: int) -> str:
    """Take whole trailing sentences up to the overlap budget.

    A raw token slice would start mid-clause, which reads to the model like
    corrupted input. Whole sentences read as a lead-in.
    """
    if overlap_tokens <= 0:
        return ""

    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    tail: list[str] = []
    total = 0
    for sentence in reversed(sentences):
        tokens = count_tokens(sentence)
        if total + tokens > overlap_tokens and tail:
            break
        tail.insert(0, sentence)
        total += tokens
        if total >= overlap_tokens:
            break
    return " ".join(tail)


def _breadcrumb_prefix(breadcrumb: tuple[str, ...]) -> str:
    if not breadcrumb:
        return ""
    return " > ".join(breadcrumb) + "\n\n"
