"""Application configuration.

Every knob the system has is declared here, and nothing else reads ``os.environ``
directly. Secrets only ever arrive from the environment: the provider manifest in
``config/providers.yaml`` references them as ``${VAR}`` placeholders which are
interpolated at load time, so the manifest itself is safe to commit.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.enums import FusionStrategy, ModelTier, Privacy

REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Provider manifest
# ---------------------------------------------------------------------------

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def interpolate_env(value: Any, env: dict[str, str] | None = None) -> Any:
    """Recursively replace ``${VAR}`` and ``${VAR:-default}`` with env values.

    An unset variable with no default resolves to ``None`` rather than an empty
    string, so a provider whose credentials are absent is skipped at registration
    time instead of failing later with a confusing 401.
    """
    source: Any = os.environ if env is None else env

    if isinstance(value, dict):
        return {k: interpolate_env(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate_env(v, env) for v in value]
    if not isinstance(value, str):
        return value

    match = _ENV_PATTERN.fullmatch(value.strip())
    if match:
        # Whole-string placeholder: preserve None so "unset" stays distinguishable.
        name, default = match.group(1), match.group(2)
        resolved = source.get(name)
        if resolved:
            return resolved
        return default if default is not None else None

    def _sub(m: re.Match[str]) -> str:
        return source.get(m.group(1)) or (m.group(2) or "")

    return _ENV_PATTERN.sub(_sub, value)


class ModelConfig(BaseModel):
    """Declared capabilities and economics of one model.

    The router reasons over these attributes and never over provider names, so a
    newly added vendor becomes routable purely by appearing here.
    """

    name: str
    tier: ModelTier = ModelTier.BALANCED
    context_window: int = 8192
    max_output_tokens: int = 2048
    supports_tools: bool = False
    supports_json_schema: bool = False
    supports_streaming: bool = True
    cost_per_1m_input: float = 0.0
    cost_per_1m_output: float = 0.0
    aliases: list[str] = Field(default_factory=list)


class LLMProviderConfig(BaseModel):
    """One entry in the ``llm:`` section of the provider manifest."""

    name: str
    type: str
    base_url: str | None = None
    api_key: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    privacy: Privacy = Privacy.CLOUD
    enabled: bool = True
    timeout_s: float = 120.0
    connect_timeout_s: float = 10.0
    max_concurrency: int = 8
    max_retries: int = 2
    models: list[ModelConfig] = Field(default_factory=list)
    default_model: str | None = None

    @model_validator(mode="after")
    def _default_model_present(self) -> LLMProviderConfig:
        if self.default_model is None and self.models:
            self.default_model = self.models[0].name
        return self

    def model_by_name(self, name: str) -> ModelConfig | None:
        for m in self.models:
            if m.name == name or name in m.aliases:
                return m
        return None


class EmbeddingProviderConfig(BaseModel):
    """One entry in the ``embeddings:`` section of the provider manifest."""

    name: str
    type: str
    model: str
    dimension: int
    base_url: str | None = None
    api_key: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    timeout_s: float = 60.0
    connect_timeout_s: float = 10.0
    # Embedding is a network hop to a possibly-serialized model server, so both
    # the per-call batch and the number of in-flight calls are bounded.
    batch_size: int = 32
    max_concurrency: int = 2
    max_retries: int = 3
    normalize: bool = True


class ProviderManifest(BaseModel):
    llm: list[LLMProviderConfig] = Field(default_factory=list)
    embeddings: list[EmbeddingProviderConfig] = Field(default_factory=list)

    @property
    def enabled_llm(self) -> list[LLMProviderConfig]:
        return [p for p in self.llm if p.enabled]

    @property
    def enabled_embeddings(self) -> list[EmbeddingProviderConfig]:
        return [p for p in self.embeddings if p.enabled]


def load_provider_manifest(
    path: Path | None = None,
    raw_json: str | None = None,
) -> ProviderManifest:
    """Load the provider manifest from JSON (env) or YAML (file).

    ``PROVIDERS_JSON`` wins when set, which is how platforms without a writable
    filesystem supply the manifest.
    """
    if raw_json:
        data = json.loads(raw_json)
    elif path and path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    else:
        data = {}

    return ProviderManifest.model_validate(interpolate_env(data))


# ---------------------------------------------------------------------------
# Settings sections
# ---------------------------------------------------------------------------


class ChunkingSettings(BaseModel):
    target_tokens: int = 512
    max_tokens: int = 768
    min_tokens: int = 80
    overlap_tokens: int = 64
    # Never merge content across sections at or above this heading level.
    hard_boundary_level: int = 2
    prepend_breadcrumb: bool = True

    @model_validator(mode="after")
    def _sane(self) -> ChunkingSettings:
        if self.overlap_tokens >= self.target_tokens:
            raise ValueError("overlap_tokens must be smaller than target_tokens")
        if self.max_tokens < self.target_tokens:
            raise ValueError("max_tokens must be >= target_tokens")
        return self


class OCRSettings(BaseModel):
    provider: str = "tesseract"
    languages: list[str] = Field(default_factory=lambda: ["eng"])
    dpi: int = 300
    # Per-page thresholds for "does this page already have usable text?".
    min_chars_per_page: int = 100
    min_alpha_ratio: float = 0.55
    max_cid_artifact_ratio: float = 0.01
    max_replacement_ratio: float = 0.02
    min_mean_word_len: float = 1.5
    max_mean_word_len: float = 20.0
    # Document-level decision thresholds.
    native_coverage: float = 0.80
    ocr_coverage: float = 0.20
    scanned_image_area: float = 0.85
    max_ocr_pages: int = 500
    timeout_s: float = 300.0


class RetrievalSettings(BaseModel):
    fusion: FusionStrategy = FusionStrategy.RRF
    rrf_k: int = 60
    vector_weight: float = 0.6
    keyword_weight: float = 0.4
    # Candidates pulled from each arm before fusion.
    candidate_k: int = 50
    top_k: int = 8
    min_score: float = 0.0
    # HNSW probe width, retried wider when the vector arm under-returns because
    # of tenant post-filtering. See docs/hybrid-search.md.
    ef_search: int = 200
    ef_search_retry_multiplier: int = 3
    # Multiplicative boost applied to location-scoped chunks before ranking.
    #
    # The value is derived, not guessed. With RRF at k=60, a hit at rank r
    # scores 1/(60+r), so a boost b lifts rank r above rank 1 exactly when
    # b > (60+r)/61. At 1.02 only a rank-2 hit can overtake rank 1; at 1.05 a
    # rank-4 hit could, which would bury relevant organization content beneath
    # irrelevant location content.
    #
    # The right value therefore depends on the fusion strategy's score spread:
    # weighted fusion spans 0-1 and needs a larger boost for the same effect.
    # See docs/hybrid-search.md.
    #
    # Genuine conflicts are not handled by this number at all -- they are
    # handled by topic suppression in services/retrieval/hierarchical.py.
    location_boost: float = 1.02
    # Cosine similarity above which two chunks count as near-duplicates.
    dedup_threshold: float = 0.97
    default_text_search_config: str = "english"
    # Relax the lexical query from AND to OR. PostgreSQL's query parsers join
    # terms with AND, which makes the lexical arm return nothing for most
    # natural-language questions -- and hybrid search silently becomes
    # vector-only. Set False for corpora where precision matters more than
    # recall and queries are keyword-shaped. See docs/hybrid-search.md.
    lexical_relax_to_or: bool = True

    @model_validator(mode="after")
    def _weights(self) -> RetrievalSettings:
        if self.vector_weight + self.keyword_weight <= 0:
            raise ValueError("vector_weight + keyword_weight must be positive")
        return self


class ContextSettings(BaseModel):
    """Token budget for what actually reaches the model."""

    max_context_tokens: int = 4000
    max_tool_result_tokens: int = 1500
    reserve_output_tokens: int = 1024
    # Fraction of a model's context window we are willing to fill.
    context_window_utilization: float = 0.75


class AgentSettings(BaseModel):
    max_iterations: int = 4
    max_tool_calls: int = 8
    deadline_s: float = 90.0
    tool_timeout_s: float = 15.0
    tool_concurrency: int = 4
    max_query_chars: int = 4000
    conversation_ttl_s: int = 60 * 60 * 24
    max_history_messages: int = 20
    plan_cache_ttl_s: int = 300


class QueueSettings(BaseModel):
    provider: str = "redis_streams"
    stream: str = "ingestion"
    group: str = "workers"
    dead_letter_stream: str = "ingestion.dlq"
    # A message held longer than this by a dead consumer is reclaimed.
    visibility_timeout_ms: int = 5 * 60 * 1000
    max_attempts: int = 3
    block_ms: int = 5000
    batch_size: int = 1
    max_stream_length: int = 100_000


class StorageSettings(BaseModel):
    provider: str = "local"
    local_path: Path = Field(default_factory=lambda: REPO_ROOT / "storage")
    bucket: str | None = None
    region: str | None = None
    endpoint_url: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    max_upload_bytes: int = 50 * 1024 * 1024
    allowed_mime_types: list[str] = Field(
        default_factory=lambda: [
            "application/pdf",
            "text/plain",
            "text/markdown",
            # Photographs of menus, rate cards and notices are how a lot of this
            # material actually exists. They go straight to OCR.
            "image/png",
            "image/jpeg",
            "image/webp",
            "image/tiff",
        ]
    )


class SecuritySettings(BaseModel):
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    access_token_ttl_s: int = 60 * 30
    refresh_token_ttl_s: int = 60 * 60 * 24 * 14
    rate_limit_per_minute: int = 60
    #: Separate, lower budget for visitors who are not signed in. Every question
    #: costs a model call, and the public site is reachable by anyone.
    public_rate_limit_per_minute: int = 20
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    @field_validator("jwt_secret")
    @classmethod
    def _not_default_in_prod(cls, v: str) -> str:
        if os.environ.get("APP_ENV") == "production" and v == "change-me-in-production":
            raise ValueError("JWT secret must be set explicitly in production")
        return v


class Settings(BaseSettings):
    """Root settings object. Built once and cached."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["local", "test", "staging", "production"] = "local"
    app_name: str = "enterprise-agent-runtime"
    debug: bool = False
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    api_prefix: str = "/api/v1"

    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/agentdb"
    database_pool_size: int = 10
    database_max_overflow: int = 5
    database_echo: bool = False
    # Aborts any transaction accidentally left open across a slow call.
    idle_in_transaction_timeout_ms: int = 15_000
    statement_timeout_ms: int = 30_000

    redis_url: str = "redis://localhost:6379/0"

    # Dimension of the vector column. Must match the live embedding provider; a
    # boot guard refuses to start when they disagree.
    embedding_dim: int = 768
    embedding_provider: str = "default"

    providers_file: Path = Field(default_factory=lambda: REPO_ROOT / "config" / "providers.yaml")
    providers_json: str | None = None

    chunking: ChunkingSettings = Field(default_factory=ChunkingSettings)
    ocr: OCRSettings = Field(default_factory=OCRSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    context: ContextSettings = Field(default_factory=ContextSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    queue: QueueSettings = Field(default_factory=QueueSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)

    _manifest: ProviderManifest | None = PrivateAttr(default=None)

    @property
    def providers(self) -> ProviderManifest:
        if self._manifest is None:
            self._manifest = load_provider_manifest(self.providers_file, self.providers_json)
        return self._manifest

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def alembic_database_url(self) -> str:
        """URL Alembic uses. Migrations run through the same async driver as the
        app, so there is no second Postgres driver to install or keep in sync."""
        return self.database_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
