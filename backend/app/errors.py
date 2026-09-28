"""Application errors. `message` is always safe to show to end users."""
from __future__ import annotations


class AppError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str | None = None, *, code: str | None = None, status_code: int | None = None) -> None:
        self.message = message or "Something went wrong."
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        super().__init__(self.message)


class BadRequest(AppError):
    status_code = 400
    code = "bad_request"


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"


class Conflict(AppError):
    status_code = 409
    code = "conflict"


class PayloadTooLarge(AppError):
    status_code = 413
    code = "file_too_large"


class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"


class UpstreamError(AppError):
    status_code = 502
    code = "upstream_error"


class GuardrailUnavailable(AppError):
    status_code = 503
    code = "safety_check_unavailable"


class NonRetryableIngestError(AppError):
    """Problem with the document itself (unreadable, flagged, too big). Retrying will not help."""

    status_code = 422
    code = "unprocessable_document"

    def __init__(self, message: str, *, stage: str = "parse", category: str | None = None) -> None:
        super().__init__(message)
        self.stage = stage
        self.category = category
