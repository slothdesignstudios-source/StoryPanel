"""OpenTelemetry tracing, Sentry and Langfuse set-up.

Telemetry must never break generation: every exporter is optional, initialisation
failures are logged and counted, and helper calls swallow their own errors.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from storypanel.config import get_settings
from storypanel.obs import metrics
from storypanel.obs.logging import get_logger, redact_mapping, redact_value

log = get_logger("obs")
_configured = False
_langfuse: Any = None


def configure_tracing(service: str, exporter: SpanExporter | None = None) -> None:
    """Idempotent. `exporter` lets tests capture spans in memory."""
    global _configured
    if _configured and exporter is None:
        return
    s = get_settings()
    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": service, "deployment.environment": s.env, "service.version": s.release}
        ),
        sampler=ParentBased(TraceIdRatioBased(s.trace_sample_ratio)),
    )
    try:
        if exporter is not None:
            from opentelemetry.sdk.trace.export import SimpleSpanProcessor

            provider.add_span_processor(SimpleSpanProcessor(exporter))
        elif s.otlp_endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{s.otlp_endpoint}/v1/traces"))
            )
        trace.set_tracer_provider(provider)
    except Exception as exc:  # telemetry is best effort
        metrics.telemetry_failures.labels("otlp").inc()
        log.warning("tracing_init_failed", error=str(exc))
    _configured = True
    _configure_sentry(service)
    _configure_langfuse()


def _scrub_sentry(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any]:
    request = event.get("request") or {}
    for key in ("data", "cookies", "query_string"):
        request.pop(key, None)
    if "headers" in request:
        request["headers"] = redact_mapping(dict(request["headers"]))
    event["request"] = request
    event.pop("user", None)  # only the pseudonymous tag set by the API is kept
    for exc in (event.get("exception") or {}).get("values", []):
        exc["value"] = redact_value(exc.get("value"))
        for frame in (exc.get("stacktrace") or {}).get("frames", []):
            frame.pop("vars", None)
    return event


def _configure_sentry(service: str) -> None:
    s = get_settings()
    if not s.sentry_dsn:
        return
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=s.sentry_dsn.get_secret_value(), environment=s.env, release=s.release,
            send_default_pii=False, include_local_variables=False, traces_sample_rate=0.0,
            before_send=_scrub_sentry, server_name=service,  # type: ignore[arg-type]
        )  # fmt: skip
    except Exception as exc:
        metrics.telemetry_failures.labels("sentry").inc()
        log.warning("sentry_init_failed", error=str(exc))


def _langfuse_mask(*, data: Any, **_: Any) -> Any:
    """Langfuse receives prompt *versions* and metadata, not raw user text, by default."""
    if get_settings().log_prompts:
        return redact_value(data)
    if isinstance(data, str):
        return f"[masked {len(data)} chars]"
    if isinstance(data, dict):
        return {k: _langfuse_mask(data=v) for k, v in redact_mapping(data).items()}
    if isinstance(data, list):
        return [_langfuse_mask(data=v) for v in data]
    return data


def _configure_langfuse() -> None:
    global _langfuse
    s = get_settings()
    if not (s.langfuse_public_key and s.langfuse_secret_key):
        return
    try:
        from langfuse import Langfuse

        _langfuse = Langfuse(
            public_key=s.langfuse_public_key, secret_key=s.langfuse_secret_key.get_secret_value(),
            host=s.langfuse_host, environment=s.env, release=s.release, mask=_langfuse_mask,
            tracer_provider=trace.get_tracer_provider(),  # type: ignore[arg-type]
        )  # fmt: skip
    except Exception as exc:
        metrics.telemetry_failures.labels("langfuse").inc()
        log.warning("langfuse_init_failed", error=str(exc))


@contextmanager
def generation_span(name: str, *, model: str, metadata: dict[str, Any]) -> Iterator[Any]:
    """A Langfuse generation when configured, otherwise a plain OTel span."""
    if _langfuse is not None:
        try:
            cm = _langfuse.start_as_current_observation(
                name=name, as_type="generation", model=model, metadata=metadata
            )
            obs = cm.__enter__()
        except Exception:
            metrics.telemetry_failures.labels("langfuse").inc()
            cm = None
        if cm is not None:
            try:
                yield obs
            finally:
                try:
                    cm.__exit__(None, None, None)
                except Exception:
                    metrics.telemetry_failures.labels("langfuse").inc()
            return
    with tracer().start_as_current_span(name) as span:
        span.set_attribute("gen_ai.request.model", model)
        for k, v in metadata.items():
            span.set_attribute(f"sp.{k}", str(v))
        yield None


def record_generation(obs: Any, *, usage: dict[str, int] | None = None, cost_usd: float = 0.0) -> None:
    if obs is None:
        return
    try:
        obs.update(usage_details=usage or {}, cost_details={"total": cost_usd})
    except Exception:
        metrics.telemetry_failures.labels("langfuse").inc()


def report_exception(exc: BaseException, **tags: Any) -> None:
    """Send an unexpected error to Sentry with low-cardinality tags. Never raises."""
    if not get_settings().sentry_dsn:
        return
    try:
        import sentry_sdk

        with sentry_sdk.new_scope() as scope:
            for key, value in tags.items():
                scope.set_tag(key, str(value))
            sentry_sdk.capture_exception(exc)
    except Exception:
        metrics.telemetry_failures.labels("sentry").inc()


def tracer() -> trace.Tracer:
    return trace.get_tracer("storypanel")


def inject_trace_context() -> dict[str, str]:
    """Carrier for crossing async boundaries (outbox rows, workflow inputs)."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


def extract_trace_context(carrier: dict[str, str] | None) -> Any:
    return propagate.extract(carrier or {})


def flush() -> None:
    try:
        provider = trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush(2000)
        if _langfuse is not None:
            _langfuse.flush()
    except Exception:
        metrics.telemetry_failures.labels("flush").inc()
