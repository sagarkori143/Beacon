"""Builds every provider from configuration, and holds them for the process.

This is where "the set of providers is data, not code" becomes concrete. Nothing
in the application imports a concrete provider; they ask the bundle on
``app.state.providers`` (or the one the worker constructs) for an interface.

A provider whose credentials are missing, or whose construction fails, is
**skipped with a warning rather than crashing startup**. A deployment with only
a local model server configured must boot exactly as happily as one with four
vendors, and a cloud key that expires must degrade routing rather than take the
API down.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.core.config import Settings
from app.core.errors import ConfigurationError
from app.core.logging import get_logger
from app.providers.embeddings.base import EmbeddingProvider, build_embedding_provider
from app.providers.llm.base import ModelInfo, ModelProvider
from app.providers.llm.registry import build_llm_provider
from app.providers.ocr.base import OCRProvider, build_ocr_provider
from app.providers.queue.base import QueueProvider, build_queue_provider
from app.providers.storage.base import StorageProvider, build_storage_provider
from app.providers.vector_store.base import VectorStore, build_vector_store

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from app.providers.search.base import SearchProvider

log = get_logger(__name__)


@dataclass(slots=True)
class ProviderBundle:
    """Every provider the process has, keyed by the name given in the manifest."""

    llm: dict[str, ModelProvider] = field(default_factory=dict)
    embeddings: EmbeddingProvider | None = None
    storage: StorageProvider | None = None
    queue: QueueProvider | None = None
    ocr: OCRProvider | None = None
    search: SearchProvider | None = None
    vector_store: VectorStore | None = None
    #: Providers that failed to construct, with the reason. Surfaced by /health
    #: so a missing key is visible rather than mysterious.
    skipped: dict[str, str] = field(default_factory=dict)

    # -- accessors -----------------------------------------------------------

    def get_llm(self, name: str) -> ModelProvider:
        provider = self.llm.get(name)
        if provider is None:
            raise ConfigurationError(
                f"No LLM provider named '{name}'. Configured: "
                f"{', '.join(sorted(self.llm)) or '(none)'}"
            )
        return provider

    def require_embeddings(self) -> EmbeddingProvider:
        if self.embeddings is None:
            raise ConfigurationError("No embedding provider is configured")
        return self.embeddings

    def require_storage(self) -> StorageProvider:
        if self.storage is None:
            raise ConfigurationError("No storage provider is configured")
        return self.storage

    def require_queue(self) -> QueueProvider:
        if self.queue is None:
            raise ConfigurationError("No queue provider is configured")
        return self.queue

    def require_ocr(self) -> OCRProvider:
        if self.ocr is None:
            raise ConfigurationError("No OCR provider is configured")
        return self.ocr

    def require_search(self) -> SearchProvider:
        if self.search is None:
            raise ConfigurationError("No search provider is configured")
        return self.search

    def require_vector_store(self) -> VectorStore:
        if self.vector_store is None:
            raise ConfigurationError("No vector store is configured")
        return self.vector_store

    @property
    def available_llm(self) -> dict[str, ModelProvider]:
        """Providers whose circuit is not open."""
        return {
            name: provider
            for name, provider in self.llm.items()
            if getattr(provider, "circuit", None) is None or provider.circuit.is_available  # type: ignore[attr-defined]
        }

    async def catalog(self) -> list[ModelInfo]:
        """Every model across every provider. The router's candidate pool."""
        results = await asyncio.gather(
            *(p.list_models() for p in self.llm.values()), return_exceptions=True
        )
        catalog: list[ModelInfo] = []
        for outcome in results:
            if isinstance(outcome, BaseException):
                continue
            catalog.extend(outcome)
        return catalog

    async def health(self) -> dict[str, Any]:
        """Aggregate health. Never raises: a probe failure is data, not an error."""
        checks: dict[str, Any] = {}
        named: list[tuple[str, Any]] = []

        for name, provider in self.llm.items():
            named.append((f"llm:{name}", provider))
        if self.embeddings:
            named.append(("embeddings", self.embeddings))
        if self.storage:
            named.append(("storage", self.storage))
        if self.queue:
            named.append(("queue", self.queue))
        if self.ocr:
            named.append(("ocr", self.ocr))

        results = await asyncio.gather(*(p.health() for _, p in named), return_exceptions=True)
        for (label, _), outcome in zip(named, results, strict=True):
            if isinstance(outcome, BaseException):
                checks[label] = {"ok": False, "error": str(outcome)[:200]}
            else:
                checks[label] = outcome.to_dict()

        if self.skipped:
            checks["skipped"] = self.skipped
        return checks

    async def aclose(self) -> None:
        closers = [
            *self.llm.values(),
            self.embeddings,
            self.storage,
            self.queue,
            self.ocr,
        ]
        await asyncio.gather(
            *(c.aclose() for c in closers if c is not None), return_exceptions=True
        )


