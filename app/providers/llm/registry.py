"""LLM provider type registry.

The set of usable LLM vendors is **data, not code**: the manifest in
``config/providers.yaml`` names a ``type`` and this registry maps that string to
a class. Adding a vendor is one new module plus one manifest entry.

Kept in its own module so provider implementations can import the decorator
without importing each other.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar

from app.core.config import LLMProviderConfig
from app.core.errors import ConfigurationError
from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.providers.llm.base import ModelProvider

log = get_logger(__name__)

TProvider = TypeVar("TProvider", bound="type[ModelProvider]")

_LLM_TYPES: dict[str, type[ModelProvider]] = {}


def register_llm_provider(type_name: str) -> Callable[[TProvider], TProvider]:
    """Class decorator registering a provider implementation under ``type_name``."""

    def decorator(cls: TProvider) -> TProvider:
        if type_name in _LLM_TYPES and _LLM_TYPES[type_name] is not cls:
            raise ConfigurationError(f"Duplicate LLM provider type '{type_name}'")
        _LLM_TYPES[type_name] = cls
        return cls

    return decorator


def available_llm_types() -> list[str]:
    _load_builtin_providers()
    return sorted(_LLM_TYPES)


def build_llm_provider(config: LLMProviderConfig) -> ModelProvider:
    """Instantiate one provider from its manifest entry."""
    _load_builtin_providers()
    cls = _LLM_TYPES.get(config.type)
    if cls is None:
        raise ConfigurationError(
            f"Unknown LLM provider type '{config.type}' for provider '{config.name}'. "
            f"Known types: {', '.join(sorted(_LLM_TYPES))}"
        )
    return cls(config)  # type: ignore[call-arg]


_loaded = False


def _load_builtin_providers() -> None:
    """Import the shipped provider modules so their decorators run.

    Import is lazy and side-effecting by design: a provider module is only
    imported when the registry is first consulted, which keeps optional vendor
    SDKs out of the import path of tests that do not need them.
    """
    global _loaded
    if _loaded:
        return
    _loaded = True

    from app.providers.llm import (  # noqa: F401
        anthropic,
        fake,
        gemini,
        ollama,
        openai,
        openai_compatible,
    )
