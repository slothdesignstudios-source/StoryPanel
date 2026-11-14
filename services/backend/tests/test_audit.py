"""Regression probes from the post-remediation audit (2026-11-12). Each test encodes a
required behaviour stated in the audit brief; see docs/remediation-tracker.md, "Audit"."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text

from storypanel.config import get_settings
from storypanel.db.models import ProviderAttempt, User
from storypanel.db.session import transaction
from storypanel.errors import AppError, ErrorCode
from storypanel.providers import fake, metering
from storypanel.providers.base import CallContext, ImageRequest, RequestUnknown


async def _ctx(stage: str = "panel_generate") -> CallContext:
    async with transaction() as db:
        u = User(id=uuid.uuid4(), auth_subject=f"t:{uuid.uuid4().hex}")
        db.add(u)
    return CallContext(stage, str(u.id), str(uuid.uuid4()))


async def _attempts(pid: str | None) -> list[tuple[int, str, Decimal, str]]:
    async with transaction() as db:
        return [tuple(r) for r in (await db.execute(  # type: ignore[misc]
            select(ProviderAttempt.attempt_no, ProviderAttempt.status, ProviderAttempt.cost_counted_usd,
                   ProviderAttempt.cost_basis)
            .where(ProviderAttempt.project_id == uuid.UUID(pid)).order_by(ProviderAttempt.attempt_no))).all()]  # fmt: skip


def _req() -> ImageRequest:
    return ImageRequest("p", "4:3")


# ------------------------------------------------------------------ 1. lost results
@pytest.mark.parametrize("after_success", [False, True])
async def test_lost_result_keeps_its_cost_and_the_replacement_needs_its_own_reservation(
    monkeypatch: pytest.MonkeyPatch, after_success: bool
) -> None:
    from storypanel.providers import registry

    monkeypatch.setattr(get_settings(), "spend_limit_project_usd", Decimal("0.06"))
    ctx = await _ctx()
    key = f"lost:{ctx.project_id}"
    if after_success:  # produced and stored as succeeded, then the worker died before saving the image
        await metering.generate_image(_req(), ctx, key=key)
        registry.get_providers().image._jobs.clear()  # type: ignore[attr-defined]
    else:
        fake.control.errors["image:fetch"] = [RequestUnknown("r", "result_expired")]
    with pytest.raises(AppError):
        await metering.generate_image(_req(), ctx, key=key)
    first = (await _attempts(ctx.project_id))[0]
    assert first[2] == Decimal("0.04") and first[1] in ("lost", "succeeded_unused")  # cost not erased
    # A replacement is a new attempt with a new reservation: 0.04 counted + 0.04 > 0.06 ceiling.
    with pytest.raises(AppError) as e:
        await metering.generate_image(_req(), ctx, key=key)
    assert e.value.code is ErrorCode.SPEND_LIMIT
    assert len(fake.control.models_used) == 1  # no replacement was submitted
    monkeypatch.setattr(get_settings(), "spend_limit_project_usd", Decimal("1"))
    await metering.generate_image(_req(), ctx, key=key)
    rows = await _attempts(ctx.project_id)
    assert [r[0] for r in rows] == [1, 2] and rows[0][2] == Decimal("0.04") and rows[1][1] == "succeeded"
    assert sum(r[2] for r in rows) == Decimal("0.08")


# ------------------------------------------------------------------ 2. cancellation acknowledgement
@pytest.mark.parametrize("count_failed", [True, False])
async def test_cancellation_acknowledgement_is_not_proof_of_no_charge(
    monkeypatch: pytest.MonkeyPatch, count_failed: bool
) -> None:
    """Even when the operator treats ordinary job failures as unbilled, a job that 'failed'
    because we cancelled it may have run (and been billed) first: it stays a potential charge."""
    monkeypatch.setattr(get_settings(), "count_failed_requests_as_billed", count_failed)
    fake.control.cancel_reports_job_failed = True
    ctx = await _ctx()
    key = f"cxl:{ctx.project_id}"
    fake.control.hang_fetches = 1
    task = asyncio.create_task(metering.generate_image(_req(), ctx, key=key))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if fake.control.fetches:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.1)
    assert fake.control.cancels  # acknowledged by the provider
    async with transaction() as db:
        await db.execute(text("update provider_attempts set updated_at = now() - interval '1 hour' "
                              "where project_id = :p"), {"p": ctx.project_id})  # fmt: skip
    await metering.reconcile_open_requests()
    ((_, status, counted, basis),) = await _attempts(ctx.project_id)
    assert counted == Decimal("0.04") and basis == "unresolved", (status, counted, basis)


# ------------------------------------------------------------------ 3. deletion recovery
async def test_deletion_recovers_when_stripe_clerk_and_storage_all_fail(client: Any, headers: dict[str, str],
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    """Every external cleanup fails at first. Nothing is lost: Stripe ids, the auth subject and
    the unconfirmed object keys are all still held; each part is retried; and once the services
    recover, deletion completes."""
    import httpx
    from pydantic import SecretStr
    from sqlalchemy import update

    from storypanel import storage
    from storypanel.db.models import AccountDeletion, Asset, PendingObjectDeletion
    from storypanel.services import billing, deletion
    from tests.test_deletion import _exists, _PartialDenial, _put

    uid = uuid.UUID((await client.get("/v1/me", headers=headers)).json()["id"])
    keys = await _put(2)
    async with transaction() as db:
        await db.execute(
            update(User).where(User.id == uid).values(stripe_customer_id="cus_x", subscription_id="sub_x")
        )
        for k in keys:
            db.add(
                Asset(
                    owner_id=uid,
                    kind="panel",
                    storage_key=k,
                    content_type="image/png",
                    bytes=1,
                    sha256="0" * 64,
                )
            )
    healthy = {"stripe": False, "clerk": False, "s3": False}

    async def cancel_all(**kw: Any) -> int:
        if not healthy["stripe"]:
            raise AppError(ErrorCode.PROVIDER_ERROR, "The payment service is unavailable.")
        return 1

    monkeypatch.setattr(billing, "cancel_all_subscriptions", cancel_all)
    real_http = httpx.AsyncClient
    monkeypatch.setattr(deletion.httpx, "AsyncClient", lambda **kw: real_http(transport=httpx.MockTransport(
        lambda r: httpx.Response(200 if healthy["clerk"] else 503)), **kw))  # fmt: skip
    s = get_settings()
    monkeypatch.setattr(s, "auth_mode", "clerk")
    monkeypatch.setattr(s, "clerk_secret_key", SecretStr("sk_clerk_test"))
    real_s3 = storage._client()
    monkeypatch.setattr(
        storage, "_client", lambda: real_s3 if healthy["s3"] else _PartialDenial(real_s3, {keys[0]})
    )

    await deletion.request_deletion(uid, source="user")
    for _ in range(2):  # repeated passes while everything is down
        await deletion.advance(uid)
        async with transaction() as db:
            await db.execute(update(AccountDeletion).values(next_attempt_at=AccountDeletion.requested_at))
    async with transaction() as db:
        job = (await db.execute(select(AccountDeletion).where(AccountDeletion.user_id == uid))).scalar_one()
        pending = (await db.execute(select(PendingObjectDeletion.storage_key))).scalars().all()
    assert job.status == "billing" and "anonymising" not in job.steps and "purging" not in job.steps
    assert job.stripe_customer_id == "cus_x" and job.subscription_ids == ["sub_x"]  # needed to finish billing
    assert job.auth_subject is not None  # needed to finish the Clerk deletion
    assert list(pending) == [keys[0]] and await _exists(keys[0]) and not await _exists(keys[1])
    for e in ("billing:", "purging:", "identity:"):
        assert e in (job.last_error or ""), job.last_error
    healthy.update(stripe=True, clerk=True, s3=True)
    assert await deletion.advance(uid) == "completed"
    async with transaction() as db:
        job = (await db.execute(select(AccountDeletion).where(AccountDeletion.user_id == uid))).scalar_one()
        assert (await db.execute(select(PendingObjectDeletion))).first() is None
    assert job.stripe_customer_id is None and job.auth_subject is None and not await _exists(keys[0])


async def test_deletion_waits_for_workflow_cancellation_and_retries_dead_letters(client: Any,
                                                                                 headers: dict[str, str]) -> None:  # fmt: skip
    """If Temporal cannot be reached, the cancellation is retried (dead letters revived) and
    content is not purged while a workflow might still be running."""
    from sqlalchemy import update

    from storypanel.db.models import AccountDeletion, Outbox, Project
    from storypanel.services import deletion

    uid = uuid.UUID((await client.get("/v1/me", headers=headers)).json()["id"])
    pid = uuid.uuid4()
    async with transaction() as db:
        db.add(Project(id=pid, owner_id=uid, prompt="p", workflow_id=f"comic-{pid}"))
        db.add(Outbox(kind="start_workflow", dedupe_key=f"start:{pid}", status="delivered",
                      payload={"workflow": "ComicWorkflow", "workflow_id": f"comic-{pid}", "input": {"project_id": str(pid)}}))  # fmt: skip
    await deletion.request_deletion(uid, source="user")
    assert await deletion.advance(uid) == "draining"
    async with transaction() as db:
        row = (await db.execute(select(Outbox).where(Outbox.kind == "cancel"))).scalar_one()
        assert row.payload["workflow_id"] == f"comic-{pid}" and row.dedupe_key.startswith(
            f"cancel:deletion:{uid}:"
        )
        await db.execute(update(Outbox).where(Outbox.id == row.id).values(status="dead", attempts=20))
        assert (await db.execute(select(Project).where(Project.id == pid))).first() is not None  # not purged
    assert await deletion.advance(uid) == "draining"
    async with transaction() as db:
        row = (await db.execute(select(Outbox).where(Outbox.kind == "cancel"))).scalar_one()
        assert row.status == "pending" and row.attempts == 0  # revived, not abandoned
        job = (await db.execute(select(AccountDeletion).where(AccountDeletion.user_id == uid))).scalar_one()
        assert "workflow cancellations not yet delivered" in (job.last_error or "")
        await db.execute(
            update(Outbox).where(Outbox.id == row.id).values(status="delivered")
        )  # Temporal is back
    assert await deletion.advance(uid) == "completed"


# ------------------------------------------------------------------ 4. storage permissions
# Every S3 operation the deployed application performs -> the IAM actions it needs.
# (create_bucket / put_bucket_cors are local-only bootstrap in ensure_bucket; Terraform owns the
# deployed bucket, its CORS and its versioning.)
_S3_ACTIONS = {
    "put_object": {"s3:PutObject"}, "get_object": {"s3:GetObject"},
    "generate_presigned_url": {"s3:GetObject"}, "head_bucket": {"s3:ListBucket"},
    "get_bucket_versioning": {"s3:GetBucketVersioning"}, "list_object_versions": {"s3:ListBucketVersions"},
    # DeleteObjects is authorised per key: DeleteObject, and DeleteObjectVersion when a
    # VersionId is given (which delete_keys does on versioned buckets)
    "delete_objects": {"s3:DeleteObject", "s3:DeleteObjectVersion"},
}  # fmt: skip
_LOCAL_ONLY = {"create_bucket", "put_bucket_cors"}


def test_terraform_iam_grants_exactly_what_storage_uses() -> None:
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    code = (root / "services/backend/src/storypanel/storage.py").read_text()
    used = set(re.findall(r"_(?:signing_)?client\(\)\.([a-z_]+)", code))
    assert used - _LOCAL_ONLY <= set(_S3_ACTIONS), (
        f"unmapped S3 calls: {used - _LOCAL_ONLY - set(_S3_ACTIONS)}"
    )
    needed = set().union(*(_S3_ACTIONS[op] for op in used - _LOCAL_ONLY))
    tf = (root / "infra/terraform/main.tf").read_text()
    policy = tf[
        tf.index('data "aws_iam_policy_document" "app"') : tf.index('resource "aws_iam_user_policy" "app"')
    ]
    granted = set(re.findall(r'"(s3:[A-Za-z*]+)"', policy))
    assert "s3:*" not in granted
    assert needed <= granted, f"missing in Terraform: {needed - granted}"
    assert granted <= needed, f"granted but unused (least privilege): {granted - needed}"
    # object actions on bucket/*, bucket actions on the bucket ARN
    for stmt in re.findall(r"statement \{(.*?)\n  \}", policy, re.S):
        acts = set(re.findall(r'"(s3:[A-Za-z]+)"', stmt))
        obj = acts & {"s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion"}
        assert not obj or "/*" in stmt, stmt
        assert not (acts - obj) or "/*" not in stmt, stmt
    # versioning is on, so deletion must remove versions (delete_keys does) and noncurrent
    # versions also expire by lifecycle as a backstop
    assert 'status = "Enabled"' in tf and "noncurrent_version_expiration" in tf


# ------------------------------------------------------------------ 6. subscription periods
async def test_upgrade_with_billing_anchor_reset_grants_the_new_full_period(
    client: Any, headers: dict[str, str]
) -> None:
    """Upgrade with billing_cycle_anchor=now: Stripe credits unused creator time and charges a
    full new pro period starting today. That paid period earns the full pro allowance."""
    from tests.test_billing import PERIOD, hook, invoice, line, plan_credits, user_with_customer

    uid = await user_with_customer(client, headers)
    await hook(
        client, "invoice.paid", invoice("in_1", "subscription_create", [line("price_creator", PERIOD, 900)])
    )
    reset = (PERIOD[0] + 10 * 86400, PERIOD[0] + 10 * 86400 + 2_592_000)
    await hook(client, "invoice.paid", invoice("in_2", "subscription_update", [
        line("price_creator", (reset[0], PERIOD[1]), -600, proration=True),
        line("price_pro", reset, 2900)]))  # fmt: skip
    assert await plan_credits(uid) == 80 + 240


async def test_proration_arriving_before_the_base_invoice_is_not_lost(
    client: Any, headers: dict[str, str]
) -> None:
    from tests.test_billing import PERIOD, hook, invoice, line, plan_credits, user_with_customer

    uid = await user_with_customer(client, headers)
    change = PERIOD[0] + 10 * 86400
    upgrade = invoice("in_up", "subscription_update", [line("price_creator", (change, PERIOD[1]), -600, proration=True),
                                                       line("price_pro", (change, PERIOD[1]), 1300, proration=True)])  # fmt: skip
    await hook(client, "invoice.paid", upgrade)  # delivered first
    await hook(
        client, "invoice.paid", invoice("in_1", "subscription_create", [line("price_creator", PERIOD, 900)])
    )
    assert await plan_credits(uid) == 240  # same total as in-order delivery (80 + 160)
    # and in-order delivery of a duplicate base after that grants nothing more
    await hook(
        client, "invoice.paid", invoice("in_1b", "subscription_cycle", [line("price_creator", PERIOD, 900)])
    )
    assert await plan_credits(uid) == 240


async def test_two_live_subscriptions_each_grant_their_own_periods_and_alert(
    client: Any, headers: dict[str, str]
) -> None:
    from prometheus_client import REGISTRY

    from tests.test_billing import PERIOD, hook, invoice, line, plan_credits, sub_obj, user_with_customer

    uid = await user_with_customer(client, headers)
    before = (
        REGISTRY.get_sample_value("sp_billing_anomalies_total", {"kind": "multiple_live_subscriptions"}) or 0
    )
    await hook(
        client,
        "customer.subscription.created",
        sub_obj("active", "price_creator", "sub_a", uid=uid),
        created=10,
    )
    await hook(
        client, "customer.subscription.created", sub_obj("active", "price_pro", "sub_b", uid=uid), created=11
    )
    await hook(
        client,
        "invoice.paid",
        invoice(
            "in_a", "subscription_create", [line("price_creator", PERIOD, 900, sub="sub_a")], sub="sub_a"
        ),
    )
    await hook(
        client,
        "invoice.paid",
        invoice("in_b", "subscription_create", [line("price_pro", PERIOD, 2900, sub="sub_b")], sub="sub_b"),
    )
    assert await plan_credits(uid) == 80 + 240  # both were paid for
    assert (await client.get("/v1/credits", headers=headers)).json()["plan"] == "pro"  # best plan
    after = (
        REGISTRY.get_sample_value("sp_billing_anomalies_total", {"kind": "multiple_live_subscriptions"}) or 0
    )
    assert after > before  # support is alerted


# ------------------------------------------------------------------ 7. evaluation run budget
async def test_eval_run_budget_survives_restart_midnight_concurrency_and_unresolved_charges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from storypanel.evals.harness import ensure_run

    run = f"audit-{uuid.uuid4().hex[:6]}"
    uid, spent = await ensure_run(run, Decimal("0.30"), "audit")
    assert spent == 0
    ctx = lambda: CallContext("panel_generate", str(uid), str(uuid.uuid4()))  # noqa: E731
    # an unresolved charge (submission outcome unknown) counts against the run
    from storypanel.errors import AppError as _AE

    fake.control.errors["image:submit"] = [_AE(ErrorCode.PROVIDER_AMBIGUOUS, "timeout", retryable=True)]
    with pytest.raises(AppError):
        await metering.generate_image(_req(), ctx(), key=f"amb:{run}")
    await metering.generate_image(_req(), ctx(), key=f"ok:{run}")
    async with transaction() as db:  # both happened "before midnight"
        await db.execute(text("alter table provider_attempts disable trigger provider_attempts_guard"))
        await db.execute(
            text("update provider_attempts set created_at = now() - interval '1 day' where user_id = :u"),
            {"u": uid},
        )
        await db.execute(text("alter table provider_attempts enable trigger provider_attempts_guard"))
    # "restart": a new process re-attaches to the run and sees what was already counted
    uid2, spent2 = await ensure_run(run, Decimal("0.30"), "audit")
    assert uid2 == uid and spent2 == Decimal("0.08")
    with pytest.raises(ValueError):
        await ensure_run(run, Decimal("5"), "audit")  # the ceiling cannot be silently raised

    # concurrent reservations: 0.22 left = at most 5 more images of 0.04
    async def one(n: int) -> str:
        try:
            await metering.generate_image(_req(), ctx(), key=f"c{n}:{run}")
            return "ok"
        except AppError as exc:
            return exc.code.value

    results = await asyncio.gather(*[one(n) for n in range(12)])
    assert results.count("ok") == 5 and results.count("spend_limit_reached") == 7
    async with transaction() as db:
        total = (await db.execute(text("select sum(cost_counted_usd) from provider_attempts where user_id = :u"),
                                  {"u": uid})).scalar_one()  # fmt: skip
    assert Decimal(total) == Decimal("0.28") <= Decimal("0.30")


# ------------------------------------------------------------------ compute-time billing (fal ESRGAN)
@pytest.mark.usefixtures("fal_voyage")
async def test_compute_second_billing_reserves_the_wait_budget_and_stays_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io

    from PIL import Image

    from storypanel.providers import pricing

    table = {"entries": [dict(e) for e in pricing.SIMULATED["entries"]]}
    for e in table["entries"]:
        if e["model"] == "fake-upscale":
            e.update(unit="per_compute_second", usd="0.00111")
    monkeypatch.setattr(pricing, "SIMULATED", table)
    pricing.reset_cache()
    try:
        ctx = await _ctx("export")
        buf = io.BytesIO()
        Image.new("RGB", (32, 24), "white").save(buf, format="PNG")
        result = await metering.upscale_image(
            buf.getvalue(), ctx, key=f"up:{ctx.project_id}", wait_seconds=150
        )
        ((no, status, counted, basis),) = await _attempts(ctx.project_id)
        assert status == "succeeded" and basis == "unresolved"  # compute time is not observable: upper bound
        assert counted == Decimal("0.1665")  # 150 s x $0.00111
        assert (
            await metering.record_provider_charges(
                [("fake", result.request_id or "", Decimal("0.0047"), "fal usage export")]
            )
            == 1
        )
        ((_, _, counted, basis),) = await _attempts(ctx.project_id)
        assert (counted, basis) == (Decimal("0.0047"), "provider_confirmed")
    finally:
        monkeypatch.undo()
        pricing.reset_cache()
