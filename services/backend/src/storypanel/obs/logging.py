"""Structured JSON logging with redaction and trace correlation.

Context identifiers (request, user, project, workflow, panel, attempt...) are bound with
`bind_context` and travel with every log line. High-cardinality ids belong here and in
traces, never in metric labels.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import sys
from typing import Any

import structlog
from opentelemetry import trace

from storypanel.config import Settings, get_settings

REDACTED = "[redacted]"
_SENSITIVE_KEY = re.compile(
    r"(authorization|cookie|password|secret|token|api[_-]?key|signature|card|cvc|"
    r"signed_url|url_signed|prompt_text|raw_prompt|instruction_text|image_bytes|email)$",
    re.I,
)
_SENSITIVE_VALUE = [
    re.compile(r"\b(sk|pk|rk|whsec)_(live|test)_[A-Za-z0-9]{8,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}"),  # JWTs
    re.compile(r"([?&](X-Amz-Signature|X-Amz-Credential|Signature|sig|token)=)[^&\s\"']+", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.I),
]


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        out = value
        for pat in _SENSITIVE_VALUE:
            out = pat.sub(lambda m: (m.group(1) if m.lastindex else "") + REDACTED, out)
        return out
    if isinstance(value, dict):
        return redact_mapping(value)
    if isinstance(value, list | tuple):
        return [redact_value(v) for v in value]
    return value


def redact_mapping(data: dict[str, Any]) -> dict[str, Any]:
    return {k: (REDACTED if _SENSITIVE_KEY.search(str(k)) else redact_value(v)) for k, v in data.items()}


def pseudonymize(user_id: object, settings: Settings | None = None) -> str:
    """Stable pseudonymous user reference for logs, traces, Sentry and Langfuse."""
    salt = (settings or get_settings()).pseudonym_salt.get_secret_value().encode()
    return "u_" + hmac.new(salt, str(user_id).encode(), hashlib.sha256).hexdigest()[:16]


def _redact_processor(_: Any, __: str, event: dict[str, Any]) -> dict[str, Any]:
    return redact_mapping(event)


def _trace_processor(_: Any, __: str, event: dict[str, Any]) -> dict[str, Any]:
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid:
        event["trace_id"] = format(ctx.trace_id, "032x")
        event["span_id"] = format(ctx.span_id, "016x")
    return event


def configure_logging(service: str | None = None) -> None:
    s = get_settings()
    static = {"service": service or s.service_name, "environment": s.env, "release": s.release}

    def _static(_: Any, __: str, event: dict[str, Any]) -> dict[str, Any]:
        for k, v in static.items():
            event.setdefault(k, v)
        return event

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
            _static,  # type: ignore[list-item]
            _trace_processor,  # type: ignore[list-item]
            structlog.processors.format_exc_info,
            _redact_processor,  # type: ignore[list-item]
            structlog.processors.EventRenamer("message"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(s.log_level)),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=False,
    )
    logging.basicConfig(level=logging.WARNING, stream=sys.stdout, force=True)


def get_logger(name: str = "storypanel") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]


def bind_context(**ids: Any) -> None:
    structlog.contextvars.bind_contextvars(**{k: str(v) for k, v in ids.items() if v is not None})


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()
