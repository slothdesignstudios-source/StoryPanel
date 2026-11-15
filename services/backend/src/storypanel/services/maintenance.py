"""Periodic housekeeping: retention, deletion, reconciliation and stuck-work detection.

Runs inside the worker process under a Redis lock so only one worker does each pass.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

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
        # Images a synchronous provider delivered for this comic are kept as owner-level assets
        # (so a retry never pays twice); they go with the comic. The attempt rows (financial
        # records) keep their costs and ids; their asset reference simply no longer resolves.
        result_ids = list((await db.execute(text(
            "select result_asset_id from provider_attempts where project_id = :p and result_asset_id is not null"),
            {"p": project_id})).scalars())  # fmt: skip
        if result_ids:
            keys += list(
                (await db.execute(select(Asset.storage_key).where(Asset.id.in_(result_ids)))).scalars()
            )
            await db.execute(delete(Asset).where(Asset.id.in_(result_ids)))
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


# ------------------------------------------------------------------ lost workflows
# The stage a comic stopped in, by status: where Resume restarts it (nothing already made is redone).
RESUME_STAGE = {"queued": "intake", "intake": "intake", "awaiting_answers": "intake", "planning": "planning",
                "awaiting_cast_approval": "cast_gate", "scripting": "scripting",
                "awaiting_script_approval": "script_gate", "illustrating": "illustrating"}  # fmt: skip
LOST_MESSAGE = (
    "This comic's background job was lost (the local servers restarted without keeping it). "
    "Press Resume to continue from where it stopped."
)


async def find_lost_workflows(
    client: Any, min_age: timedelta = timedelta(minutes=10)
) -> list[dict[str, Any]]:
    """Running comics whose workflow no longer runs in Temporal (not found, or closed). Read-only.

    A comic whose workflow start is still waiting in the outbox, or that changed in the last
    `min_age`, is never reported."""
    from temporalio.client import WorkflowExecutionStatus
    from temporalio.service import RPCError, RPCStatusCode

    async with transaction() as db:
        rows = (await db.execute(
            select(Project.id, Project.status, Project.workflow_id, Project.updated_at)
            .where(Project.status.in_([s.value for s in ACTIVE]), Project.deleted_at.is_(None),
                   Project.workflow_id.is_not(None),
                   Project.updated_at < datetime.now(UTC) - min_age))).all()  # fmt: skip
        queued = set((await db.execute(select(Outbox.payload["workflow_id"].astext).where(
            Outbox.status == "pending", Outbox.kind == "start_workflow"))).scalars())  # fmt: skip
    lost = []
    for pid, status, wid, _ in rows:
        if wid in queued:
            continue
        try:
            state = (await client.get_workflow_handle(wid).describe()).status
            if state == WorkflowExecutionStatus.RUNNING:
                continue
            why = f"workflow {state.name.lower() if state else 'closed'}"
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
            why = "workflow not found"
        lost.append({"project_id": str(pid), "status": status, "workflow_id": wid, "reason": why,
                     "resume_from": RESUME_STAGE[status], **await _resume_preview(uuid.UUID(str(pid)), status)})  # fmt: skip
    return lost


async def _resume_preview(pid: uuid.UUID, status: str) -> dict[str, Any]:
    """Read-only: credits held now (released if the comic is marked stopped) and what pressing
    Resume would redo. Anything already saved (Story Bible, ready sheets, location references,
    the written script, drawn panels) is reused; only unfinished items are made again."""
    from storypanel.db.models import Character, CreditReservation, Location, Panel, PanelVersion

    async with transaction() as db:
        held = (await db.execute(select(func.coalesce(func.sum(CreditReservation.amount - CreditReservation.settled), 0))
                                 .where(CreditReservation.project_id == pid, CreditReservation.status == "open"))).scalar_one()  # fmt: skip
        stage = RESUME_STAGE[status]
        if stage in ("cast_gate", "script_gate"):
            redo, paid = "nothing: it returns to the approval and waits", False
        elif stage == "scripting":
            redo, paid = (
                "writes the script if none was saved (one paid text call), then the Story Bible check",
                True,
            )
        elif stage == "illustrating":
            todo = (await db.execute(select(func.count()).select_from(Panel).where(
                Panel.project_id == pid, Panel.status.not_in(["ok", "needs_attention"])))).scalar_one()  # fmt: skip
            orphan = (await db.execute(select(func.count()).select_from(PanelVersion).join(
                Panel, Panel.id == PanelVersion.panel_id).where(
                Panel.project_id == pid, Panel.current_version_id.is_(None)))).scalar_one()  # fmt: skip
            redo = (f"draws the {todo} panel(s) not yet drawn (paid image calls and checks); drawn panels are kept"
                    + (f"; {orphan} image(s) made before the loss but never finished are not reused and are drawn again"
                       if orphan else ""))  # fmt: skip
            paid = todo > 0
        else:  # intake / planning
            sheets = (await db.execute(select(func.count()).select_from(Character).where(
                Character.project_id == pid, Character.sheet_status != "ready"))).scalar_one()  # fmt: skip
            locs = (await db.execute(select(func.count()).select_from(Location).where(
                Location.project_id == pid, Location.reference_asset_id.is_(None)))).scalar_one()  # fmt: skip
            redo = (
                f"finishes planning: a saved Story Bible is reused; {sheets} character sheet(s) and {locs} location "
                "reference(s) not yet saved are drawn (paid)"
            )
            paid = True
    return {"credits_held": int(held), "resume_redoes": redo, "resume_makes_paid_calls": paid}


async def recover_lost_workflows(client: Any, *, apply: bool,
                                 min_age: timedelta = timedelta(minutes=10)) -> list[dict[str, Any]]:  # fmt: skip
    """Mark each lost comic as stopped where it was, releasing its credit holds, so the person can
    press Resume (which restarts at that stage; nothing already made or paid for is redone).
    With apply=False only lists them."""
    from storypanel.pipeline.stages_art import stage_end

    lost = await find_lost_workflows(client, min_age)
    for item in lost if apply else ():
        await stage_end(uuid.UUID(item["project_id"]), status="failed", code="workflow_lost", message=LOST_MESSAGE,
                        resume_from=item["resume_from"])  # fmt: skip
        log.warning("lost_workflow_recovered", **item)
    return lost
