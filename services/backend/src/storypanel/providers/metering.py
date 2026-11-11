"""Everything around a paid provider call: price lookup, spend reservation, concurrency,
fallback routing, settlement, cancellation and reconciliation.

Lifecycle of one logical paid operation (one `provider_usage` row, unique key):

    reserved -> submitting -> started(request id) -> succeeded | failed | cancelled
                    \\-> ambiguous (outcome unknown; never auto-resubmitted)

- Before dispatch the *estimated* cost is reserved against the per-project, per-user-day
  and global-day ceilings inside one serialised transaction, so concurrent calls cannot
  jointly exceed a ceiling. `cost_counted_usd` is what budgets see: the estimate while the
  outcome is open, the actual cost once known.
- The provider request id is stored before waiting. A retry resumes that id; it submits
  anew only when the provider has said the earlier request failed or never existed.
- If submission itself timed out, or a worker died between "submitting" and storing the
  id, the outcome is unknown. The row becomes `ambiguous`, keeps its estimated cost, and
  the operation fails permanently instead of risking a second paid generation. A local
  idempotency key is not a provider-side deduplication guarantee, so we never rely on it.

Customer credits live in the credit ledger and are never computed from these figures.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from storypanel.config import get_settings
from storypanel.db.models import ProviderUsage
from storypanel.db.session import transaction
from storypanel.errors import AppError, ErrorCode
from storypanel.obs import metrics
from storypanel.obs.logging import get_logger
from storypanel.obs.tracing import generation_span, record_generation
from storypanel.providers import pricing
from storypanel.providers.base import (
    CallContext,
    ImageInput,
    ImageRequest,
    ImageResult,
    RequestPending,
    RequestUnknown,
    T,
    TextRole,
    Usage,
)  # fmt: skip
from storypanel.providers.registry import get_providers
from storypanel.services.events import redis_client

log = get_logger("providers")
OPEN_STATES = ("reserved", "submitting", "started", "ambiguous", "cancelled")

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
        if await r.eval(_ACQUIRE, 1, key, now, now + lease_seconds, limit, token):  # type: ignore[misc,no-untyped-call]
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


def _uuid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


# ------------------------------------------------------------------ spend ceilings
def _day_start() -> datetime:
    now = datetime.now(UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def spend_totals(db: AsyncSession, ctx: CallContext) -> dict[str, Decimal]:
    day = _day_start()
    counted = func.coalesce(func.sum(ProviderUsage.cost_counted_usd), 0)
    totals = {
        "global": (await db.execute(select(counted).where(ProviderUsage.created_at >= day))).scalar_one()
    }
    if ctx.user_id:
        totals["user"] = (await db.execute(select(counted).where(
            ProviderUsage.created_at >= day, ProviderUsage.user_id == _uuid(ctx.user_id)))).scalar_one()  # fmt: skip
    if ctx.project_id:
        totals["project"] = (await db.execute(select(counted).where(
            ProviderUsage.project_id == _uuid(ctx.project_id)))).scalar_one()  # fmt: skip
    return {k: Decimal(v) for k, v in totals.items()}


def _limits() -> dict[str, Decimal]:
    s = get_settings()
    return {"global": s.spend_limit_global_daily_usd, "user": s.spend_limit_user_daily_usd,
            "project": s.spend_limit_project_usd}  # fmt: skip


async def check_spend_limits(ctx: CallContext, estimate: Decimal = Decimal(0)) -> None:
    """Refuse when reserving `estimate` would cross any ceiling. Read-only variant."""
    async with transaction() as db:
        _enforce(await spend_totals(db, ctx), estimate)


def _enforce(totals: dict[str, Decimal], estimate: Decimal) -> None:
    for scope, total in totals.items():
        if total + estimate > _limits()[scope]:
            metrics.spend_limit_hits.labels(scope).inc()
            log.warning(
                "spend_limit_reached", scope=scope, total_usd=float(total), estimate_usd=float(estimate)
            )
            raise AppError(ErrorCode.SPEND_LIMIT,
                           "Generation is paused for now. Please try again later." if scope == "global" else
                           "This comic has reached its generation budget. Contact support to continue.")  # fmt: skip


async def _reserve(ctx: CallContext, *, key: str, provider: str, model: str, operation: str,
                   estimate: Decimal, price: pricing.Price) -> ProviderUsage:  # fmt: skip
    """Create (or reopen after a definitive failure) the usage row, holding `estimate`
    against every ceiling. Returns the row as it stands after the reservation."""
    async with transaction() as db:
        # One lock serialises all reservations, so two calls cannot both see room under a
        # ceiling and jointly exceed it. Each transaction is a few milliseconds long.
        await db.execute(text("select pg_advisory_xact_lock(hashtext('sp-provider-spend'))"))
        row = (await db.execute(select(ProviderUsage).where(ProviderUsage.idempotency_key == key)
                                .with_for_update())).scalar_one_or_none()  # fmt: skip
        if row is not None and row.status not in ("failed",):
            return row  # reserved/started/ambiguous/succeeded: the caller decides what that means
        _enforce(await spend_totals(db, ctx), estimate)
        values = dict(user_id=_uuid(ctx.user_id), project_id=_uuid(ctx.project_id), stage=ctx.stage,
                      target_id=ctx.target_id, provider=provider, model=model, operation=operation,
                      status="reserved", cost_estimate_usd=estimate, cost_counted_usd=estimate,
                      cost_usd=Decimal(0), cost_basis="estimated", price_ref=price.ref(),
                      prompt_version=ctx.prompt_version, provider_request_id=None, error_code=None)  # fmt: skip
        if row is None:
            await db.execute(insert(ProviderUsage).values(id=uuid.uuid4(), idempotency_key=key,
                                                          submissions=0, **values))  # fmt: skip
        else:
            for k, v in values.items():
                setattr(row, k, v)
        await db.flush()
        return (
            await db.execute(select(ProviderUsage).where(ProviderUsage.idempotency_key == key))
        ).scalar_one()


async def _archive_lost_result(key: str) -> None:
    async with transaction() as db:
        row = (
            await db.execute(select(ProviderUsage).where(ProviderUsage.idempotency_key == key))
        ).scalar_one()
        await db.execute(insert(ProviderUsage).values(
            id=uuid.uuid4(), idempotency_key=f"{key}:lost:{uuid.uuid4().hex[:8]}", user_id=row.user_id,
            project_id=row.project_id, stage=row.stage, target_id=row.target_id, provider=row.provider,
            model=row.model, operation=row.operation, status="succeeded_unused", images=row.images,
            provider_request_id=row.provider_request_id, cost_usd=row.cost_usd,
            cost_estimate_usd=row.cost_estimate_usd, cost_counted_usd=row.cost_counted_usd,
            cost_basis=row.cost_basis, price_ref=row.price_ref, submissions=row.submissions,
            error_code="result_lost_before_storage"))  # fmt: skip
    log.warning("provider_result_lost", operation=row.operation, provider_request_id=row.provider_request_id)


async def _update(key: str, **fields: Any) -> tuple[str, str]:
    async with transaction() as db:
        row = (await db.execute(select(ProviderUsage).where(ProviderUsage.idempotency_key == key)
                                .with_for_update())).scalar_one()  # fmt: skip
        for k, v in fields.items():
            setattr(row, k, v)
        if fields.get("status") == "submitting":
            row.submissions += 1
        return row.provider, row.operation


async def _settle(key: str, *, status: str, actual: Decimal | None, estimate: Decimal, **fields: Any) -> None:
    """Close the row. Known cost replaces the estimate; unknown cost keeps the estimate."""
    s = get_settings()
    if actual is not None:
        counted, basis = actual, "actual"
    elif status == "failed":
        # The provider said the request failed. Whether failed requests are billed is not
        # documented for every provider, so by default we keep counting the estimate.
        counted = estimate if s.count_failed_requests_as_billed else Decimal(0)
        basis = "estimated_failed" if s.count_failed_requests_as_billed else "assumed_unbilled"
    elif status == "released":
        counted, basis = Decimal(0), "never_submitted"
    else:
        counted, basis = estimate, "estimated"
    provider, operation = await _update(key, status=status, cost_counted_usd=counted, cost_basis=basis,
                                        cost_usd=actual if actual is not None else Decimal(0), **fields)  # fmt: skip
    if counted:
        metrics.provider_cost.labels(provider, operation, basis).inc(float(counted))


def _observe(provider: str, operation: str, outcome: str, latency_ms: int) -> None:
    metrics.provider_calls.labels(provider, operation, outcome).inc()
    metrics.provider_latency.labels(provider, operation).observe(latency_ms / 1000)


# ------------------------------------------------------------------ text
async def structured(
    *, role: TextRole, system: str, user: str, schema: type[T], ctx: CallContext,
    images: tuple[ImageInput, ...] = (),
) -> T:  # fmt: skip
    """Metered structured LLM call. LLM APIs offer no request id to resume by, so a timeout
    is a failure of this attempt; its estimated cost stays counted."""
    provider = get_providers().text
    model = provider.model_for(role)
    price = pricing.require(provider.name, model)
    images_px = [_pixels(i.data) for i in images]
    estimate = pricing.text_estimate(price, input_chars=len(system) + len(user), images_px=images_px,
                                     max_tokens=provider.max_tokens)  # fmt: skip
    key = f"text:{uuid.uuid4().hex}"
    operation = f"text_{role}"
    await _reserve(ctx, key=key, provider=provider.name, model=model, operation=operation, estimate=estimate,
                   price=price)  # fmt: skip
    started = time.monotonic()
    meta = {"stage": ctx.stage, "prompt_version": ctx.prompt_version, "attempt": ctx.attempt,
            "project_id": ctx.project_id, "schema": schema.__name__}  # fmt: skip
    async with provider_slot("vision" if images else "text"):
        await _update(key, status="submitting")
        with generation_span(f"{ctx.stage}.{schema.__name__}", model=model, metadata=meta) as obs:
            try:
                result, usage = await provider.structured(role=role, system=system, user=user, schema=schema,
                                                          ctx=ctx, images=images)  # fmt: skip
            except AppError as exc:
                latency = int((time.monotonic() - started) * 1000)
                outcome = "throttled" if exc.code is ErrorCode.PROVIDER_THROTTLED else "error"
                _observe(provider.name, operation, outcome, latency)
                # A refusal or a 4xx was answered by the provider: billed or not, the outcome is
                # known. Timeouts and connection errors leave it unknown (estimate kept).
                known = exc.code in (ErrorCode.MODERATION_BLOCKED, ErrorCode.PROVIDER_BAD_OUTPUT,
                                     ErrorCode.PROVIDER_THROTTLED) or not exc.retryable  # fmt: skip
                await _settle(key, status="failed" if known else "ambiguous", actual=None, estimate=estimate,
                              error_code=exc.code.value, latency_ms=latency)  # fmt: skip
                raise
            except asyncio.CancelledError:
                await asyncio.shield(_settle(key, status="cancelled", actual=None, estimate=estimate))
                raise
            served = pricing.table().get((provider.name, usage.model))
            actual = pricing.text_cost(served, usage.input_tokens, usage.output_tokens) if served else None
            record_generation(obs, usage={"input": usage.input_tokens, "output": usage.output_tokens},
                              cost_usd=float(actual if actual is not None else estimate))  # fmt: skip
    latency = int((time.monotonic() - started) * 1000)
    await _settle(key, status="succeeded", actual=actual, estimate=estimate, model=usage.model,
                  input_tokens=usage.input_tokens, output_tokens=usage.output_tokens, latency_ms=latency,
                  provider_request_id=usage.request_id)  # fmt: skip
    _observe(provider.name, operation, "ok", latency)
    metrics.provider_tokens.labels(provider.name, "input").inc(usage.input_tokens)
    metrics.provider_tokens.labels(provider.name, "output").inc(usage.output_tokens)
    log.info("provider_call", provider=provider.name, model=usage.model, operation=operation, stage=ctx.stage,
             status="succeeded", duration_ms=latency, input_tokens=usage.input_tokens,
             output_tokens=usage.output_tokens, cost_usd=float(actual if actual is not None else estimate),
             cost_basis="actual" if actual is not None else "estimated", prompt_version=ctx.prompt_version,
             provider_request_id=usage.request_id)  # fmt: skip
    return result


def _pixels(data: bytes) -> int:
    import io

    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as img:
            return int(img.width * img.height)
    except Exception:
        return 4_000_000  # unknown: assume large, which over-reserves rather than under


# ------------------------------------------------------------------ paid async requests
async def _paid_request(
    *, key: str, ctx: CallContext, operation: str, provider_name: str, model: str, price: pricing.Price,
    estimate: Decimal, submit: Callable[[], Awaitable[str]], fetch: Callable[[str, float], Awaitable[ImageResult]],
    cancel: Callable[[str], Awaitable[None]], wait_seconds: float,
) -> ImageResult:  # fmt: skip
    """Submit-or-resume one paid request under one key (see module docstring)."""
    started = time.monotonic()
    row = await _reserve(ctx, key=key, provider=provider_name, model=model, operation=operation,
                         estimate=estimate, price=price)  # fmt: skip
    # A request that already succeeded can be asked for again: the caller may have died
    # after the provider finished but before it stored the image. Re-fetching by request id
    # costs nothing; only if the provider no longer has it is a new submission made.
    refetch = row.status in ("succeeded", "succeeded_unused") and bool(row.provider_request_id)
    if row.status == "ambiguous" or (row.status == "submitting" and not row.provider_request_id):
        # A previous attempt may have reached the provider without us learning its id.
        if row.status == "submitting":
            await _settle(key, status="ambiguous", actual=None, estimate=row.cost_estimate_usd,
                          error_code=ErrorCode.PROVIDER_AMBIGUOUS_FINAL.value)  # fmt: skip
        metrics.provider_calls.labels(provider_name, operation, "ambiguous").inc()
        raise AppError(ErrorCode.PROVIDER_AMBIGUOUS_FINAL,
                       "We couldn't confirm whether the illustration service received this request, "
                       "so we didn't send it again. You can retry it.")  # fmt: skip
    request_id = row.provider_request_id
    model = row.model  # a resumed request stays on the model that accepted it
    estimate = row.cost_estimate_usd

    async with provider_slot("image"):
        if request_id is None:
            await _update(key, status="submitting")
            try:
                request_id = await submit()
            except AppError as exc:
                latency = int((time.monotonic() - started) * 1000)
                if exc.code is ErrorCode.PROVIDER_AMBIGUOUS:
                    await _settle(key, status="ambiguous", actual=None, estimate=estimate,
                                  error_code=ErrorCode.PROVIDER_AMBIGUOUS_FINAL.value, latency_ms=latency)  # fmt: skip
                    _observe(provider_name, operation, "ambiguous", latency)
                    raise AppError(ErrorCode.PROVIDER_AMBIGUOUS_FINAL,
                                   "We couldn't confirm whether the illustration service received this "
                                   "request, so we didn't send it again. You can retry it.") from exc  # fmt: skip
                # Rejected at submission: nothing was queued, so nothing was produced.
                await _settle(key, status="released", actual=None, estimate=estimate,
                              error_code=exc.code.value, latency_ms=latency)  # fmt: skip
                await _update(key, status="failed")
                _observe(provider_name, operation, "throttled" if exc.code is ErrorCode.PROVIDER_THROTTLED
                         else "error", latency)  # fmt: skip
                raise
            await _update(key, status="started", provider_request_id=request_id)
        else:
            log.info("provider_request_resumed", operation=operation, provider_request_id=request_id)
            metrics.provider_calls.labels(provider_name, operation, "resumed").inc()
        try:
            result = await fetch(request_id, wait_seconds)
        except RequestPending as exc:
            # Still running at the provider: keep the id; the activity retry resumes it.
            raise AppError(ErrorCode.TIMEOUT, "Illustration is taking longer than usual.") from exc
        except RequestUnknown as exc:
            if refetch:
                # The first result was paid for and is gone. Keep that cost on record under a
                # separate key so the retry's new submission does not erase it.
                await _archive_lost_result(key)
            await _settle(key, status="released", actual=None, estimate=estimate,
                          error_code="provider_request_unknown")  # fmt: skip
            await _update(key, status="failed")
            raise AppError(ErrorCode.PROVIDER_ERROR, "The illustration service lost this request.") from exc
        except AppError as exc:
            if exc.code in (ErrorCode.PROVIDER_AMBIGUOUS, ErrorCode.TIMEOUT):
                raise  # id kept; the next attempt resumes instead of resubmitting
            await _settle(key, status="failed", actual=None, estimate=estimate, error_code=exc.code.value,
                          latency_ms=int((time.monotonic() - started) * 1000))  # fmt: skip
            _observe(provider_name, operation, "error", int((time.monotonic() - started) * 1000))
            raise
        except asyncio.CancelledError:
            # The workflow was cancelled: stop the provider work too, and keep the estimate
            # counted because the provider may already have billed it.
            async def _stop() -> None:
                try:
                    await cancel(request_id)
                except Exception:
                    log.warning("provider_cancel_failed", provider_request_id=request_id)
                await _settle(key, status="cancelled", actual=None, estimate=estimate)

            await asyncio.shield(_stop())
            raise
    latency = int((time.monotonic() - started) * 1000)
    actual = (
        pricing.image_cost(price, pixels=_pixels(result.data)) if price.unit != "per_image" else price.usd
    )
    await _settle(key, status="succeeded", actual=actual, estimate=estimate, images=1, latency_ms=latency,
                  model=result.model)  # fmt: skip
    _observe(provider_name, operation, "ok", latency)
    metrics.provider_images.labels(provider_name, operation).inc()
    log.info("provider_call", provider=provider_name, model=result.model, operation=operation, stage=ctx.stage,
             status="succeeded", duration_ms=latency, images=1, cost_usd=float(actual), cost_basis="actual",
             prompt_version=ctx.prompt_version, provider_request_id=request_id)  # fmt: skip
    return result


# ------------------------------------------------------------------ fallback routing
async def _breaker_open(model: str) -> bool:
    try:
        return bool(await redis_client().exists(f"sp:breaker:{model}"))
    except Exception:
        return False


async def _breaker_failure(model: str) -> None:
    s = get_settings()
    try:
        r = redis_client()
        n = await r.incr(f"sp:breakerfail:{model}")
        await r.expire(f"sp:breakerfail:{model}", 60)
        if n >= s.image_breaker_failures:
            await r.set(f"sp:breaker:{model}", "1", ex=s.image_breaker_open_seconds)
            log.warning("provider_breaker_open", model=model, failures=n)
            metrics.provider_calls.labels("fal", "breaker", "open").inc()
    except Exception:
        pass


def _image_pixels(req: ImageRequest, model: str) -> int:
    from storypanel.providers.fal_profiles import profile_for

    p = profile_for(model, req.role)  # type: ignore[arg-type]
    if p is not None and p.aspect_mode == "image_size_wh":
        size = p.size_args(req.aspect)["image_size"]
        return int(size["width"] * size["height"])
    return 1_048_576  # 1K output


async def generate_image(req: ImageRequest, ctx: CallContext, *, key: str,
                         wait_seconds: float = 150.0) -> tuple[ImageResult, Usage]:  # fmt: skip
    """Paid image call with automatic fallback when the primary model is failing.

    Fallback happens only for a *new* submission (a resumed request stays where it is), and
    only when the circuit breaker for the primary is open. Content refusals never trigger it.
    """
    provider = get_providers().image
    primary = provider.model_for(req)
    fallback = provider.fallback_for(req)
    model = fallback if fallback and await _breaker_open(primary) else primary
    if model != primary:
        log.warning("provider_fallback_used", primary=primary, fallback=model, stage=ctx.stage)
    price = pricing.require(provider.name, model)
    estimate = pricing.image_cost(price, pixels=_image_pixels(req, model))
    try:
        result = await _paid_request(
            key=key, ctx=ctx, operation=f"image_{req.role}", provider_name=provider.name, model=model,
            price=price, estimate=estimate, submit=lambda: provider.submit(req, ctx, model=model),
            fetch=lambda rid, wait: provider.fetch(model, rid, wait_seconds=wait),
            cancel=lambda rid: provider.cancel(model, rid), wait_seconds=wait_seconds,
        )  # fmt: skip
    except AppError as exc:
        if (
            model == primary
            and exc.code in (ErrorCode.PROVIDER_ERROR, ErrorCode.PROVIDER_THROTTLED)
            and exc.retryable
        ):
            await _breaker_failure(primary)
        raise
    return result, Usage(result.provider, result.model, f"image_{req.role}", images=1,
                         cost_usd=pricing.image_cost(pricing.require(provider.name, result.model),
                                                     pixels=_pixels(result.data)),
                         request_id=result.request_id)  # fmt: skip


async def upscale_image(
    image: bytes, ctx: CallContext, *, key: str, wait_seconds: float = 150.0
) -> ImageResult:
    s, provider = get_settings(), get_providers().upscale
    price = pricing.require(provider.name, provider.model)
    pixels = _pixels(image) * s.upscale_factor**2
    return await _paid_request(
        key=key, ctx=ctx, operation="upscale", provider_name=provider.name, model=provider.model, price=price,
        estimate=pricing.image_cost(price, pixels=pixels),
        submit=lambda: provider.submit(image, s.upscale_factor, ctx),
        fetch=lambda rid, wait: provider.fetch(rid, wait_seconds=wait), cancel=provider.cancel,
        wait_seconds=wait_seconds,
    )  # fmt: skip


async def embed_images(images: list[bytes], ctx: CallContext) -> list[list[float]]:
    provider = get_providers().embedding
    price = pricing.require(provider.name, provider.model)
    pixels = sum(_pixels(i) for i in images)
    cost = pricing.image_cost(price, images=1, pixels=pixels)
    key = f"embed:{uuid.uuid4().hex}"
    await _reserve(ctx, key=key, provider=provider.name, model=provider.model, operation="embed",
                   estimate=cost, price=price)  # fmt: skip
    started = time.monotonic()
    async with provider_slot("vision"):
        await _update(key, status="submitting")
        try:
            vectors = await provider.embed_images(images, ctx)
        except AppError as exc:
            latency = int((time.monotonic() - started) * 1000)
            _observe(provider.name, "embed", "error", latency)
            await _settle(key, status="failed" if not exc.retryable else "ambiguous", actual=None,
                          estimate=cost, error_code=exc.code.value, latency_ms=latency)  # fmt: skip
            raise
    latency = int((time.monotonic() - started) * 1000)
    await _settle(key, status="succeeded", actual=cost, estimate=cost, images=len(images), latency_ms=latency)
    _observe(provider.name, "embed", "ok", latency)
    return vectors


# ------------------------------------------------------------------ reconciliation
async def reconcile_open_requests(older_than: timedelta = timedelta(minutes=30)) -> dict[str, int]:
    """Resolve rows whose worker went away. Asks the provider about every stored request id;
    rows with no id are released (never sent) or marked ambiguous (maybe sent)."""
    cutoff = datetime.now(UTC) - older_than
    outcome = {"released": 0, "ambiguous": 0, "succeeded_unused": 0, "failed": 0, "pending": 0}
    async with transaction() as db:
        rows = list((await db.execute(select(ProviderUsage).where(
            ProviderUsage.status.in_(("reserved", "submitting", "started")),
            ProviderUsage.updated_at < cutoff).limit(200))).scalars())  # fmt: skip
    providers = get_providers()
    for row in rows:
        if row.status == "reserved":
            await _settle(row.idempotency_key, status="released", actual=None, estimate=row.cost_estimate_usd)
            outcome["released"] += 1
            continue
        if row.status == "submitting" or not row.provider_request_id:
            await _settle(row.idempotency_key, status="ambiguous", actual=None, estimate=row.cost_estimate_usd,
                          error_code=ErrorCode.PROVIDER_AMBIGUOUS_FINAL.value)  # fmt: skip
            outcome["ambiguous"] += 1
            continue
        try:
            if row.operation == "upscale":
                await providers.upscale.fetch(row.provider_request_id, wait_seconds=0)
            else:
                await providers.image.fetch(row.model, row.provider_request_id, wait_seconds=0)
            # Produced and (presumably) billed, but nobody stored the result.
            await _settle(row.idempotency_key, status="succeeded_unused", actual=row.cost_estimate_usd,
                          estimate=row.cost_estimate_usd)  # fmt: skip
            outcome["succeeded_unused"] += 1
        except RequestPending:
            outcome["pending"] += 1
        except RequestUnknown:
            await _settle(row.idempotency_key, status="released", actual=None, estimate=row.cost_estimate_usd)
            outcome["released"] += 1
        except AppError as exc:
            await _settle(row.idempotency_key, status="failed", actual=None, estimate=row.cost_estimate_usd,
                          error_code=exc.code.value)  # fmt: skip
            outcome["failed"] += 1
    async with transaction() as db:
        unresolved = (await db.execute(select(func.coalesce(func.sum(ProviderUsage.cost_counted_usd), 0)).where(
            ProviderUsage.status.in_(OPEN_STATES), ProviderUsage.cost_basis != "actual"))).scalar_one()  # fmt: skip
    metrics.provider_unreconciled_usd.set(float(unresolved))
    return outcome
