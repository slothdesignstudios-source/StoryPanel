"""Periodic housekeeping: retention, deletion, reconciliation and stuck-work detection.

Runs inside the worker process under a Redis lock so only one worker does each pass.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, text, update

from storypanel import storage
from storypanel.config import get_settings
from storypanel.db.models import Asset, Export, IdempotencyKey, Outbox, Project, User
from storypanel.db.session import transaction
from storypanel.domain.states import ACTIVE
from storypanel.obs import metrics
from storypanel.obs.logging import get_logger
from storypanel.services import credits, events

log = get_logger("maintenance")


async def purge_project(project_id: uuid.UUID) -> int:
    """Hard-delete a project: stored objects first, then rows (FK cascades do the rest)."""
    async with transaction() as db:
        keys = list((await db.execute(select(Asset.storage_key).where(
            Asset.project_id == project_id))).scalars())  # fmt: skip
    if keys:
        await storage.delete_keys(keys)
    async with transaction() as db:
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


async def purge_user(user_id: uuid.UUID) -> None:
    """Account deletion: remove all content now, anonymise the account row.
    Ledger rows are kept (financial record) but no longer identify a person."""
    async with transaction() as db:
        ids = list((await db.execute(select(Project.id).where(Project.owner_id == user_id))).scalars())
    for pid in ids:
        await purge_project(pid)
    async with transaction() as db:
        keys = list((await db.execute(select(Asset.storage_key).where(Asset.owner_id == user_id))).scalars())
        if keys:
            await storage.delete_keys(keys)
        await db.execute(delete(Asset).where(Asset.owner_id == user_id))
        await db.execute(delete(IdempotencyKey).where(IdempotencyKey.user_id == user_id))
        await db.execute(update(User).where(User.id == user_id).values(
            email=None, auth_subject=f"deleted:{user_id}", stripe_customer_id=None,
            subscription_id=None, deleted_at=func.now()))  # fmt: skip


async def expire_exports() -> int:
    async with transaction() as db:
        rows = (await db.execute(
            select(Export.id, Asset.storage_key, Asset.id).join(Asset, Asset.id == Export.asset_id)
            .where(Export.expires_at < func.now(), Export.status == "ready").limit(100))).all()  # fmt: skip
        if rows:
            await storage.delete_keys([r[1] for r in rows])
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
