"""OCR provider abstraction.

The ingestion pipeline depends on this interface and never on Tesseract or any
other engine. Swapping in Google Document AI or AWS Textract later means adding
one file and changing ``OCR__PROVIDER`` -- no pipeline or business-logic change.

OCR is expensive, so the pipeline decides *whether* to call this at all before
it calls it; see :mod:`app.services.ingestion.ocr_gate`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TypeVar

from app.core.config import OCRSettings
from app.core.errors import ConfigurationError
from app.providers.base import ProviderHealth


@dataclass(frozen=True, slots=True)
class OCRPage:
    page_number: int
    text: str
    #: Mean per-word confidence, 0.0-1.0. Low confidence is recorded on the
    #: version so a bad scan is visible rather than silently indexed.
    confidence: float = 0.0
    language: str | None = None


@dataclass(frozen=True, slots=True)
class OCRResult:
    pages: tuple[OCRPage, ...]
    engine: str
    languages: tuple[str, ...] = ()
    duration_ms: float = 0.0
    meta: dict[str, object] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n\n".join(p.text for p in self.pages if p.text)

    @property
    def mean_confidence(self) -> float:
        scored = [p.confidence for p in self.pages if p.confidence > 0]
        return sum(scored) / len(scored) if scored else 0.0

    @property
    def page_count(self) -> int:
        return len(self.pages)


class OCRProvider(ABC):
    """Recognizes text in page images."""

    name: str
    supported_languages: frozenset[str]

    @abstractmethod
    async def extract_text(
        self,
        images: Sequence[tuple[int, bytes]],
        *,
        languages: Sequence[str] | None = None,
    ) -> OCRResult:
        """Recognize text in rendered page images.

        ``images`` is a sequence of ``(page_number, png_bytes)`` so that a
        partial run -- only the pages that actually need OCR -- keeps its real
        page numbers for citations.
        """

    @abstractmethod
    async def health(self) -> ProviderHealth: ...

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

TOCR = TypeVar("TOCR", bound="type[OCRProvider]")

_OCR_TYPES: dict[str, type[OCRProvider]] = {}


def register_ocr_provider(type_name: str) -> Callable[[TOCR], TOCR]:
    def decorator(cls: TOCR) -> TOCR:
        _OCR_TYPES[type_name] = cls
        return cls

    return decorator


_loaded = False


def _load_builtin() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    from app.providers.ocr import noop, tesseract  # noqa: F401


def available_ocr_types() -> list[str]:
    _load_builtin()
    return sorted(_OCR_TYPES)


def build_ocr_provider(settings: OCRSettings) -> OCRProvider:
    _load_builtin()
    cls = _OCR_TYPES.get(settings.provider)
    if cls is None:
        raise ConfigurationError(
            f"Unknown OCR provider '{settings.provider}'. Known: {', '.join(sorted(_OCR_TYPES))}"
        )
    return cls(settings)  # type: ignore[call-arg]
