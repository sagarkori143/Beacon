"""Tesseract OCR provider.

Selected with ``OCR__PROVIDER=tesseract``. The engine binary and its language
packs are installed in the worker image only -- the API container never runs
OCR, because parsing untrusted uploads does not belong in the process serving
requests.

Recognition is CPU-bound and blocking, so every call is pushed to a thread with
a bounded pool: without that, one large scanned PDF stalls the worker's event
loop and its heartbeats along with it.
"""

from __future__ import annotations

import asyncio
import io
import shutil
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor

from app.core.config import OCRSettings
from app.core.errors import IngestionError
from app.core.logging import get_logger
from app.providers.base import ProviderHealth
from app.providers.ocr.base import OCRPage, OCRProvider, OCRResult, register_ocr_provider

log = get_logger(__name__)


@register_ocr_provider("tesseract")
class TesseractOCRProvider(OCRProvider):
    name = "tesseract"

    def __init__(self, settings: OCRSettings) -> None:
        self.settings = settings
        self.languages = tuple(settings.languages or ["eng"])
        self.supported_languages = frozenset(self.languages)
        # Small pool: OCR is already multi-threaded internally, and the worker
        # concurrently holds page images in memory.
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tesseract")

    async def extract_text(
        self,
        images: Sequence[tuple[int, bytes]],
        *,
        languages: Sequence[str] | None = None,
    ) -> OCRResult:
        if not images:
            return OCRResult(pages=(), engine=self.name, languages=self.languages)

        lang = "+".join(languages or self.languages)
        started = time.perf_counter()
        loop = asyncio.get_running_loop()

        try:
            pages = await asyncio.wait_for(
                asyncio.gather(
                    *(
                        loop.run_in_executor(self._executor, self._recognize, page_no, data, lang)
                        for page_no, data in images
                    )
                ),
                timeout=self.settings.timeout_s,
            )
        except TimeoutError as exc:
            raise IngestionError(
                f"OCR timed out after {self.settings.timeout_s}s on {len(images)} page(s)",
                stage="OCR",
                retryable=False,
            ) from exc

        return OCRResult(
            pages=tuple(sorted(pages, key=lambda p: p.page_number)),
            engine=self.name,
            languages=tuple(lang.split("+")),
            duration_ms=(time.perf_counter() - started) * 1000.0,
            meta={"dpi": self.settings.dpi},
        )

    def _recognize(self, page_number: int, data: bytes, lang: str) -> OCRPage:
        """Blocking recognition of one page. Runs in the thread pool."""
        import pytesseract
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            if image.mode not in ("L", "RGB"):
                image = image.convert("RGB")
            # image_to_data gives per-word confidences, which image_to_string
            # does not -- and a confidence score is what lets a bad scan be
            # flagged instead of silently indexed as garbage.
            payload = pytesseract.image_to_data(
                image,
                lang=lang,
                output_type=pytesseract.Output.DICT,
                config="--oem 1 --psm 3",
            )

        words: list[str] = []
        confidences: list[float] = []
        last_line = (0, 0, 0)

        for i, raw_text in enumerate(payload.get("text", [])):
            text = (raw_text or "").strip()
            if not text:
                continue
            line = (
                payload["block_num"][i],
                payload["par_num"][i],
                payload["line_num"][i],
            )
            if words and line != last_line:
                words.append("\n")
            last_line = line
            words.append(text)
            try:
                conf = float(payload["conf"][i])
            except (TypeError, ValueError):
                conf = -1.0
            if conf >= 0:
                confidences.append(conf / 100.0)

        text = " ".join(words).replace(" \n ", "\n").replace(" \n", "\n").strip()
        return OCRPage(
            page_number=page_number,
            text=text,
            confidence=sum(confidences) / len(confidences) if confidences else 0.0,
            language=lang,
        )

    async def health(self) -> ProviderHealth:
        binary = shutil.which("tesseract")
        if binary is None:
            return ProviderHealth(
                self.name,
                ok=False,
                detail="tesseract binary not found on PATH (install tesseract-ocr)",
            )
        try:
            import pytesseract

            version = str(await asyncio.to_thread(pytesseract.get_tesseract_version))
            installed = await asyncio.to_thread(pytesseract.get_languages, config="")
        except Exception as exc:  # noqa: BLE001 - health must never raise
            return ProviderHealth(self.name, ok=False, detail=str(exc)[:200])

        missing = [lang for lang in self.languages if lang not in installed]
        return ProviderHealth(
            name=self.name,
            ok=not missing,
            detail=f"missing language packs: {', '.join(missing)}" if missing else None,
            extra={"version": version, "languages": sorted(installed)[:20]},
        )

    async def aclose(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
