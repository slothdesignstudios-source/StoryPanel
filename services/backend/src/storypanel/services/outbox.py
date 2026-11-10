"""Transactional outbox for Temporal starts, signals and cancellations.

A database commit and a Temporal call cannot be made atomic. Instead the intent is written
in the same transaction as the state change, then delivered at least once. Delivery is
idempotent: workflow ids are deterministic, and signals carry ids the workflow dedupes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from opentelemetry import context as otel_context
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from storypanel.config import get_settings
from storypanel.db.models import Outbox
from storypanel.db.session import transaction
from storypanel.obs import metrics
from storypanel.obs.logging import get_logger
from storypanel.obs.tracing import extract_trace_context, inject_trace_context

log = get_logger("outbox")
MAX_ATTEMPTS = 20


async def enqueue(db: AsyncSession, kind: str, dedupe_key: str, payload: dict[str, Any]) -> None:
    payload = {**payload, "trace": inject_trace_context()}
    await db.execute(
        insert(Outbox).values(kind=kind, dedupe_key=dedupe_key, payload=payload)
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
    )  # fmt: skip
    db.info["outbox_dirty"] = True


async def _deliver(client: Client, row: Outbox) -> None:
    p = row.payload
    s = get_settings()
    # Re-attach the trace of the request that wrote this row, so the workflow started or
    # signalled here continues that trace across the asynchronous boundary.
    token = otel_context.attach(extract_trace_context(p.get("trace")))
    try:
        await _deliver_inner(client, row, p, s)
    finally:
        otel_context.detach(token)


async def _deliver_inner(client: Client, row: Outbox, p: dict[str, Any], s: Any) -> None:
    if row.kind == "start_workflow":
        try:
            await client.start_workflow(
                p["workflow"], p["input"], id=p["workflow_id"], task_queue=s.temporal_task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                execution_timeout=timedelta(days=p.get("timeout_days", 45)),
            )  # fmt: skip
            metrics.workflow_started.labels(p["workflow"]).inc()
        except WorkflowAlreadyStartedError:
            pass  # a previous delivery succeeded; that is the desired end state
    elif row.kind in ("signal", "cancel"):
        handle: WorkflowHandle[Any, Any] = client.get_workflow_handle(p["workflow_id"])
        try:
            if row.kind == "signal":
                await handle.signal(p["signal"], p["input"])
            else:
                await handle.cancel()
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                log.warning("outbox_target_gone", kind=row.kind, workflow_id=p["workflow_id"])
                return  # the workflow already finished; nothing left to deliver to
            raise
    else:
        raise ValueError(f"unknown outbox kind {row.kind}")


async def dispatch_pending(client: Client, batch: int = 50) -> int:
    """Deliver due rows. SKIP LOCKED lets several API/worker processes share the work."""
    delivered = 0
    async with transaction() as db:
        rows = list(
            (
                await db.execute(
                    select(Outbox)
                    .where(Outbox.status == "pending", Outbox.next_attempt_at <= func.now())
                    .order_by(Outbox.id)
                    .limit(batch)
                    .with_for_update(skip_locked=True)
                )  # fmt: skip
            ).scalars()
        )
        for row in rows:
            try:
                await _deliver(client, row)
                row.status = "delivered"
                delivered += 1
            except Exception as exc:
                row.attempts += 1
                row.last_error = type(exc).__name__
                delay = min(300, 2 ** min(row.attempts, 8))
                row.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
                if row.attempts >= MAX_ATTEMPTS:
                    row.status = "dead"
                log.error("outbox_delivery_failed", kind=row.kind, attempts=row.attempts,
                          error=type(exc).__name__)  # fmt: skip
    return delivered


async def refresh_gauges() -> None:
    async with transaction() as db:
        count, oldest = (
            await db.execute(
                select(func.count(), func.min(Outbox.created_at)).where(Outbox.status == "pending")
            )
        ).one()
    metrics.outbox_pending.set(count)
    age = (datetime.now(UTC) - oldest).total_seconds() if oldest else 0.0
    metrics.outbox_oldest_age.set(age)


async def run_dispatcher(client: Client, stop: asyncio.Event, wake: asyncio.Event) -> None:
    """Background loop: wakes immediately after a commit, and polls as the safety net."""
    while not stop.is_set():
        try:
            while await dispatch_pending(client):
                pass
            await refresh_gauges()
        except Exception as exc:
            log.error("outbox_loop_error", error=type(exc).__name__)
        wake.clear()
        try:
            await asyncio.wait_for(wake.wait(), timeout=2.0)
        except TimeoutError:
            pass
