"""Decides whether a PDF needs OCR, and for which pages.

OCR is by far the most expensive stage in the pipeline, so it is never run
speculatively. Text is extracted natively first, then assessed.

**Character count alone is not a sufficient test**, and that is the whole reason
this module is more than three lines. The classic false positive is a PDF with a
broken ``ToUnicode`` CMap: it extracts thousands of characters that are pure
garbage -- ``(cid:40)(cid:72)...`` or mojibake -- and a naive
``len(text) > 100`` check happily declares it native and indexes noise. The
classic false negative is a scanned document carrying an identical text-layer
watermark on every page, which looks like a text layer until you notice every
page says the same thing.

The outcome is one of three decisions, and the third is why this is worth doing
properly: a native contract with three scanned signature pages costs three OCR
pages, not a hundred and eighty.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from app.core.config import OCRSettings
from app.core.enums import TextExtractionMode
from app.core.logging import get_logger

log = get_logger(__name__)

#: Unresolved glyph references left by a broken font encoding.
_CID_RE = re.compile(r"\(cid:\d+\)")
#: Replacement char, plus control characters that should never survive extraction.
_BROKEN_RE = re.compile(r"[�\x00-\x08\x0b\x0c\x0e-\x1f]")
_WORD_RE = re.compile(r"\S+")


@dataclass(frozen=True, slots=True)
class PageTextStats:
    """What native extraction produced for one page."""

    page_number: int
    char_count: int
    word_count: int
    mean_word_len: float
    alpha_ratio: float
    cid_artifact_ratio: float
    replacement_ratio: float
    image_area_ratio: float
    has_fonts: bool
    is_repeating: bool = False

    @property
    def looks_scanned(self) -> bool:
        """Mostly-image page with essentially no text on it."""
        return self.image_area_ratio >= 0.85 and self.char_count < 50


@dataclass(frozen=True, slots=True)
class TextLayerAssessment:
    mode: TextExtractionMode
    total_pages: int
    usable_pages: frozenset[int]
    ocr_pages: tuple[int, ...]
    coverage: float
    reasons: tuple[str, ...]
    per_page: tuple[PageTextStats, ...] = ()

    @property
    def needs_ocr(self) -> bool:
        return bool(self.ocr_pages)

    def to_event_detail(self) -> dict[str, Any]:
        """Compact summary for the ingestion job event.

        Recorded on every document so the decision is always inspectable later,
        not only when something went wrong.
        """
        return {
            "mode": self.mode.value,
            "total_pages": self.total_pages,
            "usable_pages": len(self.usable_pages),
            "coverage": round(self.coverage, 3),
            "ocr_pages": list(self.ocr_pages[:50]),
            "ocr_page_count": len(self.ocr_pages),
            "reasons": list(self.reasons),
        }


def page_stats(
    page_number: int,
    text: str,
    *,
    image_area_ratio: float = 0.0,
    has_fonts: bool = True,
) -> PageTextStats:
    """Measure one page's extracted text."""
    stripped = text or ""
    words = _WORD_RE.findall(stripped)
    non_space = [c for c in stripped if not c.isspace()]

    alpha = sum(1 for c in non_space if c.isalpha() or ord(c) > 0x2E80)
    cid_hits = len(_CID_RE.findall(stripped))
    broken = len(_BROKEN_RE.findall(stripped))

    return PageTextStats(
        page_number=page_number,
        char_count=len(stripped.strip()),
        word_count=len(words),
        mean_word_len=(sum(len(w) for w in words) / len(words)) if words else 0.0,
        alpha_ratio=(alpha / len(non_space)) if non_space else 0.0,
        cid_artifact_ratio=(cid_hits / len(words)) if words else 0.0,
        replacement_ratio=(broken / len(non_space)) if non_space else 0.0,
        image_area_ratio=image_area_ratio,
        has_fonts=has_fonts,
    )


#: A repeated block longer than this is repeated *content*, not a watermark.
#: Without the ceiling, a short document whose pages legitimately carry the same
#: prose would be sent for OCR despite having a perfectly good text layer.
_MAX_WATERMARK_CHARS = 200


def _mark_repeating(stats: list[PageTextStats], texts: list[str]) -> list[PageTextStats]:
    """Flag pages whose entire text is a short block shared by most other pages.

    A scanned deck with a "CONFIDENTIAL" watermark stamped on every page has a
    text layer on every page. Without this check it passes as native and the
    index fills with the word CONFIDENTIAL.
    """
    if len(texts) < 3:
        return stats

    normalized = [" ".join(t.split()).casefold()[:400] for t in texts]
    counts = Counter(t for t in normalized if t)
    threshold = max(2, int(len(texts) * 0.6))

    out: list[PageTextStats] = []
    for stat, text in zip(stats, normalized, strict=True):
        repeating = bool(text) and len(text) <= _MAX_WATERMARK_CHARS and counts[text] >= threshold
        out.append(
            PageTextStats(
                page_number=stat.page_number,
                char_count=stat.char_count,
                word_count=stat.word_count,
                mean_word_len=stat.mean_word_len,
                alpha_ratio=stat.alpha_ratio,
                cid_artifact_ratio=stat.cid_artifact_ratio,
                replacement_ratio=stat.replacement_ratio,
                image_area_ratio=stat.image_area_ratio,
                has_fonts=stat.has_fonts,
                is_repeating=repeating,
            )
        )
    return out


