"""Build a section tree from layout lines."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.ingestion.chunking.headings import assign_level, is_heading
from app.services.ingestion.parsing.layout import LayoutLine, LayoutStats


@dataclass(slots=True)
class Block:
    """A paragraph, list or table fragment inside a section."""

    text: str
    page_from: int
    page_to: int
    kind: str = "text"

    @property
    def char_count(self) -> int:
        return len(self.text)


@dataclass(slots=True)
class SectionNode:
    level: int
    heading: str | None
    breadcrumb: tuple[str, ...]
    blocks: list[Block] = field(default_factory=list)
    children: list[SectionNode] = field(default_factory=list)
    page_from: int = 1
    page_to: int = 1

    @property
    def text(self) -> str:
        return "\n\n".join(block.text for block in self.blocks if block.text)

    @property
    def is_empty(self) -> bool:
        return not self.blocks and not self.children

    def leaves(self) -> list[SectionNode]:
        """Sections that carry content, in document order.

        A section with both its own text and sub-sections yields itself first --
        an introductory paragraph under a heading is content in its own right and
        must not be swallowed by the first sub-section.
        """
        out: list[SectionNode] = []
        if self.blocks:
            out.append(self)
        for child in self.children:
            out.extend(child.leaves())
        return out


def build_section_tree(lines: list[LayoutLine], stats: LayoutStats) -> SectionNode:
    """Group lines under the headings that precede them.

    Running headers and footers are dropped entirely: they are page furniture,
    not content, and leaving them in puts "Sagar Hotels Employee Handbook" at the
    start of every chunk.
    """
    root = SectionNode(level=0, heading=None, breadcrumb=())
    stack: list[SectionNode] = [root]
    paragraph: list[str] = []
    para_pages: list[int] = []

    def flush_paragraph() -> None:
        if not paragraph:
            return
        text = " ".join(paragraph).strip()
        if text:
            stack[-1].blocks.append(
                Block(text=text, page_from=min(para_pages), page_to=max(para_pages))
            )
        paragraph.clear()
        para_pages.clear()

    for line in lines:
        normalized = " ".join(line.text.split()).casefold()
        if normalized in stats.repeating_lines:
            continue

        if is_heading(line, stats):
            flush_paragraph()
            level = assign_level(line, stats)
            # Unwind to the parent this heading belongs under.
            while len(stack) > 1 and stack[-1].level >= level:
                stack.pop()
            parent = stack[-1]
            node = SectionNode(
                level=level,
                heading=line.text.strip(),
                breadcrumb=(*parent.breadcrumb, line.text.strip()),
                page_from=line.page_number,
                page_to=line.page_number,
            )
            parent.children.append(node)
            stack.append(node)
            continue

        paragraph.append(line.text.strip())
        para_pages.append(line.page_number)

        # A large vertical gap ends a paragraph; so does a line that terminates
        # a sentence and is noticeably short of the column width, which is how
        # the last line of a paragraph looks.
        ends_sentence = line.text.rstrip().endswith((".", "!", "?", "。", "！", "？"))
        short_line = line.width < stats.page_width * 0.6
        if line.space_above > stats.median_line_gap * 1.8 or (ends_sentence and short_line):
            flush_paragraph()

    flush_paragraph()
    _propagate_pages(root)
    return root


def _propagate_pages(node: SectionNode) -> tuple[int, int]:
    """Set each section's page span from its content and descendants."""
    pages: list[int] = []
    for block in node.blocks:
        pages.extend((block.page_from, block.page_to))
    for child in node.children:
        first, last = _propagate_pages(child)
        pages.extend((first, last))

    if pages:
        node.page_from, node.page_to = min(pages), max(pages)
    return node.page_from, node.page_to
