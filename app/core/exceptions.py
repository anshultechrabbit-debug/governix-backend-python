from typing import Any


class AppError(Exception):
    """Base for errors that are safe to show to API clients.

    `code` is a stable machine-readable identifier (e.g. VERSION_CONFLICT);
    `message` must never contain secrets or internal details.
    """

    code = "APP_ERROR"
    status_code = 400

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details


class ValidationError(AppError):
    code = "VALIDATION_ERROR"
    status_code = 422


class AuthenticationError(AppError):
    code = "UNAUTHENTICATED"
    status_code = 401


class PermissionDeniedError(AppError):
    code = "PERMISSION_DENIED"
    status_code = 403


class NotFoundError(AppError):
    code = "NOT_FOUND"
    status_code = 404


class ConflictError(AppError):
    code = "CONFLICT"
    status_code = 409


class PayloadTooLargeError(AppError):
    code = "PAYLOAD_TOO_LARGE"
    status_code = 413


class ServiceUnavailableError(AppError):
    code = "SERVICE_UNAVAILABLE"
    status_code = 503


class ConfigurationError(Exception):
    """Invalid or unsupported infrastructure configuration. Raised at startup."""
