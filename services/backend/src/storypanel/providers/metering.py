"""Everything that wraps a provider call: spend limits, distributed concurrency caps,
usage rows, metrics, and reconciliation of paid requests after ambiguous outcomes.

We do not claim exactly-once charging by providers. What we do guarantee:
- one ProviderUsage row per logical paid operation (unique idempotency key);
- the provider's request id is stored *before* waiting for the result;
- a retry first asks the provider about the stored request id, and only submits a new
  request once the earlier one is known to have failed or is unknown to the provider.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from storypanel.config import get_settings
from storypanel.db.models import ProviderUsage
from storypanel.db.session import transaction
from storypanel.errors import AppError, ErrorCode
from storypanel.obs import metrics
from storypanel.obs.logging import get_logger
from storypanel.obs.tracing import generation_span, record_generation
from storypanel.providers.base import (
    CallContext,
    ImageInput,
    ImageRequest,
    ImageResult,
    RequestPending,
    T,
    TextRole,
    Usage,
)  # fmt: skip
from storypanel.providers.registry import get_providers
from storypanel.services.events import redis_client

log = get_logger("providers")

_ACQUIRE = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[3]) then
  redis.call('ZADD', KEYS[1], ARGV[2], ARGV[4])
  redis.call('EXPIRE', KEYS[1], 900)
  return 1
end
return 0
"""


@asynccontextmanager
async def provider_slot(kind: str, lease_seconds: float = 300.0) -> AsyncIterator[None]:
    """Distributed semaphore across all workers; leases expire if a worker dies."""
    s = get_settings()
    limit = {"text": s.provider_concurrency_text, "image": s.provider_concurrency_image,
             "vision": s.provider_concurrency_vision}[kind]  # fmt: skip
    key, token, r = f"sp:slots:{kind}", uuid.uuid4().hex, redis_client()
    delay = 0.05
    while True:
        now = time.time()
        if await r.eval(_ACQUIRE, 1, key, now, now + lease_seconds, limit, token):  # type: ignore[misc]
            break
        await asyncio.sleep(delay)
        delay = min(1.0, delay * 1.5)
    try:
        yield
    finally:
        try:
            await r.zrem(key, token)
        except Exception:
            pass  # the lease expires on its own


def _day() -> str:
    return datetime.now(UTC).strftime("%Y%m%d")


async def check_spend_limits(ctx: CallContext) -> None:
    """Refuse new paid work once a configured budget is used up (non-retryable)."""
    s, r = get_settings(), redis_client()
    scopes = [("global", f"sp:spend:global:{_day()}", s.spend_limit_global_daily_usd)]
    if ctx.user_id:
        scopes.append(("user", f"sp:spend:user:{ctx.user_id}:{_day()}", s.spend_limit_user_daily_usd))
    if ctx.project_id:
        scopes.append(("project", f"sp:spend:project:{ctx.project_id}", s.spend_limit_project_usd))
    values = await r.mget([k for _, k, _ in scopes])
    for (scope, _, limit), raw in zip(scopes, values, strict=True):
        if raw is not None and Decimal(raw) >= limit:
            metrics.spend_limit_hits.labels(scope).inc()
            log.warning("spend_limit_reached", scope=scope)
            raise AppError(ErrorCode.SPEND_LIMIT,
                           "This comic has reached its generation budget. Contact support to continue."
                           if scope != "global" else
                           "Generation is paused for now. Please try again later.")  # fmt: skip


async def _add_spend(ctx: CallContext, cost: Decimal) -> None:
    if cost <= 0:
        return
    r = redis_client()
    pipe = r.pipeline()
    pipe.incrbyfloat(f"sp:spend:global:{_day()}", float(cost))
    pipe.expire(f"sp:spend:global:{_day()}", 3 * 86400)
    if ctx.user_id:
        pipe.incrbyfloat(f"sp:spend:user:{ctx.user_id}:{_day()}", float(cost))
        pipe.expire(f"sp:spend:user:{ctx.user_id}:{_day()}", 3 * 86400)
    if ctx.project_id:
        pipe.incrbyfloat(f"sp:spend:project:{ctx.project_id}", float(cost))
        pipe.expire(f"sp:spend:project:{ctx.project_id}", 90 * 86400)
    await pipe.execute()


def _uuid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


