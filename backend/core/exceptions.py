"""Structured exception hierarchy for AI Video Studio."""

from __future__ import annotations

from typing import Any


class StudioError(Exception):
    """Base application error mapped cleanly to HTTP responses and SSE errors."""

    def __init__(
        self,
        message: str,
        code: str = "INTERNAL_ERROR",
        status_code: int = 500,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.details: dict[str, Any] = details or {}

    def to_dict(self) -> dict[str, Any]:
        """Serialize error into a consistent JSON response payload."""
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }


class ConfigurationError(StudioError):
    """Raised when required environment settings or optional dependencies are missing."""

    def __init__(
        self,
        message: str,
        code: str = "CONFIGURATION_ERROR",
        status_code: int = 500,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, code=code, status_code=status_code, details=details)


class AuthenticationError(StudioError):
    """Raised when JWT or webhook authentication fails."""

    def __init__(
        self,
        message: str = "Authentication required",
        code: str = "AUTHENTICATION_ERROR",
        status_code: int = 401,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, code=code, status_code=status_code, details=details)


class AuthorizationError(StudioError):
    """Raised when an authenticated user lacks access to a resource."""

    def __init__(
        self,
        message: str = "Permission denied",
        code: str = "AUTHORIZATION_ERROR",
        status_code: int = 403,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, code=code, status_code=status_code, details=details)


class NotFoundError(StudioError):
    """Raised when a requested database record or asset does not exist."""

    def __init__(
        self,
        message: str = "Resource not found",
        code: str = "NOT_FOUND",
        status_code: int = 404,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, code=code, status_code=status_code, details=details)


class ValidationError(StudioError):
    """Raised on domain validation failures beyond raw schema parsing."""

    def __init__(
        self,
        message: str,
        code: str = "VALIDATION_ERROR",
        status_code: int = 422,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, code=code, status_code=status_code, details=details)


class InvalidStateTransitionError(StudioError):
    """Raised when a project state transition violates the FSM."""

    def __init__(
        self,
        message: str,
        code: str = "INVALID_STATE_TRANSITION",
        status_code: int = 409,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, code=code, status_code=status_code, details=details)


class InsufficientCreditsError(StudioError):
    """Raised when a user wallet does not have enough credits for an operation."""

    def __init__(
        self,
        message: str = "Insufficient credits",
        code: str = "INSUFFICIENT_CREDITS",
        status_code: int = 402,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, code=code, status_code=status_code, details=details)


class ProviderError(StudioError):
    """Raised when an external AI, media, or storage provider fails."""

    def __init__(
        self,
        provider: str,
        message: str,
        retryable: bool = False,
        code: str = "PROVIDER_ERROR",
        status_code: int = 502,
        details: dict[str, Any] | None = None,
    ) -> None:
        merged_details = {"provider": provider, "retryable": retryable, **(details or {})}
        super().__init__(
            message=f"[{provider}] {message}",
            code=code,
            status_code=status_code,
            details=merged_details,
        )
        self.provider = provider
        self.retryable = retryable


class ProviderAuthError(ProviderError):
    """Raised when a third-party provider rejects API credentials."""

    def __init__(
        self,
        provider: str,
        message: str = "Provider authentication failed",
        retryable: bool = False,
        code: str = "PROVIDER_AUTH_ERROR",
        status_code: int = 502,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            provider=provider,
            message=message,
            retryable=retryable,
            code=code,
            status_code=status_code,
            details=details,
        )


class ProviderRateLimitError(ProviderError):
    """Raised when a third-party provider throttles or rate-limits requests."""

    def __init__(
        self,
        provider: str,
        message: str = "Provider rate limit exceeded",
        retryable: bool = True,
        code: str = "PROVIDER_RATE_LIMIT",
        status_code: int = 502,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            provider=provider,
            message=message,
            retryable=retryable,
            code=code,
            status_code=status_code,
            details=details,
        )


class DatabaseError(StudioError):
    """Raised when Supabase/PostgreSQL operations fail unexpectedly."""

    def __init__(
        self,
        message: str = "Database operation failed",
        code: str = "DATABASE_ERROR",
        status_code: int = 500,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, code=code, status_code=status_code, details=details)
