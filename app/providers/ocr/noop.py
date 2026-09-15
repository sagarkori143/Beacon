"""OCR provider that recognizes nothing.

Used when OCR is deliberately disabled (``OCR__PROVIDER=noop``) and in tests
that exercise the ingestion pipeline without needing a Tesseract install.

It returns empty pages rather than raising, so a document whose text layer is
insufficient still completes ingestion -- with zero chunks from those pages and
a job event recording that OCR was unavailable. Failing the whole document
instead would make a missing optional dependency look like data corruption.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.core.config import OCRSettings
from app.core.logging import get_logger
from app.providers.base import ProviderHealth
from app.providers.ocr.base import OCRPage, OCRProvider, OCRResult, register_ocr_provider

log = get_logger(__name__)


@register_ocr_provider("noop")
class NoopOCRProvider(OCRProvider):
    name = "noop"
    supported_languages = frozenset()

    def __init__(self, settings: OCRSettings | None = None) -> None:
        self.settings = settings

    async def extract_text(
        self,
        images: Sequence[tuple[int, bytes]],
        *,
        languages: Sequence[str] | None = None,
    ) -> OCRResult:
        if images:
            log.warning("ocr_skipped_noop_provider", pages=len(images))
        return OCRResult(
            pages=tuple(OCRPage(page_number=n, text="", confidence=0.0) for n, _ in images),
            engine=self.name,
            meta={"skipped": True},
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(self.name, ok=True, detail="OCR disabled")
