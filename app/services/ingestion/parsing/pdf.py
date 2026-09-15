"""PDF parsing with PyMuPDF.

Uploaded documents are untrusted input, so this only ever runs in the worker --
never in the process serving requests -- and every entry point is bounded: page
count, render resolution, and time.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.errors import DocumentParseError
from app.core.logging import get_logger
from app.services.ingestion.parsing.layout import LayoutLine, LayoutStats, analyze_layout

log = get_logger(__name__)

#: Refuse documents larger than this rather than pinning a worker for an hour.
MAX_PAGES = 5000
#: PyMuPDF span flag bits.
_FLAG_ITALIC = 1 << 1
_FLAG_BOLD = 1 << 4


@dataclass(slots=True)
class ParsedPDF:
    page_texts: list[str]
    lines: list[LayoutLine]
    stats: LayoutStats
    image_area_ratios: list[float]
    font_flags: list[bool]
    page_count: int
    metadata: dict[str, str] = field(default_factory=dict)

    def text_for_pages(self, pages: set[int] | None = None) -> str:
        selected = (
            self.page_texts
            if pages is None
            else [t for i, t in enumerate(self.page_texts, start=1) if i in pages]
        )
        return "\n\n".join(selected)


def parse_pdf(data: bytes, *, max_pages: int = MAX_PAGES) -> ParsedPDF:
    """Extract text and layout from a PDF.

    Returns everything the OCR gate and the chunker need in one pass, so the
    document is opened exactly once.
    """
    import fitz  # PyMuPDF

    try:
        document = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - any malformed input lands here
        raise DocumentParseError(f"Could not open PDF: {exc}") from exc

    try:
        if document.needs_pass:
            raise DocumentParseError("PDF is password protected")
        if document.page_count > max_pages:
            raise DocumentParseError(
                f"PDF has {document.page_count} pages; the limit is {max_pages}"
            )

        page_texts: list[str] = []
        lines: list[LayoutLine] = []
        image_ratios: list[float] = []
        font_flags: list[bool] = []
        page_width = 612.0

        for index in range(document.page_count):
            page = document[index]
            page_width = max(page_width, float(page.rect.width))
            page_lines, text = _extract_page(page, index + 1)
            lines.extend(page_lines)
            page_texts.append(text)
            image_ratios.append(_image_area_ratio(page))
            font_flags.append(bool(page.get_fonts()))

        stats = analyze_layout(lines, document.page_count, page_width)
        metadata = {
            key: str(value)
            for key, value in (document.metadata or {}).items()
            if value and key in ("title", "author", "subject", "creator", "producer")
        }

        return ParsedPDF(
            page_texts=page_texts,
            lines=lines,
            stats=stats,
            image_area_ratios=image_ratios,
            font_flags=font_flags,
            page_count=document.page_count,
            metadata=metadata,
        )
    finally:
        document.close()


def _extract_page(page: object, page_number: int) -> tuple[list[LayoutLine], str]:
    """Pull layout lines and plain text out of one page."""
    payload = page.get_text("dict")  # type: ignore[attr-defined]
    lines: list[LayoutLine] = []
    text_parts: list[str] = []
    previous_bottom: float | None = None

    for block in payload.get("blocks", []):
        if block.get("type") != 0:  # 0 = text, 1 = image
            continue
        for raw_line in block.get("lines", []):
            spans = raw_line.get("spans", [])
            text = "".join(span.get("text", "") for span in spans).strip()
            if not text:
                continue

            sizes = [float(span.get("size", 0.0)) for span in spans]
            chars = [len(span.get("text", "")) for span in spans]
            flags = [int(span.get("flags", 0)) for span in spans]
            total_chars = sum(chars) or 1

            bbox = raw_line.get("bbox", (0.0, 0.0, 0.0, 0.0))
            top, bottom = float(bbox[1]), float(bbox[3])
            gap = 0.0 if previous_bottom is None else max(0.0, top - previous_bottom)
            previous_bottom = bottom

            lines.append(
                LayoutLine(
                    page_number=page_number,
                    text=text,
                    x0=float(bbox[0]),
                    x1=float(bbox[2]),
                    top=top,
                    bottom=bottom,
                    max_font_size=max(sizes) if sizes else 0.0,
                    # Character-weighted so a one-word superscript does not
                    # define the line's size.
                    mode_font_size=(
                        sum(s * c for s, c in zip(sizes, chars, strict=True)) / total_chars
                        if sizes
                        else 0.0
                    ),
                    bold_ratio=sum(c for f, c in zip(flags, chars, strict=True) if f & _FLAG_BOLD)
                    / total_chars,
                    italic_ratio=sum(
                        c for f, c in zip(flags, chars, strict=True) if f & _FLAG_ITALIC
                    )
                    / total_chars,
                    space_above=gap,
                )
            )
            text_parts.append(text)

    return lines, "\n".join(text_parts)


def _image_area_ratio(page: object) -> float:
    """Fraction of the page covered by images.

    A high ratio with almost no text is the signature of a scan, and it is more
    reliable than character count alone for that judgement.
    """
    try:
        rect = page.rect  # type: ignore[attr-defined]
        page_area = float(rect.width * rect.height) or 1.0
        covered = 0.0
        for info in page.get_image_info():  # type: ignore[attr-defined]
            bbox = info.get("bbox")
            if not bbox:
                continue
            covered += abs((bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))
        return min(1.0, covered / page_area)
    except Exception:  # noqa: BLE001 - a missing image list is not fatal
        return 0.0


def render_pages(
    data: bytes, page_numbers: list[int], *, dpi: int = 300
) -> list[tuple[int, bytes]]:
    """Rasterize specific pages to PNG for OCR.

    Only the pages the gate asked for are rendered -- that selectivity is the
    entire point of the HYBRID decision.
    """
    import fitz

    document = fitz.open(stream=data, filetype="pdf")
    try:
        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        images: list[tuple[int, bytes]] = []
        for number in page_numbers:
            if not 1 <= number <= document.page_count:
                continue
            pixmap = document[number - 1].get_pixmap(matrix=matrix, alpha=False)
            images.append((number, pixmap.tobytes("png")))
        return images
    finally:
        document.close()


def parse_plain_text(data: bytes, *, encoding: str = "utf-8") -> ParsedPDF:
    """Adapt a plain-text or markdown upload to the same shape as a PDF.

    Keeping one downstream representation means the cleaning, chunking and
    embedding stages have no idea which source type they are handling.
    """
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")

    lines: list[LayoutLine] = []
    for offset, raw in enumerate(text.splitlines()):
        stripped = raw.strip()
        if not stripped:
            continue
        # Markdown headings carry their level explicitly; give them a font size
        # the heading scorer will recognize.
        hashes = len(stripped) - len(stripped.lstrip("#"))
        size = 10.0 + (4.0 - min(hashes, 4)) * 2.0 if hashes else 10.0
        lines.append(
            LayoutLine(
                page_number=1,
                text=stripped.lstrip("#").strip() if hashes else stripped,
                x0=0.0,
                x1=float(len(stripped)),
                top=float(offset),
                bottom=float(offset + 1),
                max_font_size=size,
                mode_font_size=size,
                bold_ratio=1.0 if hashes else 0.0,
                italic_ratio=0.0,
                space_above=1.0,
            )
        )

    return ParsedPDF(
        page_texts=[text],
        lines=lines,
        stats=analyze_layout(lines, 1, 612.0),
        image_area_ratios=[0.0],
        font_flags=[True],
        page_count=1,
    )
