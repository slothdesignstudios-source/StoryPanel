"""Durable, idempotent account deletion.

Policy (docs/data-retention.md):
- Requesting deletion blocks the account at once: `users.deleted_at` is set in the same
  transaction that records the job, every API call then fails with "deleted", and asset
  writes for the owner are refused (repo.save_* check the flag under a row lock).
- Work is cancelled (project, panel and export workflows; queued workflow starts are
  withdrawn) and subscriptions are cancelled. Draining then waits until every workflow
  cancellation has been delivered to Temporal (dead-lettered ones are retried), and paid provider calls in flight are given
  `deletion_drain_seconds` to finish, after which they are cancelled at the provider and
  left counted as unresolved potential charges.
- Content is deleted and *verified*: the step completes only when the owner has no asset
  rows and no unconfirmed object deletions. Versioned objects are deleted version by version.
- Subscriptions are cancelled immediately in Stripe (no proration refund, no final
  invoice). Stripe identifiers are kept on the job until that succeeds.
- The Clerk identity is deleted (unless Clerk itself told us it was deleted).
- Finally the user row is anonymised. Retained: credit ledger and provider cost records
  (financial, keyed by an internal id), audit entries, this job row (hashes and
  timestamps only) and signup-grant hashes (abuse prevention).

Each step is safe to repeat. A failing step records its error and is retried by the
maintenance loop with backoff; nothing later runs until it succeeds.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from storypanel.config import get_settings
from storypanel.db.models import (
    AccountDeletion,
    Asset,
    BillingSubscription,
    Export,
    IdempotencyKey,
    PendingObjectDeletion,
    Project,
    ProviderAttempt,
    SignupGrant,
    User,
)  # fmt: skip
from storypanel.db.session import transaction
from storypanel.errors import AppError, ErrorCode
from storypanel.obs import metrics
from storypanel.obs.logging import get_logger
from storypanel.services import outbox

log = get_logger("deletion")
# Billing runs right after work is cancelled: a slow or stuck content purge must never keep
# charging a customer who asked to leave.
STEPS = ("cancelling", "billing", "draining", "purging", "identity", "anonymising")


def identity_hash(kind: str, value: str) -> str:
    """Keyed hash of an identity, so retained records cannot be reversed to a person."""
    key = get_settings().pseudonym_salt.get_secret_value().encode()
    return hmac.new(key, f"{kind}:{value.strip().lower() if kind == 'email' else value}".encode(),
                    hashlib.sha256).hexdigest()  # fmt: skip


class StepPending(Exception):
    """The step cannot finish yet (e.g. work still draining); try again later."""


# ------------------------------------------------------------------ request
async def request_deletion(user_id: uuid.UUID, *, source: str, request_id: str | None = None) -> str:
    """Record the deletion and block the account. Idempotent; returns the job status."""
    from storypanel.services import repo

    async with transaction() as db:
        # Lock the user row first: concurrent requests serialise here, and asset writers
        # holding FOR KEY SHARE finish before the account is marked deleted.
        user = (
            await db.execute(select(User).where(User.id == user_id).with_for_update())
        ).scalar_one_or_none()
        if user is None:
            return "missing"
        existing = (await db.execute(select(AccountDeletion.status).where(
            AccountDeletion.user_id == user_id))).scalar_one_or_none()  # fmt: skip
        if existing is not None:
            return existing
        user.deleted_at = func.now()
        mirrored = set((await db.execute(select(BillingSubscription.id).where(
            BillingSubscription.user_id == user_id))).scalars())  # fmt: skip
        if user.subscription_id:
            mirrored.add(user.subscription_id)
        db.add(AccountDeletion(
            user_id=user_id, subject_hash=identity_hash("sub", user.auth_subject), source=source,
            status="requested", auth_subject=user.auth_subject, stripe_customer_id=user.stripe_customer_id,
            subscription_ids=sorted(mirrored), steps={}, attempts=0))  # fmt: skip
        await repo.audit(db, user_ref=str(user_id), action=f"account.delete.{source}", request_id=request_id)
    metrics.account_deletions.labels("requested").inc()
    log.info("account_deletion_requested", source=source)
    return "requested"


async def blocked_subject(db: AsyncSession, subject: str, issued_at: int | None) -> bool:
    """A subject whose account was deleted. Tokens issued before the deletion stay dead;
    a fresh sign-in after it completed may create a new, empty account."""
    row = (await db.execute(select(AccountDeletion.status, AccountDeletion.requested_at).where(
        AccountDeletion.subject_hash == identity_hash("sub", subject)))).one_or_none()  # fmt: skip
    if row is None:
        return False
    status, requested_at = row
    if status != "completed":
        return True
    return issued_at is None or datetime.fromtimestamp(issued_at, UTC) <= requested_at


# ------------------------------------------------------------------ steps
async def _owned_workflow_ids(db: AsyncSession, user_id: uuid.UUID) -> set[str]:
    ids: set[str] = set()
    ids |= {
        w
        for w in (await db.execute(select(Project.workflow_id).where(Project.owner_id == user_id))).scalars()
        if w
    }
    ids |= set((await db.execute(text(
        "select distinct j.workflow_id from jobs j join projects p on p.id = j.project_id where p.owner_id = :u"),
        {"u": user_id})).scalars())  # fmt: skip
    # Panel and export workflows are identified by their start rows.
    ids |= set((await db.execute(text("""
        select o.payload->>'workflow_id' from outbox o
        where o.kind = 'start_workflow' and (
          (o.payload->'input'->>'panel_id') in
            (select pa.id::text from panels pa join pages pg on pg.id = pa.page_id
             join projects p on p.id = pg.project_id where p.owner_id = :u)
          or (o.payload->'input'->>'export_id') in (select e.id::text from exports e where e.owner_id = :u)
          or (o.payload->'input'->>'project_id') in (select p.id::text from projects p where p.owner_id = :u))"""),
        {"u": user_id})).scalars())  # fmt: skip
    return {i for i in ids if i}


async def _step_cancelling(job: AccountDeletion) -> None:
    async with transaction() as db:
        wf_ids = await _owned_workflow_ids(db, job.user_id)
        # Withdraw starts that have not been delivered yet: those workflows never run.
        withdrawn = set((await db.execute(text("""
            update outbox set status = 'withdrawn', last_error = 'account deleted'
            where status = 'pending' and kind in ('start_workflow', 'signal')
              and payload->>'workflow_id' = any(:ids)
            returning payload->>'workflow_id'"""), {"ids": list(wf_ids)})).scalars())  # fmt: skip
        never_started = withdrawn - set((await db.execute(text(
            "select payload->>'workflow_id' from outbox where kind = 'start_workflow' and status = 'delivered' "
            "and payload->>'workflow_id' = any(:ids)"), {"ids": list(withdrawn)})).scalars())  # fmt: skip
        to_cancel = sorted(wf_ids - never_started)
        for wf in to_cancel:
            await outbox.enqueue(db, "cancel", f"cancel:deletion:{job.user_id}:{wf}", {"workflow_id": wf})
        await db.execute(update(Export).where(Export.owner_id == job.user_id,
                                              Export.status.in_(("queued", "running")))
                         .values(status="cancelled"))  # fmt: skip
    log.info("account_deletion_cancelled_work", workflows=len(to_cancel), never_started=len(never_started))


async def _cancels_outstanding(user_id: uuid.UUID) -> int:
    """Workflow cancellations not yet delivered to Temporal. Dead-lettered ones are put back
    in the queue: deletion keeps retrying (and the overdue-deletion alert fires) rather than
    proceeding while a workflow might still be running."""
    async with transaction() as db:
        revived = (await db.execute(text("""
            update outbox set status = 'pending', attempts = 0, next_attempt_at = now() + interval '1 minute'
            where kind = 'cancel' and status = 'dead' and dedupe_key like :k"""),
            {"k": f"cancel:deletion:{user_id}:%"})).rowcount  # type: ignore[attr-defined]  # fmt: skip
        if revived:
            log.warning("account_deletion_cancel_requeued", count=revived)
            metrics.account_deletions.labels("cancel_requeued").inc()
        return int((await db.execute(text(
            "select count(*) from outbox where kind = 'cancel' and status <> 'delivered' and dedupe_key like :k"),
            {"k": f"cancel:deletion:{user_id}:%"})).scalar_one())  # fmt: skip


async def _step_draining(job: AccountDeletion) -> None:
    from storypanel.providers import metering

    outstanding = await _cancels_outstanding(job.user_id)
    if outstanding:
        raise StepPending(f"{outstanding} workflow cancellations not yet delivered")

    async with transaction() as db:
        open_attempts = list((await db.execute(select(ProviderAttempt).where(
            ProviderAttempt.user_id == job.user_id,
            ProviderAttempt.status.in_(("reserved", "submitting", "started"))))).scalars())  # fmt: skip
    if not open_attempts:
        return
    waited = datetime.now(UTC) - job.requested_at
    if waited < timedelta(seconds=get_settings().deletion_drain_seconds):
        raise StepPending(f"{len(open_attempts)} paid provider calls still in flight")
    # Waited long enough: stop them at the provider; their cost stays counted (unresolved).
    await metering.stop_attempts([metering._snap(a) for a in open_attempts], reason="account_deleted")


async def _step_purging(job: AccountDeletion) -> None:
    from storypanel.services import maintenance

    async with transaction() as db:
        pids = list((await db.execute(select(Project.id).where(Project.owner_id == job.user_id))).scalars())
    for pid in pids:
        await maintenance.purge_project(pid, reason="account_deleted")
    async with transaction() as db:
        rows = (
            await db.execute(select(Asset.id, Asset.storage_key).where(Asset.owner_id == job.user_id))
        ).all()
        await maintenance.delete_objects_durably(db, [r[1] for r in rows], owner_id=job.user_id,
                                                 reason="account_deleted")  # fmt: skip
        await db.execute(delete(Asset).where(Asset.id.in_([r[0] for r in rows])))
    await maintenance.retry_pending_object_deletions(owner_id=job.user_id)
    async with transaction() as db:
        assets = (await db.execute(select(func.count()).select_from(Asset).where(
            Asset.owner_id == job.user_id))).scalar_one()  # fmt: skip
        pending = (await db.execute(select(func.count()).select_from(PendingObjectDeletion).where(
            PendingObjectDeletion.owner_id == job.user_id))).scalar_one()  # fmt: skip
    if assets or pending:
        raise StepPending(f"content not yet confirmed deleted (assets={assets}, objects={pending})")


async def _step_billing(job: AccountDeletion) -> None:
    from storypanel.services import billing

    if not job.stripe_customer_id and not job.subscription_ids:
        return
    cancelled = await billing.cancel_all_subscriptions(
        customer_id=job.stripe_customer_id, subscription_ids=list(job.subscription_ids or []),
        idempotency_prefix=f"deletion:{job.user_id}")  # fmt: skip
    log.info("account_deletion_subscriptions_cancelled", count=cancelled)
    async with transaction() as db:
        await db.execute(update(AccountDeletion).where(AccountDeletion.user_id == job.user_id)
                         .values(stripe_customer_id=None, subscription_ids=[]))  # fmt: skip
        await db.execute(update(User).where(User.id == job.user_id).values(
            stripe_customer_id=None, subscription_id=None, subscription_status="canceled"))  # fmt: skip


async def _step_identity(job: AccountDeletion) -> None:
    s = get_settings()
    if job.source == "clerk_webhook" or s.auth_mode != "clerk" or not job.auth_subject:
        return  # Clerk already deleted it, or identities are local (dev)
    if s.clerk_secret_key is None:
        raise AppError(ErrorCode.INVALID_STATE, "SP_CLERK_SECRET_KEY is not configured", retryable=True)
    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.delete(f"{s.clerk_api_url}/users/{job.auth_subject}",
                                 headers={"Authorization": f"Bearer {s.clerk_secret_key.get_secret_value()}"})  # fmt: skip
    if resp.status_code not in (200, 404):  # 404: already gone
        raise AppError(ErrorCode.PROVIDER_ERROR, f"identity deletion failed: HTTP {resp.status_code}")


async def _step_anonymising(job: AccountDeletion) -> None:
    async with transaction() as db:
        await db.execute(delete(IdempotencyKey).where(IdempotencyKey.user_id == job.user_id))
        await db.execute(update(User).where(User.id == job.user_id).values(
            email=None, auth_subject=f"deleted:{job.user_id}", stripe_customer_id=None, subscription_id=None))  # fmt: skip
        await db.execute(update(AccountDeletion).where(AccountDeletion.user_id == job.user_id).values(
            auth_subject=None, status="completed", completed_at=func.now(), last_error=None))  # fmt: skip


_RUN = {"cancelling": _step_cancelling, "draining": _step_draining, "purging": _step_purging,
        "billing": _step_billing, "identity": _step_identity, "anonymising": _step_anonymising}  # fmt: skip


# Steps that must wait for an earlier one; billing and identity only need "cancelling".
_REQUIRES = {"billing": ("cancelling",), "draining": ("cancelling",), "purging": ("draining",),
             "identity": ("cancelling",), "anonymising": STEPS[:-1]}  # fmt: skip


async def advance(user_id: uuid.UUID) -> str:
    """Run every step whose prerequisites are done. A failure in one independent step
    (billing, purge chain, identity) does not hold up the others; anonymisation waits for
    all of them. Safe to call concurrently (a lock admits one runner) and repeatedly.
    Returns "completed", "busy", or the first step still outstanding."""
    from storypanel.services.events import redis_client

    lock = redis_client().lock(f"sp:deletion:{user_id}", timeout=900, blocking=False)
    if not await lock.acquire():
        return "busy"
    try:
        progressed = True
        failed: list[str] = []
        failed_steps: set[str] = set()  # retried on a later call (with backoff), not in this one
        while progressed:
            progressed = False
            async with transaction() as db:
                job = (
                    await db.execute(select(AccountDeletion).where(AccountDeletion.user_id == user_id))
                ).scalar_one()
                db.expunge(job)
            if job.status == "completed":
                return "completed"
            done = dict(job.steps or {})
            for step in STEPS:
                if (
                    step in done
                    or step in failed_steps
                    or any(r not in done for r in _REQUIRES.get(step, ()))
                ):
                    continue
                try:
                    await _RUN[step](job)
                except StepPending as exc:
                    await _note(user_id, step, str(exc), backoff=False)
                    failed.append(f"{step}: {exc}")
                    failed_steps.add(step)
                    continue
                except Exception as exc:
                    await _note(user_id, step, f"{type(exc).__name__}: {getattr(exc, 'message', exc)}",
                                backoff=True)  # fmt: skip
                    log.warning("account_deletion_step_failed", step=step, error=type(exc).__name__)
                    metrics.account_deletions.labels("step_failed").inc()
                    failed.append(f"{step}: {type(exc).__name__}: {getattr(exc, 'message', exc)}")
                    failed_steps.add(step)
                    continue
                done[step] = True
                progressed = True
                async with transaction() as db:
                    await db.execute(text(
                        "update account_deletions set steps = coalesce(steps, '{}'::jsonb) || "
                        "jsonb_build_object(cast(:s as text), now()), "
                        "status = case when cast(:s as text) = 'anonymising' then 'completed' else status end "
                        "where user_id = :u"), {"s": step, "u": user_id})  # fmt: skip
                if step == "anonymising":
                    metrics.account_deletions.labels("completed").inc()
                    log.info("account_deletion_completed")
                    return "completed"
                job = await _reload(user_id)
            outstanding = next(st for st in STEPS if st not in done)
            async with transaction() as db:
                values: dict[str, Any] = {
                    "status": outstanding,
                    "last_error": "; ".join(failed)[:200] or None,
                }
                if not failed:
                    values["attempts"] = 0
                await db.execute(
                    update(AccountDeletion).where(AccountDeletion.user_id == user_id).values(**values)
                )
        return outstanding
    finally:
        try:
            await lock.release()
        except Exception:
            pass


async def _reload(user_id: uuid.UUID) -> AccountDeletion:
    async with transaction() as db:
        job = (
            await db.execute(select(AccountDeletion).where(AccountDeletion.user_id == user_id))
        ).scalar_one()
        db.expunge(job)
        return job


async def _note(user_id: uuid.UUID, step: str, error: str, *, backoff: bool) -> None:
    async with transaction() as db:
        job = (await db.execute(select(AccountDeletion).where(AccountDeletion.user_id == user_id)
                                .with_for_update())).scalar_one()  # fmt: skip
        job.status = step
        job.last_error = error[:200]
        if backoff:
            job.attempts += 1
            job.next_attempt_at = datetime.now(UTC) + timedelta(
                seconds=min(3600, 15 * 2 ** min(job.attempts, 8))
            )
        else:
            job.next_attempt_at = datetime.now(UTC) + timedelta(seconds=15)


async def process_due(limit: int = 20) -> dict[str, Any]:
    async with transaction() as db:
        ids = list((await db.execute(select(AccountDeletion.user_id).where(
            AccountDeletion.status != "completed", AccountDeletion.next_attempt_at <= func.now())
            .order_by(AccountDeletion.next_attempt_at).limit(limit))).scalars())  # fmt: skip
        stuck = (await db.execute(select(func.count()).select_from(AccountDeletion).where(
            AccountDeletion.status != "completed",
            AccountDeletion.requested_at < func.now() - text("interval '1 day'")))).scalar_one()  # fmt: skip
    metrics.account_deletions_overdue.set(stuck)
    out: dict[str, Any] = {}
    for uid in ids:
        out[str(uid)] = await advance(uid)
    return out


async def record_signup_grant(db: AsyncSession, subject: str, email: str | None) -> bool:
    """True only if neither this subject nor this email has ever received the welcome grant."""
    hashes = [identity_hash("sub", subject)] + ([identity_hash("email", email)] if email else [])
    fresh = 0
    for h in hashes:
        res = await db.execute(insert(SignupGrant).values(identity_hash=h).on_conflict_do_nothing())
        fresh += res.rowcount or 0  # type: ignore[attr-defined]
    return fresh == len(hashes)