def _observe(usage: Usage, outcome: str) -> None:
    metrics.provider_calls.labels(usage.provider, usage.operation, outcome).inc()
    metrics.provider_latency.labels(usage.provider, usage.operation).observe(usage.latency_ms / 1000)
    if usage.input_tokens:
        metrics.provider_tokens.labels(usage.provider, "input").inc(usage.input_tokens)
        metrics.provider_tokens.labels(usage.provider, "output").inc(usage.output_tokens)
    if usage.images:
        metrics.provider_images.labels(usage.provider, usage.operation).inc(usage.images)
    if usage.cost_usd:
        metrics.provider_cost.labels(usage.provider, usage.operation).inc(float(usage.cost_usd))


async def _record(ctx: CallContext, usage: Usage, *, key: str, status: str = "succeeded",
                  error_code: str | None = None) -> None:  # fmt: skip
    async with transaction() as db:
        await db.execute(
            insert(ProviderUsage)
            .values(id=uuid.uuid4(), user_id=_uuid(ctx.user_id), project_id=_uuid(ctx.project_id),
                    stage=ctx.stage, target_id=ctx.target_id, provider=usage.provider,
                    model=usage.model, operation=usage.operation, idempotency_key=key,
                    provider_request_id=usage.request_id, status=status,
                    input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                    images=usage.images, cost_usd=usage.cost_usd, latency_ms=usage.latency_ms,
                    prompt_version=ctx.prompt_version, error_code=error_code)
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
        )  # fmt: skip
    await _add_spend(ctx, usage.cost_usd)
    log.info("provider_call", provider=usage.provider, model=usage.model,
             operation=usage.operation, stage=ctx.stage, status=status,
             duration_ms=usage.latency_ms, input_tokens=usage.input_tokens,
             output_tokens=usage.output_tokens, images=usage.images,
             cost_usd=float(usage.cost_usd), prompt_version=ctx.prompt_version,
             error_code=error_code, provider_request_id=usage.request_id)  # fmt: skip


async def structured(
    *, role: TextRole, system: str, user: str, schema: type[T], ctx: CallContext,
    images: tuple[ImageInput, ...] = (),
) -> T:  # fmt: skip
    """Metered structured LLM call. Text calls have no provider idempotency; an ambiguous
    timeout is retried by the activity and both attempts are recorded."""
    await check_spend_limits(ctx)
    provider = get_providers().text
    started = time.monotonic()
    meta = {"stage": ctx.stage, "prompt_version": ctx.prompt_version, "attempt": ctx.attempt,
            "project_id": ctx.project_id, "schema": schema.__name__}  # fmt: skip
    kind = "vision" if images else "text"
    async with provider_slot(kind):
        with generation_span(f"{ctx.stage}.{schema.__name__}", model=role, metadata=meta) as obs:
            try:
                result, usage = await provider.structured(
                    role=role, system=system, user=user, schema=schema, ctx=ctx, images=images
                )
            except AppError as exc:
                failed = Usage(provider.name, role, f"text_{role}",
                               latency_ms=int((time.monotonic() - started) * 1000))  # fmt: skip
                outcome = "throttled" if exc.code is ErrorCode.PROVIDER_THROTTLED else "error"
                _observe(failed, outcome)
                await _record(ctx, failed, key=f"text:{uuid.uuid4().hex}", status="failed",
                              error_code=exc.code.value)  # fmt: skip
                raise
            record_generation(obs, usage={"input": usage.input_tokens, "output": usage.output_tokens},
                              cost_usd=float(usage.cost_usd))  # fmt: skip
    _observe(usage, "ok")
    await _record(ctx, usage, key=f"text:{uuid.uuid4().hex}")
    return result


async def _paid_request(
    *, key: str, ctx: CallContext, operation: str, provider_name: str, model: str,
    unit_cost: Decimal, submit: Any, fetch: Any, wait_seconds: float,
) -> ImageResult:  # fmt: skip
    """Submit-or-resume a paid image-type request under one idempotency key."""
    started = time.monotonic()
    async with transaction() as db:
        await db.execute(
            insert(ProviderUsage)
            .values(id=uuid.uuid4(), user_id=_uuid(ctx.user_id), project_id=_uuid(ctx.project_id),
                    stage=ctx.stage, target_id=ctx.target_id, provider=provider_name, model=model,
                    operation=operation, idempotency_key=key, status="started",
                    prompt_version=ctx.prompt_version)
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
        )  # fmt: skip
        row = (
            await db.execute(
                select(ProviderUsage).where(ProviderUsage.idempotency_key == key).with_for_update()
            )
        ).scalar_one()
        request_id = row.provider_request_id
        if row.status == "failed":
            # A previous attempt is known to have failed: a fresh submission is safe.
            row.status, row.provider_request_id, request_id = "started", None, None

    async def mark(status: str, **fields: Any) -> None:
        async with transaction() as db2:
            r = (
                await db2.execute(
                    select(ProviderUsage).where(ProviderUsage.idempotency_key == key).with_for_update()
                )
            ).scalar_one()
            r.status = status
            for k, v in fields.items():
                setattr(r, k, v)

    async with provider_slot("image"):
        if request_id is None:
            await check_spend_limits(ctx)
            try:
                request_id = await submit()
            except AppError as exc:
                # A timeout while submitting is ambiguous (the request may exist) but we
                # hold no id to reconcile with; record it as such for the reconciliation job.
                status = "ambiguous" if exc.code is ErrorCode.PROVIDER_AMBIGUOUS else "failed"
                await mark(status, error_code=exc.code.value)
                metrics.provider_calls.labels(provider_name, operation, status).inc()
                raise
            await mark("started", provider_request_id=request_id)
        else:
            log.info("provider_request_resumed", operation=operation, provider_request_id=request_id)
            metrics.provider_calls.labels(provider_name, operation, "resumed").inc()
        try:
            result: ImageResult = await fetch(request_id, wait_seconds)
        except RequestPending as exc:
            # Still running at the provider: keep the id, let the activity retry and resume.
            raise AppError(ErrorCode.TIMEOUT, "Illustration is taking longer than usual.") from exc
        except AppError as exc:
            if exc.code is ErrorCode.PROVIDER_AMBIGUOUS:
                raise  # id is kept; the next attempt resumes instead of resubmitting
            await mark("failed", error_code=exc.code.value)
            metrics.provider_calls.labels(provider_name, operation, "error").inc()
            raise
    usage = Usage(provider=result.provider, model=result.model, operation=operation, images=1,
                  cost_usd=unit_cost, latency_ms=int((time.monotonic() - started) * 1000),
                  request_id=request_id)  # fmt: skip
    await mark("succeeded", cost_usd=unit_cost, images=1, latency_ms=usage.latency_ms)
    await _add_spend(ctx, unit_cost)
    _observe(usage, "ok")
    log.info("provider_call", provider=usage.provider, model=usage.model, operation=operation,
             stage=ctx.stage, status="succeeded", duration_ms=usage.latency_ms, images=1,
             cost_usd=float(unit_cost), prompt_version=ctx.prompt_version,
             provider_request_id=request_id)  # fmt: skip
    return result


async def generate_image(req: ImageRequest, ctx: CallContext, *, key: str,
                         wait_seconds: float = 150.0) -> tuple[ImageResult, Usage]:  # fmt: skip
    s, provider = get_settings(), get_providers().image
    model = provider.model_for(req)
    cost = s.price_image_edit if (req.mode == "edit" or req.references) else s.price_image_generate
    result = await _paid_request(
        key=key, ctx=ctx, operation=f"image_{req.mode}", provider_name=provider.name, model=model,
        unit_cost=cost, submit=lambda: provider.submit(req, ctx),
        fetch=lambda rid, wait: provider.fetch(model, rid, wait_seconds=wait),
        wait_seconds=wait_seconds,
    )  # fmt: skip
    return result, Usage(result.provider, result.model, f"image_{req.mode}", images=1, cost_usd=cost,
                         request_id=result.request_id)  # fmt: skip


async def upscale_image(image: bytes, ctx: CallContext, *, key: str,
                        wait_seconds: float = 150.0) -> ImageResult:  # fmt: skip
    s, provider = get_settings(), get_providers().upscale
    return await _paid_request(
        key=key, ctx=ctx, operation="upscale", provider_name=provider.name, model=provider.model,
        unit_cost=s.price_upscale, submit=lambda: provider.submit(image, s.upscale_factor, ctx),
        fetch=lambda rid, wait: provider.fetch(rid, wait_seconds=wait), wait_seconds=wait_seconds,
    )  # fmt: skip


async def embed_images(images: list[bytes], ctx: CallContext) -> list[list[float]]:
    s, provider = get_settings(), get_providers().embedding
    await check_spend_limits(ctx)
    started = time.monotonic()
    async with provider_slot("vision"):
        try:
            vectors = await provider.embed_images(images, ctx)
        except AppError as exc:
            metrics.provider_calls.labels(provider.name, "embed", "error").inc()
            log.warning("provider_call", provider=provider.name, operation="embed", status="failed",
                        error_code=exc.code.value)  # fmt: skip
            raise
    usage = Usage(provider.name, provider.model, "embed", images=len(images),
                  cost_usd=s.price_embedding_per_image * len(images),
                  latency_ms=int((time.monotonic() - started) * 1000))  # fmt: skip
    _observe(usage, "ok")
    await _record(ctx, usage, key=f"embed:{uuid.uuid4().hex}")
    return vectors