def is_page_usable(stat: PageTextStats, settings: OCRSettings) -> bool:
    """Whether this page's native text can be indexed as-is."""
    return (
        stat.char_count >= settings.min_chars_per_page
        and stat.alpha_ratio >= settings.min_alpha_ratio
        and stat.cid_artifact_ratio <= settings.max_cid_artifact_ratio
        and stat.replacement_ratio <= settings.max_replacement_ratio
        and settings.min_mean_word_len <= stat.mean_word_len <= settings.max_mean_word_len
        and not stat.is_repeating
    )


def assess_text_layer(
    page_texts: list[str],
    settings: OCRSettings,
    *,
    image_area_ratios: list[float] | None = None,
    font_flags: list[bool] | None = None,
) -> TextLayerAssessment:
    """Decide NATIVE, OCR or HYBRID for a document.

    Evaluation order matters: the two forced-OCR rules run before the coverage
    rules, because a document that extracts plenty of *garbage* would otherwise
    clear the coverage bar.
    """
    total = len(page_texts)
    if total == 0:
        return TextLayerAssessment(
            mode=TextExtractionMode.NATIVE,
            total_pages=0,
            usable_pages=frozenset(),
            ocr_pages=(),
            coverage=0.0,
            reasons=("empty_document",),
        )

    areas = image_area_ratios or [0.0] * total
    fonts = font_flags or [True] * total
    stats = _mark_repeating(
        [
            page_stats(i + 1, text, image_area_ratio=areas[i], has_fonts=fonts[i])
            for i, text in enumerate(page_texts)
        ],
        page_texts,
    )

    usable = frozenset(s.page_number for s in stats if is_page_usable(s, settings))
    coverage = len(usable) / total
    reasons: list[str] = []
    all_pages = tuple(range(1, total + 1))

    # 1. Mostly-image pages: a scan, whatever the character count says.
    scanned = sum(1 for s in stats if s.looks_scanned)
    if scanned >= total * 0.5:
        reasons.append(f"scanned_images ({scanned}/{total} pages)")
        return _decide(
            TextExtractionMode.OCR, total, usable, all_pages, coverage, reasons, stats, settings
        )

    # 2. Broken font encoding: lots of text, none of it meaningful.
    mean_cid = sum(s.cid_artifact_ratio for s in stats) / total
    mean_broken = sum(s.replacement_ratio for s in stats) / total
    if mean_cid > 0.05 or mean_broken > 0.05:
        reasons.append(f"broken_text_encoding (cid={mean_cid:.2f}, replacement={mean_broken:.2f})")
        return _decide(
            TextExtractionMode.OCR, total, usable, all_pages, coverage, reasons, stats, settings
        )

    # 3. Enough good pages, with enough text on them.
    mean_chars = (
        sum(s.char_count for s in stats if s.page_number in usable) / len(usable) if usable else 0.0
    )
    if coverage >= settings.native_coverage and mean_chars >= 200:
        reasons.append(f"native_text (coverage={coverage:.2f})")
        return _decide(
            TextExtractionMode.NATIVE, total, usable, (), coverage, reasons, stats, settings
        )

    # 4. Almost nothing usable.
    if coverage <= settings.ocr_coverage:
        reasons.append(f"insufficient_text (coverage={coverage:.2f})")
        return _decide(
            TextExtractionMode.OCR, total, usable, all_pages, coverage, reasons, stats, settings
        )

    # 5. Mixed: OCR only the pages that need it.
    missing = tuple(p for p in all_pages if p not in usable)
    reasons.append(f"mixed_document (coverage={coverage:.2f}, ocr_pages={len(missing)})")
    return _decide(
        TextExtractionMode.HYBRID, total, usable, missing, coverage, reasons, stats, settings
    )


def _decide(
    mode: TextExtractionMode,
    total: int,
    usable: frozenset[int],
    ocr_pages: tuple[int, ...],
    coverage: float,
    reasons: list[str],
    stats: list[PageTextStats],
    settings: OCRSettings,
) -> TextLayerAssessment:
    """Apply the page cap and build the assessment.

    A 4000-page scan would otherwise consume a worker for an hour; capping it is
    a deliberate refusal rather than a silent truncation, and the reason says so.
    """
    if len(ocr_pages) > settings.max_ocr_pages:
        reasons.append(
            f"ocr_page_cap ({len(ocr_pages)} > {settings.max_ocr_pages}); "
            f"OCR limited to the first {settings.max_ocr_pages} pages"
        )
        ocr_pages = ocr_pages[: settings.max_ocr_pages]

    assessment = TextLayerAssessment(
        mode=mode,
        total_pages=total,
        usable_pages=usable,
        ocr_pages=ocr_pages,
        coverage=coverage,
        reasons=tuple(reasons),
        per_page=tuple(stats),
    )
    log.info(
        "text_layer_assessed",
        mode=mode.value,
        pages=total,
        coverage=round(coverage, 3),
        ocr_pages=len(ocr_pages),
        reasons=list(reasons),
    )
    return assessment
