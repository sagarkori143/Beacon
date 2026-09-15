"""OpenAI provider.

OpenAI speaks the Chat Completions shape this codebase already implements, so
this is a thin specialization of :class:`OpenAICompatibleProvider` that supplies
the canonical endpoint. It exists as its own ``type`` so the manifest can say
``type: openai`` without repeating a base URL, and so OpenAI-specific behaviour
has an obvious home if it diverges later.
"""

from __future__ import annotations

from app.core.config import LLMProviderConfig
from app.core.errors import ConfigurationError
from app.providers.llm.openai_compatible import OpenAICompatibleProvider
from app.providers.llm.registry import register_llm_provider


@register_llm_provider("openai")
class OpenAIProvider(OpenAICompatibleProvider):
    default_base_url = "https://api.openai.com/v1"

    def __init__(self, config: LLMProviderConfig) -> None:
        if not config.api_key:
            raise ConfigurationError(
                f"Provider '{config.name}' (openai) requires an API key; "
                "set OPENAI_API_KEY or remove the entry from the manifest"
            )
        super().__init__(config)

        # Azure OpenAI deployments keep the same body but authenticate with a
        # header and pin an api-version on the query string.
        if config.options.get("azure"):
            self._client.headers.pop("Authorization", None)
            self._client.headers["api-key"] = config.api_key
            version = config.options.get("api_version", "2024-10-21")
            self._client.params = {"api-version": version}
