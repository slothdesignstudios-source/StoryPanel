"""Paid-request recovery and per-attempt accounting (review findings 1-3).

Every test asserts provider-side effects (submissions, fetch/cancel endpoints) as well as
database rows: one row per logical operation is not evidence of one billed request.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from storypanel.db.models import ProviderAttempt, User
from storypanel.db.session import transaction
from storypanel.errors import AppError, ErrorCode
from storypanel.providers import fake, metering
from storypanel.providers.base import CallContext, ImageRequest, JobFailed, RequestUnknown, RetrievalFailed
from storypanel.services.events import redis_client


async def _ctx(stage: str = "panel_generate") -> CallContext:
    async with transaction() as db:
        u = User(id=uuid.uuid4(), auth_subject=f"t:{uuid.uuid4().hex}")
        db.add(u)
    return CallContext(stage, str(u.id), str(uuid.uuid4()))


async def _open_breaker(model: str) -> None:
    await redis_client().set(f"sp:breaker:{model}", "1", ex=120)


async def _close_breaker(model: str) -> None:
    await redis_client().delete(f"sp:breaker:{model}", f"sp:breakerfail:{model}")


# ------------------------------------------------------------------ finding 1
async def test_primary_request_resumes_on_primary_after_breaker_opens() -> None:
    ctx = await _ctx()
    key = f"f1a:{ctx.project_id}"
    fake.control.pending_fetches = 1  # accepted by the primary, still running
    with pytest.raises(AppError) as e:
        await metering.generate_image(ImageRequest("p", "4:3"), ctx, key=key)
    assert e.value.code is ErrorCode.TIMEOUT
    await _open_breaker("fake-image")  # primary starts failing before the activity retry
    result, usage = await metering.generate_image(ImageRequest("p", "4:3"), ctx, key=key)
    assert fake.control.models_used == ["fake-image"]  # one submission, never re-sent
    assert {m for m, _ in fake.control.fetches} == {"fake-image"}  # never asked the fallback
    assert result.model == "fake-image" and usage.cost_usd == Decimal("0.04")  # primary's price


async def test_fallback_request_resumes_on_fallback_after_breaker_closes() -> None:
    ctx = await _ctx()
    key = f"f1b:{ctx.project_id}"
    await _open_breaker("fake-image")
    fake.control.pending_fetches = 1
    with pytest.raises(AppError):
        await metering.generate_image(ImageRequest("p", "4:3"), ctx, key=key)
    await _close_breaker("fake-image")  # primary recovers before the retry
    result, usage = await metering.generate_image(ImageRequest("p", "4:3"), ctx, key=key)
    assert fake.control.models_used == ["fake-fallback"]
    assert {m for m, _ in fake.control.fetches} == {"fake-fallback"}
    assert result.model == "fake-fallback" and usage.cost_usd == Decimal("0.035")  # fallback's price


async def test_cancellation_goes_to_the_endpoint_that_accepted_the_request() -> None:
    ctx = await _ctx()
    key = f"f1c:{ctx.project_id}"
    fake.control.pending_fetches = 1
    with pytest.raises(AppError):
        await metering.generate_image(ImageRequest("p", "4:3"), ctx, key=key)
    await _open_breaker("fake-image")
    fake.control.hang_fetches = 1
    task = asyncio.create_task(metering.generate_image(ImageRequest("p", "4:3"), ctx, key=key))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if len(fake.control.fetches) == 2:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.1)
    assert [m for m, _ in fake.control.cancels] == ["fake-image"]
    assert fake.control.models_used == ["fake-image"]


async def _attempts(pid: str | None) -> list[tuple[int, str, Decimal, str]]:
    async with transaction() as db:
        return [tuple(r) for r in (await db.execute(  # type: ignore[misc]
            select(ProviderAttempt.attempt_no, ProviderAttempt.status, ProviderAttempt.cost_counted_usd,
                   ProviderAttempt.cost_basis)
            .where(ProviderAttempt.project_id == uuid.UUID(pid)).order_by(ProviderAttempt.attempt_no))).all()]  # fmt: skip


async def _counted(pid: str | None) -> Decimal:
    return sum((a[2] for a in await _attempts(pid)), Decimal(0))


def _req() -> ImageRequest:
    return ImageRequest("p", "4:3")


# ------------------------------------------------------------------ finding 2
@pytest.mark.parametrize("fault", [
    RetrievalFailed("r", "status_http_503"), RetrievalFailed("r", "result_http_502"),
    RetrievalFailed("r", "download_http_503"), RetrievalFailed("r", "download_RemoteProtocolError"),
    RetrievalFailed("r", "ReadTimeout"), AppError(ErrorCode.PROVIDER_ERROR, "unclassified transport error"),
])  # fmt: skip
async def test_transient_retrieval_failure_retries_retrieval_not_generation(fault: Exception) -> None:
    ctx = await _ctx()
    key = f"f2:{ctx.project_id}"
    fake.control.errors["image:fetch"] = [fault, fault]
    for _ in range(2):
        with pytest.raises(AppError) as e:
            await metering.generate_image(_req(), ctx, key=key)
        assert e.value.retryable
    result, _ = await metering.generate_image(_req(), ctx, key=key)
    assert len(fake.control.models_used) == 1  # exactly one paid submission at the provider
    assert len({rid for _, rid in fake.control.fetches}) == 1 and len(fake.control.fetches) == 3
    assert await _attempts(ctx.project_id) == [(1, "succeeded", Decimal("0.04"), "calculated")]


async def test_polling_timeout_keeps_the_request_and_resumes() -> None:
    ctx = await _ctx()
    key = f"f2p:{ctx.project_id}"
    fake.control.pending_fetches = 3
    for _ in range(3):
        with pytest.raises(AppError) as e:
            await metering.generate_image(_req(), ctx, key=key)
        assert e.value.code is ErrorCode.TIMEOUT
    await metering.generate_image(_req(), ctx, key=key)
    assert len(fake.control.models_used) == 1
    assert await _attempts(ctx.project_id) == [(1, "succeeded", Decimal("0.04"), "calculated")]


async def test_expired_result_stays_counted_and_a_new_attempt_is_separate() -> None:
    ctx = await _ctx()
    key = f"f2e:{ctx.project_id}"
    fake.control.errors["image:fetch"] = [RequestUnknown("r", "result_expired")]
    with pytest.raises(AppError) as e:
        await metering.generate_image(_req(), ctx, key=key)
    assert e.value.code is ErrorCode.PROVIDER_ERROR and e.value.retryable
    await metering.generate_image(_req(), ctx, key=key)
    assert len(fake.control.models_used) == 2  # the lost one, and its replacement
    assert await _attempts(ctx.project_id) == [
        (1, "lost", Decimal("0.04"), "unresolved"),  # a 404/expiry is not proof of no charge
        (2, "succeeded", Decimal("0.04"), "calculated"),
    ]


async def test_worker_crash_during_fetch_resumes_the_same_request() -> None:
    ctx = await _ctx()
    key = f"f2c:{ctx.project_id}"
    fake.control.crashes["image:fetch"] = 1  # the process dies while waiting
    with pytest.raises(RuntimeError):
        await metering.generate_image(_req(), ctx, key=key)
    assert (await _attempts(ctx.project_id))[0][1] == "started"
    await metering.generate_image(_req(), ctx, key=key)
    assert len(fake.control.models_used) == 1
    assert await _attempts(ctx.project_id) == [(1, "succeeded", Decimal("0.04"), "calculated")]


async def test_crash_between_acceptance_and_recording_the_id_is_never_resubmitted() -> None:
    ctx = await _ctx()
    key = f"f2a:{ctx.project_id}"
    real_mark = metering._mark

    async def die_on_started(att: metering.Attempt, **fields: object) -> None:
        if fields.get("status") == "started":
            raise RuntimeError("worker killed after the provider accepted the request")
        await real_mark(att, **fields)  # type: ignore[arg-type]

    metering._mark = die_on_started  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError):
            await metering.generate_image(_req(), ctx, key=key)
    finally:
        metering._mark = real_mark  # type: ignore[assignment]
    for _ in range(2):
        with pytest.raises(AppError) as e:
            await metering.generate_image(_req(), ctx, key=key)
        assert e.value.code is ErrorCode.PROVIDER_AMBIGUOUS_FINAL
    assert len(fake.control.models_used) == 1
    assert await _attempts(ctx.project_id) == [(1, "ambiguous", Decimal("0.04"), "unresolved")]


async def test_cancel_after_completion_is_resolved_by_reconciliation() -> None:
    ctx = await _ctx()
    key = f"f2x:{ctx.project_id}"
    fake.control.hang_fetches = 1
    fake.control.errors["image:cancel"] = [AppError(ErrorCode.PROVIDER_ERROR, "request already completed")]
    task = asyncio.create_task(metering.generate_image(_req(), ctx, key=key))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if fake.control.fetches:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.1)
    assert await _attempts(ctx.project_id) == [(1, "cancelled", Decimal("0.04"), "unresolved")]
    async with transaction() as db:
        await db.execute(text("update provider_attempts set updated_at = now() - interval '1 hour' "
                              "where project_id = :p"), {"p": ctx.project_id})  # fmt: skip
    out = await metering.reconcile_open_requests()
    assert out["succeeded_unused"] == 1
    assert await _attempts(ctx.project_id) == [(1, "succeeded_unused", Decimal("0.04"), "calculated")]
    assert len(fake.control.models_used) == 1


async def test_retry_after_cancellation_collects_the_finished_result_without_paying_again() -> None:
    ctx = await _ctx()
    key = f"f2y:{ctx.project_id}"
    fake.control.hang_fetches = 1
    fake.control.errors["image:cancel"] = [AppError(ErrorCode.PROVIDER_ERROR, "request already completed")]
    task = asyncio.create_task(metering.generate_image(_req(), ctx, key=key))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if fake.control.fetches:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.1)
    await metering.generate_image(_req(), ctx, key=key)  # the user retries the panel
    assert len(fake.control.models_used) == 1
    assert await _attempts(ctx.project_id) == [(1, "succeeded", Decimal("0.04"), "calculated")]


# ------------------------------------------------------------------ finding 3
async def test_billed_failures_then_success_keep_every_attempt_counted() -> None:
    from storypanel.config import get_settings

    assert get_settings().count_failed_requests_as_billed
    ctx = await _ctx()
    key = f"f3:{ctx.project_id}"
    fake.control.errors["image:fetch"] = [JobFailed("r", "internal"), JobFailed("r", "internal")]
    for _ in range(2):
        with pytest.raises(AppError):
            await metering.generate_image(_req(), ctx, key=key)
    await metering.generate_image(_req(), ctx, key=key)
    assert len(fake.control.models_used) == 3
    assert await _attempts(ctx.project_id) == [
        (1, "failed", Decimal("0.04"), "unresolved"), (2, "failed", Decimal("0.04"), "unresolved"),
        (3, "succeeded", Decimal("0.04"), "calculated")]  # fmt: skip
    async with transaction() as db:
        op = (await db.execute(text("select cost_counted_usd, submissions from provider_usage "
                                    "where idempotency_key = :k"), {"k": key})).one()  # fmt: skip
    assert op == (Decimal("0.12"), 3)


async def test_operator_policy_can_treat_failures_as_unbilled(monkeypatch: pytest.MonkeyPatch) -> None:
    from storypanel.config import get_settings

    monkeypatch.setattr(get_settings(), "count_failed_requests_as_billed", False)
    ctx = await _ctx()
    fake.control.errors["image:fetch"] = [JobFailed("r", "internal")]
    with pytest.raises(AppError):
        await metering.generate_image(_req(), ctx, key=f"f3u:{ctx.project_id}")
    await metering.generate_image(_req(), ctx, key=f"f3u:{ctx.project_id}")
    assert await _attempts(ctx.project_id) == [
        (1, "failed", Decimal("0"), "assumed_unbilled"), (2, "succeeded", Decimal("0.04"), "calculated")]  # fmt: skip


async def test_failed_attempts_count_toward_the_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    from storypanel.config import get_settings

    monkeypatch.setattr(get_settings(), "spend_limit_project_usd", Decimal("0.10"))
    ctx = await _ctx()
    key = f"f3c:{ctx.project_id}"
    fake.control.errors["image:fetch"] = [JobFailed("r", "internal"), JobFailed("r", "internal")]
    for _ in range(2):
        with pytest.raises(AppError):
            await metering.generate_image(_req(), ctx, key=key)
    with pytest.raises(AppError) as e:  # 0.08 already counted; another 0.04 would cross 0.10
        await metering.generate_image(_req(), ctx, key=key)
    assert e.value.code is ErrorCode.SPEND_LIMIT
    assert len(fake.control.models_used) == 2


async def test_concurrent_retries_near_the_ceiling_never_exceed_it(monkeypatch: pytest.MonkeyPatch) -> None:
    from storypanel.config import get_settings

    monkeypatch.setattr(get_settings(), "spend_limit_project_usd", Decimal("0.30"))
    ctx = await _ctx()
    fake.control.errors["image:fetch"] = [JobFailed("r", "internal") for _ in range(6)]

    async def op(n: int) -> None:
        for _ in range(3):
            try:
                await metering.generate_image(_req(), ctx, key=f"f3r:{ctx.project_id}:{n}")
                return
            except AppError as exc:
                if exc.code is ErrorCode.SPEND_LIMIT:
                    return

    await asyncio.gather(*[op(n) for n in range(8)])
    assert await _counted(ctx.project_id) <= Decimal("0.30")
    assert len(fake.control.models_used) * Decimal("0.04") == await _counted(ctx.project_id)


async def test_attempt_records_are_immutable_in_the_database() -> None:
    ctx = await _ctx()
    await metering.generate_image(_req(), ctx, key=f"f3i:{ctx.project_id}")
    for stmt in ("update provider_attempts set cost_counted_usd = 0 where project_id = :p",
                 "update provider_attempts set model = 'other' where project_id = :p",
                 "update provider_attempts set provider_request_id = 'x' where project_id = :p",
                 "delete from provider_attempts where project_id = :p"):  # fmt: skip
        with pytest.raises(Exception, match="immutable|permanent"):
            async with transaction() as db:
                await db.execute(text(stmt), {"p": ctx.project_id})


async def test_only_provider_reported_charges_are_labelled_confirmed() -> None:
    ctx = await _ctx()
    result, _ = await metering.generate_image(_req(), ctx, key=f"f3p:{ctx.project_id}")
    assert (await _attempts(ctx.project_id))[0][3] == "calculated"
    n = await metering.record_provider_charges([("fake", result.request_id or "", Decimal("0.039"),
                                                 "billing export 2026-11")])  # fmt: skip
    assert n == 1
    assert await _attempts(ctx.project_id) == [(1, "succeeded", Decimal("0.039"), "provider_confirmed")]


@pytest.mark.usefixtures("fal_voyage")
async def test_upscale_resume_uses_the_persisted_model(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    from PIL import Image

    from storypanel.providers import registry

    buf = io.BytesIO()
    Image.new("RGB", (32, 24), "white").save(buf, format="PNG")
    image = buf.getvalue()
    ctx = await _ctx("export")
    key = f"f1u:{ctx.project_id}"
    fake.control.errors["upscale:fetch"] = [RetrievalFailed("r", "status_http_503")]
    with pytest.raises(AppError):
        await metering.upscale_image(image, ctx, key=key)
    up = registry.get_providers().upscale
    monkeypatch.setattr(up, "model", "fake-upscale-v2")  # configuration changed between attempts
    await metering.upscale_image(image, ctx, key=key)
    assert {m for m, _ in fake.control.fetches} == {"fake-upscale"}
    assert fake.control.models_used == ["fake-upscale"]


async def test_run_budget_is_a_lifetime_cap_unaffected_by_day_boundaries() -> None:
    """Evaluation runs: a SpendCap on the run's own user caps all its spend, including spend
    recorded 'yesterday', instead of the resetting daily per-user ceiling."""
    from storypanel.db.models import SpendCap

    ctx = await _ctx()
    async with transaction() as db:
        db.add(SpendCap(user_id=uuid.UUID(ctx.user_id), cap_usd=Decimal("0.10"), reason="test"))  # type: ignore[arg-type]
    await metering.generate_image(_req(), ctx, key=f"cap1:{ctx.project_id}")
    async with transaction() as db:  # as if that spend happened before midnight
        await db.execute(text("alter table provider_attempts disable trigger provider_attempts_guard"))
        await db.execute(text("update provider_attempts set created_at = now() - interval '2 days'"))
        await db.execute(text("alter table provider_attempts enable trigger provider_attempts_guard"))
    await metering.generate_image(_req(), CallContext("panel_generate", ctx.user_id, str(uuid.uuid4())),
                                  key=f"cap2:{ctx.project_id}")  # fmt: skip
    with pytest.raises(AppError) as e:  # 0.08 spent across days; 0.04 more would cross 0.10
        await metering.generate_image(_req(), CallContext("panel_generate", ctx.user_id, str(uuid.uuid4())),
                                      key=f"cap3:{ctx.project_id}")  # fmt: skip
    assert e.value.code is ErrorCode.SPEND_LIMIT
    # another account's spend does not count against the run
    other = await _ctx()
    await metering.generate_image(_req(), other, key=f"cap4:{other.project_id}")


async def test_durable_fake_queue_survives_a_new_provider_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """The crash drill relies on this: a job submitted by one worker process is fetched by another."""
    from storypanel.providers.fake import FakeImageProvider

    monkeypatch.setenv("SP_FAKE_DURABLE_QUEUE", "1")
    ctx = await _ctx()
    rid = await FakeImageProvider().submit(_req(), ctx, model="fake-image")
    result = await FakeImageProvider().fetch("fake-image", rid, wait_seconds=0)  # a "new process"
    assert result.data and result.request_id == rid
