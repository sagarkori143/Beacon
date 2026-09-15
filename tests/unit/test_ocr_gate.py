"""The OCR decision.

OCR is the most expensive stage in the pipeline, so these tests are about not
running it unnecessarily -- and, just as importantly, about the two cases where
a character count alone gives the wrong answer.
"""

from __future__ import annotations

import pytest

from app.core.config import OCRSettings
from app.core.enums import TextExtractionMode
from app.services.ingestion.ocr_gate import assess_text_layer, is_page_usable, page_stats

pytestmark = pytest.mark.unit


def good_page(n: int = 1) -> str:
    """A page with a usable text layer.

    Page content varies, because real documents do. Byte-identical pages are
    the watermark signature the gate is specifically built to catch, so using
    them as "good" test data would be testing the wrong thing.
    """
    return (
        f"Section {n}. Breakfast is served in the main dining room from 7:00 AM "
        f"to 10:00 AM every day, including weekends and public holidays. Guests "
        f"on club floors may request in-room breakfast at no extra charge by "
        f"calling the front desk before 9:00 PM on day {n} of their stay."
    )


@pytest.fixture
def ocr_settings() -> OCRSettings:
    return OCRSettings()


class TestNativeText:
    def test_clean_document_skips_ocr_entirely(self, ocr_settings: OCRSettings) -> None:
        result = assess_text_layer([good_page(i) for i in range(5)], ocr_settings)
        assert result.mode is TextExtractionMode.NATIVE
        assert not result.needs_ocr
        assert result.coverage == 1.0

    def test_empty_document_is_not_an_error(self, ocr_settings: OCRSettings) -> None:
        result = assess_text_layer([], ocr_settings)
        assert result.mode is TextExtractionMode.NATIVE
        assert "empty_document" in result.reasons


class TestScannedDocuments:
    def test_blank_pages_trigger_full_ocr(self, ocr_settings: OCRSettings) -> None:
        result = assess_text_layer([""] * 6, ocr_settings)
        assert result.mode is TextExtractionMode.OCR
        assert len(result.ocr_pages) == 6

    def test_image_heavy_pages_trigger_ocr_despite_some_text(
        self, ocr_settings: OCRSettings
    ) -> None:
        """A page that is 95% image with a caption on it is a scan."""
        pages = ["Figure 1." for _ in range(6)]
        result = assess_text_layer(pages, ocr_settings, image_area_ratios=[0.95] * 6)
        assert result.mode is TextExtractionMode.OCR
        assert any("scanned_images" in r for r in result.reasons)


class TestFalsePositives:
    def test_broken_font_encoding_is_not_mistaken_for_text(self, ocr_settings: OCRSettings) -> None:
        """The classic failure: thousands of characters of pure garbage.

        A PDF with a broken ToUnicode CMap extracts plenty of content. A naive
        length check indexes it and retrieval quietly becomes useless.
        """
        garbage = " ".join(f"(cid:{i % 90 + 30})" for i in range(120))
        result = assess_text_layer([garbage] * 5, ocr_settings)

        assert result.mode is TextExtractionMode.OCR
        assert any("broken_text_encoding" in r for r in result.reasons)

    def test_mojibake_is_not_mistaken_for_text(self, ocr_settings: OCRSettings) -> None:
        broken = "�" * 400
        result = assess_text_layer([broken] * 4, ocr_settings)
        assert result.mode is TextExtractionMode.OCR

    def test_watermark_on_every_page_is_not_a_text_layer(self, ocr_settings: OCRSettings) -> None:
        """A scanned deck stamped 'CONFIDENTIAL' has text on every page.

        Without the repetition check it passes as native and the index fills
        with one repeated word.
        """
        watermark = "CONFIDENTIAL - SAGAR HOTELS INTERNAL USE ONLY. " * 4
        result = assess_text_layer([watermark] * 8, ocr_settings)

        assert result.mode is TextExtractionMode.OCR
        assert all(stat.is_repeating for stat in result.per_page)


class TestHybrid:
    def test_only_the_bad_pages_are_sent_to_ocr(self, ocr_settings: OCRSettings) -> None:
        """A native contract with three scanned signature pages.

        This is the case that makes the three-way decision worth having: OCR
        runs on 3 pages instead of 10.
        """
        pages = [good_page(i) for i in range(7)] + ["", "", ""]
        result = assess_text_layer(pages, ocr_settings)

        assert result.mode is TextExtractionMode.HYBRID
        assert result.ocr_pages == (8, 9, 10)
        assert result.usable_pages == frozenset(range(1, 8))

    def test_page_cap_truncates_rather_than_hanging(self) -> None:
        settings = OCRSettings(max_ocr_pages=5)
        result = assess_text_layer([""] * 40, settings)
        assert len(result.ocr_pages) == 5
        assert any("ocr_page_cap" in r for r in result.reasons)


class TestPageStats:
    def test_usability_requires_more_than_length(self, ocr_settings: OCRSettings) -> None:
        numeric = page_stats(1, "1 2 3 4 5 " * 40)
        assert numeric.char_count > ocr_settings.min_chars_per_page
        assert not is_page_usable(numeric, ocr_settings), "alpha ratio ignored"

    def test_event_detail_is_recorded_for_every_document(self, ocr_settings: OCRSettings) -> None:
        """The decision is always inspectable, not only when it went wrong."""
        detail = assess_text_layer([good_page(i) for i in range(3)], ocr_settings).to_event_detail()
        assert detail["mode"] == "NATIVE"
        assert detail["ocr_page_count"] == 0
        assert detail["reasons"]
