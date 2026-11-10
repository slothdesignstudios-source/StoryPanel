"""FastAPI application: middleware, typed error handling, lifecycle."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from storypanel import storage
from storypanel.api.routes import account, editor, projects
from storypanel.config import get_settings
from storypanel.db.session import dispose_engine
from storypanel.errors import AppError, ErrorCode
from storypanel.obs import metrics, tracing
from storypanel.obs.logging import bind_context, clear_context, configure_logging, get_logger
from storypanel.services import events, outbox

log = get_logger("api")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class BodyLimit:
    """Reject oversized bodies whether or not Content-Length is honest."""

    def __init__(self, app: ASGIApp, limit: int) -> None:
        self.app, self.limit = app, limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        seen = 0

        async def too_large() -> None:
            body = AppError(ErrorCode.PAYLOAD_TOO_LARGE, "Request is too large.").to_public(None)
            await JSONResponse(body, status_code=413)(scope, receive, send)

        if declared and declared.isdigit() and int(declared) > self.limit:
            await too_large()
            return

        async def limited() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.limit:
                    raise AppError(ErrorCode.PAYLOAD_TOO_LARGE, "Request is too large.")
            return message

        await self.app(scope, limited, send)


def _error_response(request: Request, exc: AppError) -> JSONResponse:
    rid = getattr(request.state, "request_id", None)
    metrics.errors.labels(exc.code.value, "api").inc()
    return JSONResponse(exc.to_public(rid), status_code=exc.http_status)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    s = get_settings()
    configure_logging("storypanel-api")
    tracing.configure_tracing("storypanel-api")
    app.state.shutting_down = False
    app.state.outbox_wake = asyncio.Event()
    app.state.temporal = None
    stop = asyncio.Event()
    tasks: list[asyncio.Task[Any]] = []
    if not s.is_deployed:
        try:
            await storage.ensure_bucket()
        except Exception:
            log.warning("bucket_unavailable")

    async def connect_temporal() -> None:
        # The API stays up (and keeps accepting work into the outbox) while Temporal is
        # unreachable; the dispatcher starts as soon as a connection succeeds.
        from storypanel.workflows.client import get_temporal_client

        delay = 1.0
        while not stop.is_set():
            try:
                app.state.temporal = await get_temporal_client()
                await outbox.run_dispatcher(app.state.temporal, stop, app.state.outbox_wake)
                return
            except Exception as exc:
                log.warning("temporal_unavailable", error=type(exc).__name__)
                await asyncio.sleep(delay)
                delay = min(30.0, delay * 2)

    if getattr(app.state, "start_dispatcher", True):
        tasks.append(asyncio.create_task(connect_temporal()))
    log.info("api_started", provider_mode=s.provider_mode, auth_mode=s.auth_mode)
    yield
    # Graceful shutdown: fail readiness first so the load balancer drains us.
    app.state.shutting_down = True
    stop.set()
    app.state.outbox_wake.set()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    tracing.flush()
    await events.close_redis()
    await dispose_engine()


def create_app(*, start_dispatcher: bool = True) -> FastAPI:
    s = get_settings()
    app = FastAPI(
        title="StoryPanel API", version="1.0.0", lifespan=lifespan,
        docs_url=None if s.env == "production" else "/docs", redoc_url=None,
    )  # fmt: skip
    app.state.start_dispatcher = start_dispatcher

    @app.middleware("http")
    async def request_context(request: Request,
                              call_next: Callable[[Request], Awaitable[Response]]) -> Response:  # fmt: skip
        incoming = request.headers.get("x-request-id", "")
        rid = incoming if 8 <= len(incoming) <= 64 and incoming.replace("-", "").isalnum() \
            else uuid.uuid4().hex  # fmt: skip
        request.state.request_id = rid
        clear_context()
        bind_context(request_id=rid)
        started = time.monotonic()
        # CSRF posture: the API authenticates with bearer tokens only (never cookies), so a
        # cross-site request cannot carry credentials. As defence in depth, state-changing
        # browser requests must come from an allowed origin.
        origin = request.headers.get("origin")
        if origin and request.method not in SAFE_METHODS and origin not in s.cors_origins \
                and not request.url.path.startswith("/webhooks/"):  # fmt: skip
            response: Response = _error_response(
                request, AppError(ErrorCode.FORBIDDEN, "Origin not allowed."))  # fmt: skip
        else:
            try:
                response = await call_next(request)
            except AppError as exc:  # raised from middleware-level code (e.g. body limit)
                response = _error_response(request, exc)
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")
        elapsed = time.monotonic() - started
        metrics.http_requests.labels(request.method, path, str(response.status_code)).inc()
        metrics.http_latency.labels(request.method, path).observe(elapsed)
        response.headers["X-Request-ID"] = rid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        response.headers.setdefault("Cache-Control", "no-store")
        if s.is_deployed:
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        if path not in ("/healthz", "/readyz", "/metrics"):
            log.info("http_request", method=request.method, route=path,
                     status=response.status_code, duration_ms=int(elapsed * 1000))  # fmt: skip
        return response

    app.add_middleware(BodyLimit, limit=s.max_request_bytes)
    app.add_middleware(
        CORSMiddleware, allow_origins=s.cors_origins, allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["authorization", "content-type", "idempotency-key", "last-event-id",
                       "x-request-id", "traceparent"],
        expose_headers=["x-request-id"], max_age=600,
    )  # fmt: skip

    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError) -> JSONResponse:
        level = log.error if exc.http_status >= 500 else log.info
        level("request_failed", error_code=exc.code.value, retryable=exc.retryable, **exc.meta)
        return _error_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Field paths and messages only: never echo the submitted values back.
        fields = [{"field": ".".join(str(p) for p in e["loc"] if p != "body"), "message": e["msg"]}
                  for e in exc.errors()[:25]]  # fmt: skip
        return _error_response(request, AppError(
            ErrorCode.VALIDATION, "Some fields need attention.", fields=fields))  # fmt: skip

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {401: ErrorCode.UNAUTHENTICATED, 403: ErrorCode.FORBIDDEN, 404: ErrorCode.NOT_FOUND,
                405: ErrorCode.VALIDATION, 413: ErrorCode.PAYLOAD_TOO_LARGE,
                429: ErrorCode.RATE_LIMITED}.get(exc.status_code, ErrorCode.INTERNAL)  # fmt: skip
        message = {ErrorCode.NOT_FOUND: "Resource not found.",
                   ErrorCode.VALIDATION: "Method not allowed."}.get(code, "Request failed.")  # fmt: skip
        resp = _error_response(request, AppError(code, message, retryable=False))
        resp.status_code = exc.status_code
        return resp

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Full detail goes to logs and Sentry; the client gets a stable, generic contract.
        log.error("unhandled_exception", error_code="internal_error", exc_info=exc)
        try:
            import sentry_sdk

            sentry_sdk.set_tag("user_ref", getattr(request.state, "user_ref", None))
            sentry_sdk.capture_exception(exc)
        except Exception:
            metrics.telemetry_failures.labels("sentry").inc()
        return _error_response(request, AppError(
            ErrorCode.INTERNAL, "Something went wrong on our side. Please try again.",
            retryable=True))  # fmt: skip

    @app.get("/metrics", include_in_schema=False)
    async def prometheus() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    app.include_router(projects.router)
    app.include_router(editor.router)
    app.include_router(account.router)
    try:
        FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics")
    except Exception:
        metrics.telemetry_failures.labels("otel_fastapi").inc()
    return app


def app_factory() -> FastAPI:
    return create_app()