def build_providers(
    settings: Settings,
    *,
    redis: Redis | None = None,
    include_queue: bool = True,
    include_search: bool = True,
) -> ProviderBundle:
    """Construct the bundle. Pure configuration in, providers out."""
    bundle = ProviderBundle()

    # --- LLM providers ---
    for config in settings.providers.enabled_llm:
        try:
            bundle.llm[config.name] = build_llm_provider(config)
        except Exception as exc:  # noqa: BLE001 - one bad provider must not stop boot
            bundle.skipped[f"llm:{config.name}"] = str(exc)[:300]
            log.warning("llm_provider_skipped", provider=config.name, reason=str(exc)[:200])

    if not bundle.llm:
        log.warning("no_llm_providers_configured", hint="chat endpoints will return 503")

    # --- embeddings ---
    embedding_configs = {c.name: c for c in settings.providers.enabled_embeddings}
    chosen = embedding_configs.get(settings.embedding_provider) or next(
        iter(embedding_configs.values()), None
    )
    if chosen is not None:
        try:
            bundle.embeddings = build_embedding_provider(chosen)
        except Exception as exc:  # noqa: BLE001
            bundle.skipped[f"embeddings:{chosen.name}"] = str(exc)[:300]
            log.error("embedding_provider_failed", provider=chosen.name, reason=str(exc)[:200])

    # --- storage, ocr ---
    try:
        bundle.storage = build_storage_provider(settings.storage)
    except Exception as exc:  # noqa: BLE001
        bundle.skipped["storage"] = str(exc)[:300]
        log.error("storage_provider_failed", reason=str(exc)[:200])

    try:
        bundle.ocr = build_ocr_provider(settings.ocr)
    except Exception as exc:  # noqa: BLE001
        bundle.skipped["ocr"] = str(exc)[:300]
        log.error("ocr_provider_failed", reason=str(exc)[:200])

    # --- queue (needs a Redis handle for the default implementation) ---
    if include_queue:
        try:
            kwargs: dict[str, Any] = {"redis": redis} if redis is not None else {}
            bundle.queue = build_queue_provider(settings.queue, **kwargs)
        except Exception as exc:  # noqa: BLE001
            bundle.skipped["queue"] = str(exc)[:300]
            log.error("queue_provider_failed", reason=str(exc)[:200])

    # --- search ---
    if include_search:
        from app.providers.search.base import build_search_provider

        try:
            bundle.search = build_search_provider(settings)
            bundle.vector_store = build_vector_store(settings)
        except Exception as exc:  # noqa: BLE001
            bundle.skipped["search"] = str(exc)[:300]
            log.error("search_provider_failed", reason=str(exc)[:200])

    log.info(
        "providers_built",
        llm=sorted(bundle.llm),
        embeddings=bundle.embeddings.name if bundle.embeddings else None,
        storage=bundle.storage.name if bundle.storage else None,
        ocr=bundle.ocr.name if bundle.ocr else None,
        queue=bundle.queue.name if bundle.queue else None,
        skipped=sorted(bundle.skipped),
    )
    return bundle
