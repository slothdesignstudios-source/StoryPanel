"""Periodic housekeeping: retention, deletion, reconciliation and stuck-work detection.

Runs inside the worker process under a Redis lock so only one worker does each pass.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from storypanel import storage
from storypanel.config import get_settings
from storypanel.db.models import Asset, Export, IdempotencyKey, Outbox, PendingObjectDeletion, Project
from storypanel.db.session import transaction
from storypanel.domain.states import ACTIVE
from storypanel.obs import metrics
from storypanel.obs.logging import get_logger
from storypanel.services import credits, events

log = get_logger("maintenance")


async def delete_objects_durably(db: AsyncSession, keys: list[str], *, owner_id: uuid.UUID | None,
                                 reason: str) -> storage.DeleteResult:  # fmt: skip
    """Delete objects; every key whose deletion S3 did not confirm is recorded in
    `pending_object_deletions` in the caller's transaction, so removing the asset rows
    never drops the only reference to a still-stored object."""
    result = await storage.delete_keys(keys)
    for key, code in result.failed.items():
        await db.execute(insert(PendingObjectDeletion).values(
            storage_key=key, owner_id=owner_id, reason=reason[:40], last_error=code[:60], attempts=1)
            .on_conflict_do_update(index_elements=["storage_key"], set_={
                "attempts": PendingObjectDeletion.attempts + 1, "last_error": code[:60]}))  # fmt: skip
    if result.failed:
        log.warning("object_deletion_unconfirmed", count=len(result.failed), reason=reason,
                    codes=sorted(set(result.failed.values())))  # fmt: skip
    return result


async def retry_pending_object_deletions(owner_id: uuid.UUID | None = None, limit: int = 500) -> int:
    """Retry unconfirmed deletions. Returns how many are still unconfirmed."""
    async with transaction() as db:
        q = select(PendingObjectDeletion).where(PendingObjectDeletion.next_attempt_at <= func.now())
        if owner_id is not None:
            q = select(PendingObjectDeletion).where(PendingObjectDeletion.owner_id == owner_id)
        rows = list((await db.execute(q.limit(limit).with_for_update(skip_locked=True))).scalars())
        if rows:
            result = await storage.delete_keys([r.storage_key for r in rows])
            done = set(result.deleted)
            for r in rows:
                if r.storage_key in done:
                    await db.delete(r)
                else:
                    r.attempts += 1
                    r.last_error = result.failed.get(r.storage_key, "unknown")[:60]
                    r.next_attempt_at = datetime.now(UTC) + timedelta(
                        seconds=min(86400, 60 * 2 ** min(r.attempts, 10))
                    )
        remaining = (await db.execute(select(func.count()).select_from(PendingObjectDeletion))).scalar_one()
    metrics.object_deletions_pending.set(remaining)
    return int(remaining)


async def purge_project(project_id: uuid.UUID, reason: str = "project_deleted") -> int:
    """Hard-delete a project: stored objects first (unconfirmed ones are recorded), then
    rows (FK cascades do the rest). Safe to repeat."""
    async with transaction() as db:
        project = (
            await db.execute(select(Project.owner_id).where(Project.id == project_id))
        ).scalar_one_or_none()
        if project is None:
            return 0
        keys = list((await db.execute(select(Asset.storage_key).where(
            Asset.project_id == project_id))).scalars())  # fmt: skip
        await delete_objects_durably(db, keys, owner_id=project, reason=reason)
        await credits.close_for_project(db, project_id)
        # panel_versions/character_refs hold RESTRICT references to assets: remove them first.
        await db.execute(text("delete from panel_versions where panel_id in "
                              "(select id from panels where project_id = :p)"), {"p": project_id})  # fmt: skip
        await db.execute(text("delete from character_refs where character_id in "
                              "(select id from characters where project_id = :p)"), {"p": project_id})  # fmt: skip
        await db.execute(delete(Project).where(Project.id == project_id))
    return len(keys)


async def purge_deleted_projects() -> int:
    s = get_settings()
    cutoff = datetime.now(UTC) - timedelta(days=s.deleted_project_purge_days)
    async with transaction() as db:
        ids = list((await db.execute(select(Project.id).where(
            Project.deleted_at.is_not(None), Project.deleted_at < cutoff).limit(50))).scalars())  # fmt: skip
    for pid in ids:
        await purge_project(pid)
    return len(ids)


async def expire_exports() -> int:
    async with transaction() as db:
        rows = (await db.execute(
            select(Export.id, Asset.storage_key, Asset.id).join(Asset, Asset.id == Export.asset_id)
            .where(Export.expires_at < func.now(), Export.status == "ready").limit(100))).all()  # fmt: skip
        if rows:
            await delete_objects_durably(db, [r[1] for r in rows], owner_id=None, reason="export_expired")
            await db.execute(update(Export).where(Export.id.in_([r[0] for r in rows]))
                             .values(status="expired", asset_id=None))  # fmt: skip
            await db.execute(delete(Asset).where(Asset.id.in_([r[2] for r in rows])))
    return len(rows)


async def measure_health() -> None:
    async with transaction() as db:
        stuck = (await db.execute(text(
            """select count(*) from projects p
               where p.deleted_at is null and p.status = any(:active)
                 and p.status not like 'awaiting%'
                 and coalesce((select max(created_at) from project_events e
                               where e.project_id = p.id), p.updated_at)
                     < now() - interval '15 minutes'"""),
            {"active": [s.value for s in ACTIVE]})).scalar_one()  # fmt: skip
        metrics.stuck_projects.set(stuck)
        drift = await credits.reconcile(db)
        if drift:
            log.error("credit_reconciliation_drift", users=len(drift))
        await db.execute(delete(Outbox).where(
            Outbox.status == "delivered", Outbox.created_at < func.now() - text("interval '7 days'")))  # fmt: skip
        await db.execute(delete(IdempotencyKey).where(
            IdempotencyKey.created_at < func.now() - text("interval '2 days'")))  # fmt: skip


async def reconcile_provider_requests() -> dict[str, int]:
    from storypanel.providers import metering

    outcome = await metering.reconcile_open_requests()
    if any(outcome[k] for k in ("ambiguous", "succeeded_unused")):
        log.warning("provider_requests_reconciled", **outcome)
    return outcome


async def run_once() -> dict[str, int]:
    async with transaction() as db:
        purged_events = await events.purge_expired(db)
    result = {"events": purged_events, "projects": await purge_deleted_projects(),
              "exports": await expire_exports()}  # fmt: skip
    await measure_health()
    try:
        result.update({f"provider_{k}": v for k, v in (await reconcile_provider_requests()).items()})
    except Exception:
        log.error("provider_reconciliation_failed", exc_info=True)
    try:
        from storypanel.services import billing

        if await events.redis_client().set("sp:maintenance:subs", "1", nx=True, ex=3600):  # hourly
            result["subscriptions_reconciled"] = await billing.reconcile_subscriptions()
    except Exception:
        log.error("subscription_reconciliation_failed", exc_info=True)
    try:
        from storypanel.services import deletion

        result["account_deletions_advanced"] = len(await deletion.process_due())
        result["objects_pending_deletion"] = await retry_pending_object_deletions()
    except Exception:
        log.error("deletion_maintenance_failed", exc_info=True)
    return result


async def run_loop(stop: asyncio.Event, interval: float = 60.0) -> None:
    redis = events.redis_client()
    while not stop.is_set():
        try:
            if await redis.set("sp:maintenance:lock", "1", nx=True, ex=int(interval) - 5):
                log.info("maintenance_pass", **await run_once())
        except Exception:
            log.error("maintenance_failed", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass
