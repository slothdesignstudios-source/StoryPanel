"""Everything around a paid provider call: price lookup, spend reservation, concurrency,
fallback routing, settlement, cancellation and reconciliation.

Data model. A *logical operation* (`provider_usage`, unique idempotency key such as
"panel:<version id>") owns one or more immutable *attempts* (`provider_attempts`). An
attempt is one provider submission. It is never reopened: a retry after a failure is a
new attempt, so the cost of every potentially billed request stays counted. Spending
ceilings sum `provider_attempts.cost_counted_usd`.

Attempt lifecycle:

    reserved -> submitting -> started(request id) -> succeeded [-> succeeded_unused]
        |            |               |-> failed     (provider: job failed / unusable output)
        |            |               |-> lost       (id unknown / result expired; NOT proof of no charge)
        |            |               '-> cancelled  (we cancelled) -> cancel_confirmed (provider acknowledged;
        |            |                                 still an unresolved potential charge)
        |            '-> ambiguous (submission outcome unknown: never resubmitted)
        '-> released (rejected before queueing)

Rules:
- The persisted attempt (provider, model, request id, price) governs every resume, fetch,
  cancellation and settlement. Routing (circuit breaker) only chooses the model of a *new*
  attempt.
- Transient retrieval failures (5xx, resets, timeouts while polling or downloading) keep
  the attempt open and retry retrieval. Only a definitive provider job failure, or a lost
  result, permits a new submission.
- Before dispatch the estimate is reserved against the per-project, per-user-day and
  global-day ceilings in one serialised transaction. Estimates are conservative
  heuristics, not guaranteed upper bounds; settlement replaces them with the usage-based
  calculation, which is still an operator-price calculation and never labelled as a
  provider-confirmed charge (see `record_provider_charges`).
- A local idempotency key is not a provider-side deduplication guarantee; we never rely
  on one.

Customer credits live in the credit ledger and are never computed from these figures.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from storypanel.config import get_settings
from storypanel.db.models import ProviderAttempt, ProviderUsage, SpendCap
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
    JobFailed,
    RequestPending,
    RequestUnknown,
    RetrievalFailed,
    SyncImageResult,
    T,
    TextProvider,
    TextRole,
    Usage,
)  # fmt: skip
from storypanel.providers.registry import get_providers
from storypanel.services.events import redis_client

log = get_logger("providers")
# attempt states whose outcome reconciliation can still resolve
RESOLVABLE = ("reserved", "submitting", "started", "cancelled")
SETTLED_BASES = ("calculated", "provider_confirmed", "released", "assumed_unbilled")
UNRESOLVED_BASES = ("reserved", "unresolved")

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


async def spend_totals(db: AsyncSession, ctx: CallContext) -> dict[str, tuple[Decimal, Decimal]]:
    """scope -> (counted spend, limit), over every attempt including failed, lost and
    unresolved ones. A user with a SpendCap (e.g. an evaluation run's dedicated user) is held
    to that lifetime cap instead of the daily per-user ceiling."""
    s = get_settings()
    day = _day_start()
    counted = func.coalesce(func.sum(ProviderAttempt.cost_counted_usd), 0)

    async def total(*where: Any) -> Decimal:
        return Decimal((await db.execute(select(counted).where(*where))).scalar_one())

    out = {"global": (await total(ProviderAttempt.created_at >= day), s.spend_limit_global_daily_usd)}
    if ctx.user_id:
        uid = _uuid(ctx.user_id)
        cap = (await db.execute(select(SpendCap.cap_usd).where(SpendCap.user_id == uid))).scalar_one_or_none()
        if cap is not None:
            out["user_cap"] = (await total(ProviderAttempt.user_id == uid), Decimal(cap))
        else:
            out["user"] = (await total(ProviderAttempt.created_at >= day, ProviderAttempt.user_id == uid),
                           s.spend_limit_user_daily_usd)  # fmt: skip
    if ctx.project_id:
        out["project"] = (
            await total(ProviderAttempt.project_id == _uuid(ctx.project_id)),
            s.spend_limit_project_usd,
        )
    return out


async def check_spend_limits(ctx: CallContext, estimate: Decimal = Decimal(0)) -> None:
    """Refuse when reserving `estimate` would cross any ceiling. Read-only variant."""
    async with transaction() as db:
        _enforce(await spend_totals(db, ctx), estimate)


def _enforce(totals: dict[str, tuple[Decimal, Decimal]], estimate: Decimal) -> None:
    for scope, (total, limit) in totals.items():
        if total + estimate > limit:
            metrics.spend_limit_hits.labels(scope).inc()
            log.warning(
                "spend_limit_reached", scope=scope, total_usd=float(total), estimate_usd=float(estimate)
            )
            raise AppError(ErrorCode.SPEND_LIMIT,
                           "Generation is paused for now. Please try again later." if scope == "global" else
                           "This comic has reached its generation budget. Contact support to continue.")  # fmt: skip


# ------------------------------------------------------------------ operations and attempts
@dataclass(frozen=True)
class Attempt:
    id: uuid.UUID
    operation_id: uuid.UUID
    no: int
    provider: str
    model: str
    operation: str
    status: str
    request_id: str | None
    estimate: Decimal
    price: pricing.Price
    submitted_at: datetime | None = None


def _snap(a: ProviderAttempt) -> Attempt:
    return Attempt(a.id, a.operation_id, a.attempt_no, a.provider, a.model, a.operation, a.status,
                   a.provider_request_id, a.cost_estimate_usd,
                   pricing.Price.from_ref(a.provider, a.model, a.price_ref), a.submitted_at)  # fmt: skip


def _time_budget(att: Attempt) -> float | None:
    """Seconds of provider compute an attempt reserved, for compute-time billing; else None."""
    if att.price.unit != "per_compute_second" or not att.price.usd:
        return None
    return float(att.estimate / att.price.usd)


def _elapsed(att: Attempt) -> float:
    if att.submitted_at is None:
        return 0.0
    return (datetime.now(UTC) - att.submitted_at).total_seconds()


async def _latest(db: AsyncSession, key: str) -> ProviderAttempt | None:
    return (await db.execute(
        select(ProviderAttempt).join(ProviderUsage, ProviderUsage.id == ProviderAttempt.operation_id)
        .where(ProviderUsage.idempotency_key == key).order_by(ProviderAttempt.attempt_no.desc()).limit(1)
    )).scalar_one_or_none()  # fmt: skip


async def latest_attempt(key: str) -> Attempt | None:
    async with transaction() as db:
        row = await _latest(db, key)
        return _snap(row) if row else None


async def _sync_operation(db: AsyncSession, operation_id: uuid.UUID) -> None:
    """Mirror the latest attempt and the summed costs onto the operation row."""
    await db.execute(text("""
        update provider_usage u set
          status = a.status, model = a.model, provider_request_id = a.provider_request_id,
          cost_estimate_usd = a.cost_estimate_usd, cost_basis = a.cost_basis, error_code = a.error_code,
          latency_ms = a.latency_ms, price_ref = a.price_ref,
          cost_counted_usd = t.counted, cost_usd = t.calculated, submissions = t.submissions,
          images = t.images, input_tokens = t.input_tokens, output_tokens = t.output_tokens,
          updated_at = now()
        from (select * from provider_attempts where operation_id = :op order by attempt_no desc limit 1) a,
             (select coalesce(sum(cost_counted_usd), 0) counted,
                     coalesce(sum(coalesce(cost_confirmed_usd, cost_calculated_usd)), 0) calculated,
                     count(submitted_at) submissions, coalesce(sum(images), 0) images,
                     coalesce(sum(input_tokens), 0) input_tokens, coalesce(sum(output_tokens), 0) output_tokens
              from provider_attempts where operation_id = :op) t
        where u.id = :op"""), {"op": operation_id})  # fmt: skip


async def _begin_attempt(ctx: CallContext, *, key: str, provider: str, model: str, operation: str,
                         estimate: Decimal, price: pricing.Price, after: int) -> Attempt:  # fmt: skip
    """Create attempt `after + 1`, holding `estimate` against every ceiling."""
    async with transaction() as db:
        # One lock serialises all reservations, so two calls cannot both see room under a
        # ceiling and jointly exceed it. Each transaction is a few milliseconds long.
        await db.execute(text("select pg_advisory_xact_lock(hashtext('sp-provider-spend'))"))
        op = (await db.execute(select(ProviderUsage).where(ProviderUsage.idempotency_key == key)
                               .with_for_update())).scalar_one_or_none()  # fmt: skip
        if op is None:
            op_id = uuid.uuid4()
            await db.execute(insert(ProviderUsage).values(
                id=op_id, idempotency_key=key, user_id=_uuid(ctx.user_id), project_id=_uuid(ctx.project_id),
                stage=ctx.stage, target_id=ctx.target_id, provider=provider, model=model, operation=operation,
                status="reserved", prompt_version=ctx.prompt_version, submissions=0))  # fmt: skip
        else:
            op_id = op.id
        if ctx.user_id:
            # Deletion blocks new paid work at once, including activities already running when
            # it was requested: no new reservation, so nothing new is submitted or billed.
            deleted = (await db.execute(text("select deleted_at is not null from users where id = :u"),
                                        {"u": _uuid(ctx.user_id)})).scalar_one_or_none()  # fmt: skip
            if deleted:
                raise AppError(ErrorCode.FORBIDDEN, "This account has been deleted.", retryable=False)
        latest = await _latest(db, key)
        if (latest.attempt_no if latest else 0) != after:
            # Someone else started an attempt for this operation since we looked.
            raise AppError(ErrorCode.TIMEOUT, "This illustration is already being worked on.", retryable=True)
        _enforce(await spend_totals(db, ctx), estimate)
        row = ProviderAttempt(
            id=uuid.uuid4(), operation_id=op_id, attempt_no=after + 1, user_id=_uuid(ctx.user_id),
            project_id=_uuid(ctx.project_id), stage=ctx.stage, provider=provider, model=model,
            operation=operation, status="reserved", price_ref=price.ref(), cost_estimate_usd=estimate,
            cost_counted_usd=estimate, cost_basis="reserved", input_tokens=0, output_tokens=0, images=0)  # fmt: skip
        db.add(row)
        await db.flush()
        await _sync_operation(db, op_id)
        return _snap(row)


async def _mark(att: Attempt, *, only_if: tuple[str, ...] | None = None, **fields: Any) -> bool:
    """Update an attempt. With `only_if`, skip (return False) when another process has
    already moved it out of those states, e.g. it settled while being reconciled."""
    async with transaction() as db:
        row = (await db.execute(select(ProviderAttempt).where(ProviderAttempt.id == att.id)
                                .with_for_update())).scalar_one()  # fmt: skip
        if only_if is not None and row.status not in only_if:
            return False
        for k, v in fields.items():
            setattr(row, k, v)
        if fields.get("status") == "submitting":
            row.submitted_at = datetime.now(UTC)
        await db.flush()
        await _sync_operation(db, row.operation_id)
        return True


def _failure_basis() -> tuple[str, bool]:
    """A provider-side job failure: billed or not is not documented for every provider."""
    if get_settings().count_failed_requests_as_billed:
        return "unresolved", True
    return "assumed_unbilled", False


async def _close(att: Attempt, *, status: str, basis: str, calculated: Decimal | None = None,
                 only_if: tuple[str, ...] | None = None, **fields: Any) -> Decimal:  # fmt: skip
    """Settle an attempt. Returns what it now counts against ceilings."""
    if basis == "calculated":
        assert calculated is not None
        counted = calculated
    elif basis == "unresolved":
        counted = att.estimate
    else:  # released / assumed_unbilled
        counted = Decimal(0)
    if not await _mark(att, only_if=only_if, status=status, cost_basis=basis, cost_counted_usd=counted,
                       cost_calculated_usd=calculated,
                       resolved_at=datetime.now(UTC) if basis != "unresolved" else None, **fields):  # fmt: skip
        return Decimal(0)
    if counted:
        metrics.provider_cost.labels(att.provider, att.operation, basis).inc(float(counted))
    return counted


ATTEMPT_REF = "attempt:"


async def record_provider_charges(charges: list[tuple[str, str, Decimal, str]]) -> int:
    """Attach provider-reported charges, e.g. from a billing export:
    (provider, provider_request_id, usd, source). Only these become `provider_confirmed`.

    A synchronous image call that timed out has no request id. Its row can be resolved by the
    attempt id instead, written as "attempt:<uuid>" in the request-id column. The operator reads the
    charge (or USD 0 when the provider shows none) from the provider's usage dashboard for that
    time and model, and records where it came from in `source`."""
    n = 0
    async with transaction() as db:
        for provider, request_id, usd, source in charges:
            if usd < 0:
                raise AppError(ErrorCode.VALIDATION, "A provider charge cannot be negative.")
            if request_id.startswith(ATTEMPT_REF):
                try:
                    att_id = uuid.UUID(request_id[len(ATTEMPT_REF) :].strip())
                except ValueError:
                    raise AppError(ErrorCode.VALIDATION, f"Not an attempt id: {request_id[:60]}") from None
                match = (ProviderAttempt.provider == provider) & (ProviderAttempt.id == att_id)
            else:
                match = (ProviderAttempt.provider == provider) & (
                    ProviderAttempt.provider_request_id == request_id
                )
            rows = (await db.execute(select(ProviderAttempt).where(match).with_for_update())).scalars().all()
            for row in rows:
                row.cost_confirmed_usd, row.cost_counted_usd = usd, usd
                row.cost_basis, row.confirmed_source = "provider_confirmed", source[:120]
                row.resolved_at = datetime.now(UTC)
                n += 1
            await db.flush()
            for op_id in {r.operation_id for r in rows}:
                await _sync_operation(db, op_id)
    return n


def _observe(provider: str, operation: str, outcome: str, latency_ms: int) -> None:
    metrics.provider_calls.labels(provider, operation, outcome).inc()
    metrics.provider_latency.labels(provider, operation).observe(latency_ms / 1000)


# ------------------------------------------------------------------ text
async def structured(
    *, role: TextRole, system: str, user: str, schema: type[T], ctx: CallContext,
    images: tuple[ImageInput, ...] = (), provider: TextProvider | None = None,
) -> T:  # fmt: skip
    """Metered structured LLM call: one operation with one attempt. LLM APIs offer no
    request id to resume by, so a timeout leaves this attempt unresolved (estimate kept).
    `provider` selects another structured provider (Gemini visual identity QA); default Claude."""
    provider = provider or get_providers().text
    model = provider.model_for(role)
    price = pricing.require(provider.name, model)
    # everything sent counts toward input: prompts, image labels and the output schema
    input_bytes = (len(system.encode()) + len(user.encode()) + sum(len((i.label or "").encode()) for i in images)
                   + len(json.dumps(schema.model_json_schema()).encode()))  # fmt: skip
    estimate = pricing.text_estimate(price, input_bytes=input_bytes, images_wh=[_size(i.data) for i in images],
                                     max_tokens=provider.max_tokens)  # fmt: skip
    key = f"text:{uuid.uuid4().hex}"
    operation = f"text_{role}"
    att = await _begin_attempt(ctx, key=key, provider=provider.name, model=model, operation=operation,
                               estimate=estimate, price=price, after=0)  # fmt: skip
    started = time.monotonic()
    meta = {"stage": ctx.stage, "prompt_version": ctx.prompt_version, "attempt": ctx.attempt,
            "project_id": ctx.project_id, "schema": schema.__name__}  # fmt: skip
    async with provider_slot("vision" if images else "text"):
        await _mark(att, status="submitting")
        with generation_span(f"{ctx.stage}.{schema.__name__}", model=model, metadata=meta) as obs:
            try:
                result, usage = await provider.structured(role=role, system=system, user=user, schema=schema,
                                                          ctx=ctx, images=images)  # fmt: skip
            except AppError as exc:
                latency = int((time.monotonic() - started) * 1000)
                outcome = "throttled" if exc.code is ErrorCode.PROVIDER_THROTTLED else "error"
                _observe(provider.name, operation, outcome, latency)
                # A refusal or a 4xx was answered by the provider: the outcome is known, and
                # whether it was billed is policy. Timeouts and connection errors leave it unknown.
                known = exc.code in (ErrorCode.MODERATION_BLOCKED, ErrorCode.PROVIDER_BAD_OUTPUT,
                                     ErrorCode.PROVIDER_THROTTLED) or not exc.retryable  # fmt: skip
                await _close(att, status="failed" if known else "ambiguous",
                             basis=_failure_basis()[0] if known else "unresolved",
                             error_code=exc.code.value, latency_ms=latency)  # fmt: skip
                raise
            except asyncio.CancelledError:
                await asyncio.shield(_close(att, status="cancelled", basis="unresolved"))
                raise
            served = pricing.table().get((provider.name, usage.model))
            actual = pricing.text_cost(served, usage.input_tokens, usage.output_tokens) if served else None
            record_generation(obs, usage={"input": usage.input_tokens, "output": usage.output_tokens},
                              cost_usd=float(actual if actual is not None else estimate))  # fmt: skip
    latency = int((time.monotonic() - started) * 1000)
    if actual is not None and actual > estimate:
        log.warning("provider_estimate_exceeded", operation=operation, estimate_usd=float(estimate),
                    calculated_usd=float(actual))  # fmt: skip
    # The served model may differ (server-side fallback); it is recorded on the operation.
    await _close(att, status="succeeded", basis="calculated" if actual is not None else "unresolved",
                 calculated=actual, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                 latency_ms=latency, provider_request_id=usage.request_id)  # fmt: skip
    if usage.model != model:
        async with transaction() as db:
            await db.execute(text("update provider_usage set model = :m where idempotency_key = :k"),
                             {"m": usage.model, "k": key})  # fmt: skip
    _observe(provider.name, operation, "ok", latency)
    metrics.provider_tokens.labels(provider.name, "input").inc(usage.input_tokens)
    metrics.provider_tokens.labels(provider.name, "output").inc(usage.output_tokens)
    log.info("provider_call", provider=provider.name, model=usage.model, operation=operation, stage=ctx.stage,
             status="succeeded", duration_ms=latency, input_tokens=usage.input_tokens,
             output_tokens=usage.output_tokens, cost_usd=float(actual if actual is not None else estimate),
             cost_basis="calculated" if actual is not None else "unresolved", prompt_version=ctx.prompt_version,
             provider_request_id=usage.request_id)  # fmt: skip
    return result


def _size(data: bytes) -> tuple[int, int]:
    import io

    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as img:
            return int(img.width), int(img.height)
    except Exception:
        return 8000, 8000  # unknown: the largest accepted image, which the cap then bounds


def _pixels(data: bytes) -> int:
    import io

    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as img:
            return int(img.width * img.height)
    except Exception:
        return 4_000_000  # unknown: assume large, which over-reserves rather than under


# ------------------------------------------------------------------ paid async requests
def _final_ambiguous() -> AppError:
    return AppError(ErrorCode.PROVIDER_AMBIGUOUS_FINAL,
                    "We couldn't confirm whether the illustration service received this request, "
                    "so we didn't send it again. You can retry it.")  # fmt: skip


Choose = Callable[[], Awaitable[tuple[str, pricing.Price, Decimal]]]


async def _over_budget(att: Attempt, request_id: str, cancel: Callable[[str, str], Awaitable[None]],
                       fail: Callable[..., AppError]) -> AppError:  # fmt: skip
    """A compute-billed request used the time it reserved: cancel it at the provider and stop.
    The provider may still finish (fal cannot always stop a running job), so the reserved
    amount stays an unresolved potential charge until a provider charge is imported."""
    try:
        await cancel(att.model, request_id)
    except Exception:
        log.warning("provider_cancel_failed", model=att.model, provider_request_id=request_id)
    await _close(att, status="cancelled", basis="unresolved", error_code="time_budget_exceeded",
                 only_if=RESOLVABLE)  # fmt: skip
    metrics.provider_calls.labels(att.provider, att.operation, "time_budget").inc()
    log.warning("provider_time_budget_exceeded", model=att.model, provider_request_id=request_id,
                budget_s=_time_budget(att))  # fmt: skip
    return fail(
        ErrorCode.PROVIDER_ERROR,
        "This step took longer than its cost limit allows.",
        "time_budget",
        retryable=False,
    )


async def _paid_request(
    *, key: str, ctx: CallContext, operation: str, provider_name: str, choose: Choose,
    submit: Callable[[str], Awaitable[str]], fetch: Callable[[str, str, float], Awaitable[ImageResult]],
    cancel: Callable[[str, str], Awaitable[None]], wait_seconds: float,
) -> ImageResult:  # fmt: skip
    """Submit-or-resume one paid request under one operation key (see module docstring).

    `choose()` picks (model, price, estimate) and is called only when a new attempt is
    needed. `submit(model)`, `fetch(model, request_id, wait)` and `cancel(model, request_id)`
    always receive the *persisted* model of the attempt they act on."""
    started = time.monotonic()
    latest = await latest_attempt(key)
    if latest is not None and latest.status == "submitting" and latest.request_id is None:
        # A previous worker died between submitting and recording the id: maybe sent.
        await _close(latest, status="ambiguous", basis="unresolved",
                     error_code=ErrorCode.PROVIDER_AMBIGUOUS_FINAL.value)  # fmt: skip
        metrics.provider_calls.labels(provider_name, operation, "ambiguous").inc()
        raise _final_ambiguous()
    if latest is not None and latest.status == "ambiguous":
        metrics.provider_calls.labels(provider_name, operation, "ambiguous").inc()
        raise _final_ambiguous()
    resumable = (latest is not None and latest.request_id is not None
                 and latest.status in ("started", "cancelled", "succeeded"))  # fmt: skip
    if resumable:
        assert latest is not None
        att = latest
    else:
        model, price, estimate = await choose()
        att = await _begin_attempt(ctx, key=key, provider=provider_name, model=model, operation=operation,
                                   estimate=estimate, price=price, after=latest.no if latest else 0)  # fmt: skip
    refetch = att.status == "succeeded"
    model, price = att.model, att.price

    def fail(code: ErrorCode, message: str, phase: str, *, retryable: bool | None = None) -> AppError:
        return AppError(code, message, retryable=retryable, meta={"model": model, "phase": phase})

    async with provider_slot("image"):
        request_id = att.request_id
        budget = None if refetch else _time_budget(att)
        submitted = time.monotonic() - _elapsed(att)  # resumed: when the provider accepted it
        if request_id is None:
            await _mark(att, status="submitting")
            submitted = time.monotonic()
            try:
                request_id = await submit(model)
            except AppError as exc:
                latency = int((time.monotonic() - started) * 1000)
                if exc.code is ErrorCode.PROVIDER_AMBIGUOUS:
                    await _close(att, status="ambiguous", basis="unresolved",
                                 error_code=ErrorCode.PROVIDER_AMBIGUOUS_FINAL.value, latency_ms=latency)  # fmt: skip
                    _observe(provider_name, operation, "ambiguous", latency)
                    raise _final_ambiguous() from exc
                if exc.code is ErrorCode.PROVIDER_ERROR and exc.retryable:
                    # A 5xx at submission: the queue may or may not have taken it. No id to
                    # resume, so a new attempt is allowed; this one stays a potential charge.
                    await _close(att, status="failed", basis="unresolved", error_code="submit_server_error",
                                 latency_ms=latency)  # fmt: skip
                else:  # 4xx / 429: rejected before queueing
                    await _close(att, status="released", basis="released", error_code=exc.code.value,
                                 latency_ms=latency)  # fmt: skip
                _observe(provider_name, operation, "throttled" if exc.code is ErrorCode.PROVIDER_THROTTLED
                         else "error", latency)  # fmt: skip
                exc.meta.update(model=model, phase="submit")
                raise
            await _mark(att, status="started", provider_request_id=request_id)
        else:
            log.info(
                "provider_request_resumed", operation=operation, model=model, provider_request_id=request_id
            )
            metrics.provider_calls.labels(provider_name, operation, "resumed").inc()
        wait = wait_seconds
        if budget is not None:
            # Compute-time billing: never wait (or let retries resume) past the reserved time.
            remaining = budget - (time.monotonic() - submitted)
            if remaining <= 0:
                raise await _over_budget(att, request_id, cancel, fail)
            wait = min(wait_seconds, remaining)
        try:
            result = await fetch(model, request_id, wait)
        except RequestPending as exc:
            if budget is not None and time.monotonic() - submitted >= budget:
                raise await _over_budget(att, request_id, cancel, fail) from exc
            # Still running at the provider: keep the id; the activity retry resumes it.
            raise fail(ErrorCode.TIMEOUT, "Illustration is taking longer than usual.", "pending") from exc
        except RetrievalFailed as exc:
            # Transport trouble talking to an accepted request: retry retrieval, not generation.
            log.warning("provider_retrieval_failed", operation=operation, model=model,
                        provider_request_id=request_id, reason=exc.reason)  # fmt: skip
            metrics.provider_calls.labels(provider_name, operation, "retrieval_error").inc()
            raise fail(ErrorCode.PROVIDER_ERROR, "We couldn't collect the illustration yet.", "retrieval",
                       retryable=True) from exc  # fmt: skip
        except RequestUnknown as exc:
            if refetch:
                # The stored result was paid for (calculated cost already counted) and is gone.
                await _mark(att, status="succeeded_unused", error_code=exc.reason)
            else:
                # Unknown or expired: not proof that nothing was produced or billed.
                await _close(att, status="lost", basis="unresolved", error_code=exc.reason)
            log.warning("provider_result_lost", operation=operation, model=model,
                        provider_request_id=request_id, reason=exc.reason)  # fmt: skip
            raise fail(ErrorCode.PROVIDER_ERROR, "The illustration service lost this request.", "lost",
                       retryable=True) from exc  # fmt: skip
        except JobFailed as exc:
            if att.status == "cancelled":
                # We cancelled it; the provider's "failed/cancelled" answer only acknowledges that.
                # It may have run (and been billed) first, so the estimate stays a potential charge.
                await _close(
                    att, status="cancel_confirmed", basis="unresolved", error_code="cancel_acknowledged"
                )
            else:
                basis, _ = _failure_basis()
                await _close(att, status="failed", basis=basis, error_code=(exc.error_type or "job_failed")[:40],
                             latency_ms=int((time.monotonic() - started) * 1000))  # fmt: skip
            _observe(provider_name, operation, "error", int((time.monotonic() - started) * 1000))
            if exc.moderation:
                raise fail(ErrorCode.MODERATION_BLOCKED, "The illustration service refused this content.",
                           "job") from exc  # fmt: skip
            raise fail(ErrorCode.PROVIDER_ERROR, "The illustration failed at the provider.", "job") from exc
        except AppError as exc:
            if exc.code in (ErrorCode.MODERATION_BLOCKED, ErrorCode.PROVIDER_BAD_OUTPUT) and not refetch:
                # The provider produced output we cannot use: it was generated, so it is charged.
                await _close(att, status="failed", basis="calculated", calculated=att.estimate,
                             error_code=exc.code.value)  # fmt: skip
            # Anything else keeps the id: the next attempt asks again rather than re-paying.
            exc.meta.update(model=model, phase="retrieval")
            raise
        except asyncio.CancelledError:
            # The workflow was cancelled: stop the provider work, and keep the estimate counted
            # until reconciliation learns whether it finished.
            async def _stop() -> None:
                assert request_id is not None
                try:
                    await cancel(model, request_id)
                except Exception:
                    log.warning("provider_cancel_failed", model=model, provider_request_id=request_id)
                if not refetch:
                    await _close(att, status="cancelled", basis="unresolved")

            await asyncio.shield(_stop())
            raise
    latency = int((time.monotonic() - started) * 1000)
    calculated = _result_cost(price, result.data)
    if not refetch:
        if calculated is None:  # compute-time billing: we cannot calculate it, keep the upper bound
            await _close(att, status="succeeded", basis="unresolved", images=1, latency_ms=latency)
        else:
            await _close(att, status="succeeded", basis="calculated", calculated=calculated, images=1,
                         latency_ms=latency)  # fmt: skip
    _observe(provider_name, operation, "ok", latency)
    metrics.provider_images.labels(provider_name, operation).inc()
    log.info("provider_call", provider=provider_name, model=model, operation=operation, stage=ctx.stage,
             status="succeeded", duration_ms=latency, images=1,
             cost_usd=float(calculated if calculated is not None else att.estimate),
             cost_basis="calculated" if calculated is not None else "unresolved", prompt_version=ctx.prompt_version, provider_request_id=request_id,
             attempt=att.no, refetch=refetch)  # fmt: skip
    return result


def _result_cost(price: pricing.Price, data: bytes) -> Decimal | None:
    """Usage-based cost of a delivered image, or None when the unit cannot be calculated
    from what we observe (compute-time billing)."""
    if price.unit == "per_compute_second":
        return None
    if price.unit == "per_image":
        return pricing.image_cost(price)
    return pricing.image_cost(price, pixels=_pixels(data))


# ------------------------------------------------------------------ synchronous image calls
SYNC_PREFIX = "image_sync_"


def is_sync(att: Attempt) -> bool:
    """Attempts made through a synchronous image API (OpenAI, Gemini): no request to fetch again."""
    return att.operation.startswith(SYNC_PREFIX)


def _queue_cancel(att: Attempt) -> Callable[[str, str], Awaitable[None]] | None:
    providers = get_providers()
    queue = providers.upscale if att.operation == "upscale" else providers.image
    return queue.cancel if queue is not None else None


async def _stored_result(att_id: uuid.UUID) -> tuple[bytes, ProviderAttempt] | None:
    from storypanel.services import repo

    async with transaction() as db:
        row = (await db.execute(select(ProviderAttempt).where(ProviderAttempt.id == att_id))).scalar_one()
        if row.result_asset_id is None:
            return None
        try:
            return await repo.asset_bytes(db, row.result_asset_id), row
        except Exception:
            return None


async def _store_result(ctx: CallContext, data: bytes) -> uuid.UUID:
    """Persist a delivered image before the attempt is marked succeeded (an owner-level asset,
    purged with the account and with the comic; see services/maintenance.py)."""
    from storypanel import storage
    from storypanel.services import repo

    image = storage.validate_image(data)
    last: Exception | None = None
    for delay in (0.0, 0.5, 2.0):
        await asyncio.sleep(delay)
        try:
            async with transaction() as db:
                asset = await repo.save_image(db, owner_id=uuid.UUID(str(ctx.user_id)), project_id=None,
                                              kind="provider_result", image=image)  # fmt: skip
                return asset.id
        except AppError:
            raise  # e.g. the owner was deleted meanwhile: not a storage hiccup
        except Exception as exc:  # storage/database hiccup: try again, the image is in memory
            last = exc
    raise AppError(
        ErrorCode.PROVIDER_ERROR, "The generated image could not be saved.", retryable=True
    ) from last


async def generate_image_sync(req: ImageRequest, ctx: CallContext, *, key: str, choice: str,
                              model: str) -> tuple[ImageResult, Usage]:  # fmt: skip
    """One paid image from a synchronous provider, under one operation key.

    - A stored success is returned again, never re-generated (crash after the provider answered).
    - An attempt left `submitting` (worker died mid-call) or `ambiguous` (timeout, dropped
      connection) is never re-sent automatically: it stays an unresolved potential charge and the
      caller gets PROVIDER_AMBIGUOUS_FINAL; a user retry creates a new operation key.
    - A refusal is recorded and raised; it is never routed to another provider.
    - Settled cost = provider-reported usage x price table; without a complete usage report the
      reservation stays counted as unresolved.
    """
    drawer = get_providers().drawer(choice)
    operation = f"{SYNC_PREFIX}{req.role}"[:30]
    latest = await latest_attempt(key)
    if latest is not None:
        if latest.status in ("succeeded", "succeeded_unused"):
            stored = await _stored_result(latest.id)
            if stored is not None:
                data, row = stored
                metrics.provider_calls.labels(choice, operation, "reused").inc()
                return (ImageResult(data, choice, row.model, row.provider_request_id),
                        Usage(choice, row.model, operation, images=1, cost_usd=row.cost_counted_usd,
                              request_id=row.provider_request_id))  # fmt: skip
        elif latest.status == "reserved":
            await _close(latest, status="released", basis="released", error_code="never_submitted",
                         only_if=("reserved",))  # fmt: skip
        elif latest.status in ("submitting", "started"):
            await _close(latest, status="ambiguous", basis="unresolved",
                         error_code=ErrorCode.PROVIDER_AMBIGUOUS_FINAL.value, only_if=RESOLVABLE)  # fmt: skip
            metrics.provider_calls.labels(choice, operation, "ambiguous").inc()
            raise _final_ambiguous()
        elif latest.status in ("ambiguous", "cancelled"):
            metrics.provider_calls.labels(choice, operation, "ambiguous").inc()
            raise _final_ambiguous()
    price = pricing.require(drawer.name, model)
    n_inputs = min(len(req.references) + (1 if req.image is not None else 0),
                   drawer.capabilities.max_reference_images)  # fmt: skip
    estimate = pricing.image_reservation(price, prompt_bytes=len(req.prompt.encode()), input_images=n_inputs)
    att = await _begin_attempt(ctx, key=key, provider=drawer.name, model=model, operation=operation,
                               estimate=estimate, price=price, after=latest.no if latest else 0)  # fmt: skip
    started = time.monotonic()

    def usage_cost(meta: dict[str, Any]) -> Decimal | None:
        u = meta.get("usage")
        return pricing.multimodal_cost(price, pricing.ImageUsage(**u)) if isinstance(u, dict) else None

    async with provider_slot("image"):
        await _mark(att, status="submitting")
        try:
            res: SyncImageResult = await drawer.generate(req, ctx, model=model)
        except AppError as exc:
            latency = int((time.monotonic() - started) * 1000)
            code = exc.code
            if code is ErrorCode.PROVIDER_AMBIGUOUS:
                await _close(att, status="ambiguous", basis="unresolved", latency_ms=latency,
                             error_code=str(exc.meta.get("kind", "ambiguous"))[:40])  # fmt: skip
                _observe(choice, operation, "ambiguous", latency)
                raise _final_ambiguous() from exc
            if code is ErrorCode.MODERATION_BLOCKED or code is ErrorCode.PROVIDER_BAD_OUTPUT:
                # The provider processed the request: billed per its usage if it reported one.
                cost = usage_cost(exc.meta)
                if cost is not None:
                    await _close(att, status="failed", basis="calculated", calculated=cost, latency_ms=latency,
                                 error_code=code.value, usage_detail=exc.meta.get("usage"))  # fmt: skip
                else:
                    await _close(att, status="failed", basis=_failure_basis()[0], latency_ms=latency,
                                 error_code=code.value)  # fmt: skip
                _observe(choice, operation, "refused" if code is ErrorCode.MODERATION_BLOCKED else "bad_output",
                         latency)  # fmt: skip
                raise
            if code is ErrorCode.PROVIDER_ERROR and exc.retryable:
                # The provider answered with a server error: no image. Billing unknown -> policy.
                await _close(att, status="failed", basis=_failure_basis()[0], latency_ms=latency,
                             error_code="server_error")  # fmt: skip
                _observe(choice, operation, "error", latency)
                raise
            # Rejected before any work (429, auth, invalid request, configuration): not billed.
            await _close(att, status="released", basis="released", latency_ms=latency,
                         error_code=code.value[:40])  # fmt: skip
            _observe(
                choice,
                operation,
                "throttled" if code is ErrorCode.PROVIDER_THROTTLED else "rejected",
                latency,
            )
            raise
        except asyncio.CancelledError:
            # No provider-side cancellation exists: the call may still complete and be billed.
            await asyncio.shield(
                _close(att, status="cancelled", basis="unresolved", error_code="cancelled_in_flight")
            )
            raise
        except Exception as exc:
            latency = int((time.monotonic() - started) * 1000)
            await _close(att, status="ambiguous", basis="unresolved", latency_ms=latency,
                         error_code=f"unexpected_{type(exc).__name__}"[:40])  # fmt: skip
            _observe(choice, operation, "ambiguous", latency)
            raise _final_ambiguous() from exc
    latency = int((time.monotonic() - started) * 1000)
    cost = pricing.multimodal_cost(price, res.usage)
    u = res.usage
    fields: dict[str, Any] = {
        "images": 1, "latency_ms": latency, "provider_request_id": res.request_id or None,
        "usage_detail": u.as_dict(),
        "input_tokens": (u.input_text_tokens or 0) + (u.input_image_tokens or 0),
        "output_tokens": (u.output_text_tokens or 0) + (u.output_image_tokens or 0),
    }  # fmt: skip
    try:
        fields["result_asset_id"] = await _store_result(ctx, res.data)
    except AppError:
        # Paid for, but not kept: count it, and let the caller retry with a new attempt.
        await _close(att, status="succeeded_unused", basis="calculated" if cost is not None else "unresolved",
                     calculated=cost, error_code="result_not_stored", **fields)  # fmt: skip
        raise
    await _close(att, status="succeeded", basis="calculated" if cost is not None else "unresolved",
                 calculated=cost, **fields)  # fmt: skip
    if cost is not None and cost > estimate:
        log.warning("provider_estimate_exceeded", operation=operation, estimate_usd=float(estimate),
                    calculated_usd=float(cost))  # fmt: skip
    _observe(choice, operation, "ok", latency)
    metrics.provider_images.labels(choice, operation).inc()
    log.info("provider_call", provider=choice, model=model, operation=operation, stage=ctx.stage, status="succeeded",
             duration_ms=latency, images=1, cost_usd=float(cost if cost is not None else estimate),
             cost_basis="calculated" if cost is not None else "unresolved", prompt_version=ctx.prompt_version,
             provider_request_id=res.request_id, attempt=att.no, input_images=n_inputs)  # fmt: skip
    return (ImageResult(res.data, choice, model, res.request_id),
            Usage(choice, model, operation, images=1, cost_usd=cost if cost is not None else estimate,
                  request_id=res.request_id))  # fmt: skip


async def draw(req: ImageRequest, ctx: CallContext, *, key: str, choice: str | None,
               model: str | None) -> tuple[ImageResult, Usage]:  # fmt: skip
    """Pipeline entry point: the comic's own provider, never another one. `choice` None is a comic
    created before per-comic providers existed (drawn with fal)."""
    if choice in (None, "fal"):
        if get_providers().image is None:
            raise AppError(ErrorCode.INVALID_STATE,
                           "This comic was drawn with fal, which is disabled here. Start a new comic to continue.",
                           retryable=False, meta={"provider": "fal"})  # fmt: skip
        return await generate_image(req, ctx, key=key)
    assert model is not None
    return await generate_image_sync(req, ctx, key=key, choice=choice, model=model)


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

    The breaker only picks the model of a *new* attempt. A resumed request is fetched,
    cancelled and priced through the endpoint and price recorded when it was accepted.
    Content refusals never trip the breaker.
    """
    provider = get_providers().image
    if provider is None:
        raise AppError(ErrorCode.INVALID_STATE, "fal image generation is disabled (SP_FAL_ENABLED=false).",
                       retryable=False, meta={"provider": "fal"})  # fmt: skip
    primary = provider.model_for(req)
    fallback = provider.fallback_for(req)

    async def choose() -> tuple[str, pricing.Price, Decimal]:
        model = fallback if fallback and await _breaker_open(primary) else primary
        if model != primary:
            log.warning("provider_fallback_used", primary=primary, fallback=model, stage=ctx.stage)
        price = pricing.require(provider.name, model)
        return model, price, pricing.image_cost(price, pixels=_image_pixels(req, model))

    try:
        result = await _paid_request(
            key=key, ctx=ctx, operation=f"image_{req.role}", provider_name=provider.name, choose=choose,
            submit=lambda model: provider.submit(req, ctx, model=model),
            fetch=lambda model, rid, wait: provider.fetch(model, rid, wait_seconds=wait),
            cancel=provider.cancel, wait_seconds=wait_seconds,
        )  # fmt: skip
    except AppError as exc:
        if (exc.meta.get("model") == primary and exc.meta.get("phase") in ("submit", "job")
                and exc.code in (ErrorCode.PROVIDER_ERROR, ErrorCode.PROVIDER_THROTTLED)):  # fmt: skip
            await _breaker_failure(primary)
        raise
    att = await latest_attempt(key)
    assert att is not None
    cost = _result_cost(att.price, result.data)
    cost = att.estimate if cost is None else cost
    return result, Usage(result.provider, att.model, f"image_{req.role}", images=1, cost_usd=cost,
                         request_id=result.request_id)  # fmt: skip


async def upscale_image(
    image: bytes, ctx: CallContext, *, key: str, wait_seconds: float = 150.0
) -> ImageResult:
    s, provider = get_settings(), get_providers().upscale
    if provider is None:
        raise AppError(ErrorCode.INVALID_STATE, "External upscaling is disabled (SP_EXPORT_UPSCALE=local).",
                       retryable=False, meta={"provider": "fal"})  # fmt: skip

    async def choose() -> tuple[str, pricing.Price, Decimal]:
        price = pricing.require(provider.name, provider.model)
        return provider.model, price, pricing.image_cost(price, pixels=_pixels(image) * s.upscale_factor**2,
                                                         seconds=wait_seconds)  # fmt: skip

    return await _paid_request(
        key=key, ctx=ctx, operation="upscale", provider_name=provider.name, choose=choose,
        submit=lambda model: provider.submit(image, s.upscale_factor, ctx, model=model),
        fetch=lambda model, rid, wait: provider.fetch(model, rid, wait_seconds=wait), cancel=provider.cancel,
        wait_seconds=wait_seconds,
    )  # fmt: skip


async def embed_images(images: list[bytes], ctx: CallContext) -> list[list[float]]:
    provider = get_providers().embedding
    if provider is None:
        raise AppError(ErrorCode.INVALID_STATE, "Embedding identity QA is disabled (SP_VOYAGE_ENABLED=false).",
                       retryable=False, meta={"provider": "voyage"})  # fmt: skip
    price = pricing.require(provider.name, provider.model)
    pixels = sum(_pixels(i) for i in images)
    cost = pricing.image_cost(price, images=1, pixels=pixels)
    att = await _begin_attempt(ctx, key=f"embed:{uuid.uuid4().hex}", provider=provider.name,
                               model=provider.model, operation="embed", estimate=cost, price=price, after=0)  # fmt: skip
    started = time.monotonic()
    async with provider_slot("vision"):
        await _mark(att, status="submitting")
        try:
            vectors = await provider.embed_images(images, ctx)
        except AppError as exc:
            latency = int((time.monotonic() - started) * 1000)
            _observe(provider.name, "embed", "error", latency)
            await _close(att, status="ambiguous" if exc.retryable else "failed",
                         basis="unresolved" if exc.retryable else _failure_basis()[0],
                         error_code=exc.code.value, latency_ms=latency)  # fmt: skip
            raise
    latency = int((time.monotonic() - started) * 1000)
    await _close(att, status="succeeded", basis="calculated", calculated=cost, images=len(images),
                 latency_ms=latency)  # fmt: skip
    _observe(provider.name, "embed", "ok", latency)
    return vectors


async def stop_attempts(attempts: list[Attempt], *, reason: str) -> None:
    """Stop open attempts now (account deletion): cancel at the provider through each
    attempt's own endpoint; never-sent reservations are released, everything else stays
    counted as an unresolved potential charge for reconciliation."""
    for att in attempts:
        if att.status == "reserved":
            await _close(
                att, status="released", basis="released", error_code=reason[:40], only_if=("reserved",)
            )
            continue
        if is_sync(att):
            # A synchronous call in flight cannot be cancelled at the provider: it may still finish
            # and be billed, so it stays an unresolved potential charge.
            await _close(
                att, status="ambiguous", basis="unresolved", error_code=reason[:40], only_if=RESOLVABLE
            )
            continue
        cancel = _queue_cancel(att)
        if att.request_id is not None and cancel is not None:
            try:
                await cancel(att.model, att.request_id)
            except Exception:
                log.warning("provider_cancel_failed", model=att.model, provider_request_id=att.request_id)
        await _close(att, status="cancelled" if att.request_id else "ambiguous", basis="unresolved",
                     error_code=reason[:40], only_if=RESOLVABLE)  # fmt: skip


# ------------------------------------------------------------------ reconciliation
async def reconcile_open_requests(older_than: timedelta = timedelta(minutes=30)) -> dict[str, int]:
    """Durably resolve attempts whose worker went away or which were cancelled.

    Asks the provider about every stored request id through the attempt's own endpoint.
    Attempts never sent are released; attempts that may have been sent without an id stay
    unresolved (ambiguous). Lost/unknown ids are *not* released: they stay counted."""
    cutoff = datetime.now(UTC) - older_than
    outcome = {"released": 0, "ambiguous": 0, "succeeded_unused": 0, "failed": 0, "pending": 0, "lost": 0,
               "retrieval_error": 0, "cancelled_over_budget": 0, "provider_disabled": 0}  # fmt: skip
    async with transaction() as db:
        rows = [_snap(r) for r in (await db.execute(select(ProviderAttempt).where(
            ProviderAttempt.status.in_(RESOLVABLE), ProviderAttempt.updated_at < cutoff)
            .order_by(ProviderAttempt.updated_at).limit(200))).scalars()]  # fmt: skip
    providers = get_providers()
    for att in rows:
        if att.status == "reserved":
            await _close(
                att, status="released", basis="released", error_code="never_submitted", only_if=("reserved",)
            )
            outcome["released"] += 1
            continue
        if att.request_id is None or is_sync(att):
            # Never sent with an id, or a synchronous call whose worker went away mid-call: there is
            # nothing to ask the provider, so it stays an unresolved potential charge.
            await _close(att, status="ambiguous", basis="unresolved", only_if=RESOLVABLE,
                         error_code=ErrorCode.PROVIDER_AMBIGUOUS_FINAL.value)  # fmt: skip
            outcome["ambiguous"] += 1
            continue
        queue = providers.upscale if att.operation == "upscale" else providers.image
        if queue is None:
            # fal attempts from before fal was disabled: no client exists, so they are not queried;
            # they stay unresolved until fal is re-enabled or a provider charge is imported. Touching
            # updated_at moves them to the back of the sweep so they cannot starve other attempts.
            await _mark(att, error_code="provider_disabled", only_if=RESOLVABLE)
            outcome["provider_disabled"] += 1
            continue
        try:
            result = await queue.fetch(att.model, att.request_id, wait_seconds=0)
            calculated = _result_cost(att.price, result.data)
            # Produced (and presumably billed), but nobody stored the result.
            await _close(att, status="succeeded_unused", basis="calculated" if calculated is not None else "unresolved",
                         calculated=calculated, images=1, only_if=RESOLVABLE)  # fmt: skip
            outcome["succeeded_unused"] += 1
        except RequestPending:
            budget = _time_budget(att)
            if budget is not None and att.status == "started" and _elapsed(att) >= budget:
                # Abandoned compute-billed job past its reserved time: stop paying for it.
                try:
                    await queue.cancel(att.model, att.request_id)
                except Exception:
                    log.warning("provider_cancel_failed", model=att.model, provider_request_id=att.request_id)
                await _close(att, status="cancelled", basis="unresolved", error_code="time_budget_exceeded",
                             only_if=("started",))  # fmt: skip
                outcome["cancelled_over_budget"] += 1
                continue
            if att.status == "cancelled":
                try:
                    await queue.cancel(att.model, att.request_id)
                except Exception:
                    pass
            await _mark(
                att, error_code="still_pending", only_if=RESOLVABLE
            )  # touches updated_at: re-checked next sweep
            outcome["pending"] += 1
        except JobFailed as exc:
            if att.status == "cancelled":  # acknowledgement of our cancel, not proof of no charge
                await _close(att, status="cancel_confirmed", basis="unresolved", only_if=("cancelled",),
                             error_code="cancel_acknowledged")  # fmt: skip
            else:
                await _close(att, status="failed", basis=_failure_basis()[0], only_if=RESOLVABLE,
                             error_code=(exc.error_type or "job_failed")[:40])  # fmt: skip
            outcome["failed"] += 1
        except RequestUnknown as exc:
            await _close(att, status="lost", basis="unresolved", error_code=exc.reason, only_if=RESOLVABLE)
            outcome["lost"] += 1
        except (RetrievalFailed, AppError):
            outcome["retrieval_error"] += 1  # transient: next sweep
    async with transaction() as db:
        by_provider = (await db.execute(
            select(ProviderAttempt.provider, func.coalesce(func.sum(ProviderAttempt.cost_counted_usd), 0))
            .where(ProviderAttempt.cost_basis.in_(UNRESOLVED_BASES)).group_by(ProviderAttempt.provider))).all()  # fmt: skip
    metrics.provider_unreconciled_usd.set(float(sum((Decimal(v) for _, v in by_provider), Decimal(0))))
    for prov, v in by_provider:
        metrics.provider_unresolved_usd.labels(prov).set(float(v))
    return outcome
