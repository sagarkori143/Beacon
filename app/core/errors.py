"""Domain exception hierarchy.

Every layer raises from this hierarchy; the API layer is the only place that
translates them into HTTP. Provider-specific exceptions (httpx errors, vendor
error bodies) are normalized into :class:`ProviderError` inside the provider
package so nothing above it needs to know which vendor was in play.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for every expected error in the system."""

    #: HTTP status the API layer maps this to.
    status_code: int = 500
    #: Stable machine-readable code clients can branch on.
    code: str = "internal_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_problem(self) -> dict[str, Any]:
        """RFC 9457 problem details body."""
        body: dict[str, Any] = {
            "type": f"about:blank#{self.code}",
            "title": self.code.replace("_", " "),
            "status": self.status_code,
            "detail": self.message,
        }
        if self.details:
            body["errors"] = self.details
        return body


# --- configuration / startup ------------------------------------------------


class ConfigurationError(AppError):
    """The system is misconfigured in a way that cannot be recovered at runtime."""

    status_code = 500
    code = "configuration_error"


class EmbeddingSpaceMismatch(ConfigurationError):
    """The live embedding provider disagrees with the indexed embedding space.

    Raised by the boot guard. Starting anyway would either crash on every insert
    or, far worse, silently write vectors from an incompatible space into a
    same-dimension column and quietly destroy retrieval quality.
    """

    code = "embedding_space_mismatch"


# --- auth / tenancy ---------------------------------------------------------


class AuthenticationError(AppError):
    status_code = 401
    code = "unauthenticated"


class AuthorizationError(AppError):
    status_code = 403
    code = "forbidden"


class TenantScopeError(AuthorizationError):
    """A request tried to reach outside the scope its token grants."""

    code = "tenant_scope_violation"


# --- resources --------------------------------------------------------------


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class ValidationError(AppError):
    status_code = 422
    code = "validation_error"


class RateLimitError(AppError):
    status_code = 429
    code = "rate_limited"

    def __init__(self, message: str, *, retry_after_s: int = 60) -> None:
        super().__init__(message, details={"retry_after_s": retry_after_s})
        self.retry_after_s = retry_after_s


# --- documents & ingestion --------------------------------------------------


class UnsupportedFileType(ValidationError):
    code = "unsupported_file_type"


class FileTooLarge(ValidationError):
    code = "file_too_large"


class DocumentParseError(AppError):
    status_code = 422
    code = "document_parse_error"


class IngestionError(AppError):
    """A pipeline stage failed. Carries the stage for the job record."""

    status_code = 500
    code = "ingestion_error"

    def __init__(
        self,
        message: str,
        *,
        stage: str | None = None,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.stage = stage
        self.retryable = retryable


class ValidationGateFailed(IngestionError):
    """A pre-activation gate rejected the version. The previous one stays active."""

    code = "validation_gate_failed"


# --- providers --------------------------------------------------------------


class ProviderError(AppError):
    """Base for every provider failure, regardless of vendor."""

    status_code = 502
    code = "provider_error"

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        retryable: bool = False,
        status: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.provider = provider
        self.retryable = retryable
        self.status = status


class ProviderTimeout(ProviderError):
    status_code = 504
    code = "provider_timeout"

    def __init__(self, message: str, *, provider: str | None = None) -> None:
        super().__init__(message, provider=provider, retryable=True)


class ProviderUnavailable(ProviderError):
    """The provider is down, rate limited, or its circuit breaker is open."""

    status_code = 503
    code = "provider_unavailable"

    def __init__(self, message: str, *, provider: str | None = None) -> None:
        super().__init__(message, provider=provider, retryable=True)


class ProviderAuthError(ProviderError):
    status_code = 502
    code = "provider_auth_error"


class StructuredOutputError(ProviderError):
    """The model could not be coaxed into producing schema-valid output."""

    status_code = 502
    code = "structured_output_error"


class NoModelAvailable(AppError):
    """Routing found no healthy model satisfying the request's requirements."""

    status_code = 503
    code = "no_model_available"

    def __init__(self, message: str, *, retry_after_s: int = 15) -> None:
        super().__init__(message, details={"retry_after_s": retry_after_s})
        self.retry_after_s = retry_after_s


# --- tools ------------------------------------------------------------------


class ToolError(AppError):
    """A tool failed in a way worth surfacing. Most tool failures are *returned*
    to the model as results rather than raised; this is for the rest."""

    status_code = 500
    code = "tool_error"


class UnknownToolError(ToolError):
    status_code = 404
    code = "unknown_tool"
