"""Typed error contract shared by the API, services and workflows.

Every failure that can reach a user is an AppError with a stable code, a safe message and
a retryability flag. Anything else is mapped to INTERNAL without leaking detail.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    VALIDATION = "validation_error"
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    VERSION_CONFLICT = "version_conflict"
    INVALID_STATE = "invalid_state"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXCEEDED = "quota_exceeded"
    INSUFFICIENT_CREDITS = "insufficient_credits"
    SPEND_LIMIT = "spend_limit_reached"
    MODERATION_BLOCKED = "moderation_blocked"
    PROVIDER_ERROR = "provider_error"
    PROVIDER_THROTTLED = "provider_throttled"
    PROVIDER_BAD_OUTPUT = "provider_bad_output"
    PROVIDER_AMBIGUOUS = "provider_outcome_unknown"
    PROVIDER_AMBIGUOUS_FINAL = "provider_outcome_unknown_final"
    PRICING_UNAVAILABLE = "pricing_unavailable"
    TIMEOUT = "timeout"
    STORAGE = "storage_error"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    INTERNAL = "internal_error"


_STATUS: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION: 422, ErrorCode.UNAUTHENTICATED: 401, ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404, ErrorCode.CONFLICT: 409, ErrorCode.VERSION_CONFLICT: 409,
    ErrorCode.INVALID_STATE: 409, ErrorCode.RATE_LIMITED: 429, ErrorCode.QUOTA_EXCEEDED: 429,
    ErrorCode.INSUFFICIENT_CREDITS: 402, ErrorCode.SPEND_LIMIT: 429,
    ErrorCode.MODERATION_BLOCKED: 422, ErrorCode.PROVIDER_ERROR: 502,
    ErrorCode.PROVIDER_THROTTLED: 503, ErrorCode.PROVIDER_BAD_OUTPUT: 502,
    ErrorCode.PROVIDER_AMBIGUOUS: 502, ErrorCode.PROVIDER_AMBIGUOUS_FINAL: 502,
    ErrorCode.PRICING_UNAVAILABLE: 503, ErrorCode.TIMEOUT: 504, ErrorCode.STORAGE: 503,
    ErrorCode.PAYLOAD_TOO_LARGE: 413, ErrorCode.INTERNAL: 500,
}  # fmt: skip

_RETRYABLE = {
    ErrorCode.RATE_LIMITED, ErrorCode.PROVIDER_ERROR, ErrorCode.PROVIDER_THROTTLED,
    ErrorCode.PROVIDER_BAD_OUTPUT, ErrorCode.TIMEOUT, ErrorCode.STORAGE,
}  # fmt: skip


class AppError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        fields: list[dict[str, str]] | None = None,
        retryable: bool | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fields = fields or []
        self.retryable = (code in _RETRYABLE) if retryable is None else retryable
        self.meta = meta or {}  # internal only: logged, never serialised to clients

    @property
    def http_status(self) -> int:
        return _STATUS[self.code]

    def to_public(self, request_id: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
            "request_id": request_id,
            "retryable": self.retryable,
        }
        if self.fields:
            body["fields"] = self.fields
        return {"error": body}


def not_found(what: str = "Resource") -> AppError:
    # Used for both "does not exist" and "not yours" so ownership cannot be probed.
    return AppError(ErrorCode.NOT_FOUND, f"{what} not found.")


def invalid_state(message: str) -> AppError:
    return AppError(ErrorCode.INVALID_STATE, message)


def version_conflict(what: str, current: int) -> AppError:
    return AppError(
        ErrorCode.VERSION_CONFLICT,
        f"{what} was changed elsewhere. Reload to see the latest version.",
        meta={"current_version": current},
    )


# Errors a Temporal activity must not retry: retrying cannot change the outcome.
NON_RETRYABLE_CODES = frozenset(
    c.value for c in ErrorCode if c not in _RETRYABLE and c is not ErrorCode.INTERNAL
)
