"""Durable, ordered project events backing the SSE stream.

Events are rows first (so a reconnecting client can replay from its last sequence id)
and a Redis publish second (a wake-up hint only; losing it costs latency, not data).
"""

from __future__ import annotations

import uuid
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from storypanel.config import get_settings
from storypanel.db.models import Project, ProjectEvent

_redis: aioredis.Redis | None = None


def redis_client() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
    _redis = None


def channel(project_id: object) -> str:
    return f"sp:events:{project_id}"


async def emit(db: AsyncSession, project_id: uuid.UUID, type_: str, payload: dict[str, Any]) -> int:
    """Append an event inside the caller's transaction. Returns its sequence number."""
    seq = (
        await db.execute(
            update(Project)
            .where(Project.id == project_id)
            .values(event_seq=Project.event_seq + 1)
            .returning(Project.event_seq)
        )  # fmt: skip
    ).scalar_one()
    db.add(ProjectEvent(project_id=project_id, seq=seq, type=type_, payload=payload))
    db.info.setdefault("notify_projects", set()).add(str(project_id))
    return int(seq)


async def notify_after_commit(db: AsyncSession) -> None:
    """Call after commit; failures are ignored because the stream also polls."""
    for pid in db.info.pop("notify_projects", set()):
        try:
            await redis_client().publish(channel(pid), "1")
        except Exception:
            pass


async def replay(
    db: AsyncSession, project_id: uuid.UUID, after_seq: int, limit: int = 500
) -> list[ProjectEvent]:
    return list(
        (
            await db.execute(
                select(ProjectEvent)
                .where(ProjectEvent.project_id == project_id, ProjectEvent.seq > after_seq)
                .order_by(ProjectEvent.seq)
                .limit(limit)
            )  # fmt: skip
        ).scalars()
    )


async def oldest_retained_seq(db: AsyncSession, project_id: uuid.UUID) -> int | None:
    return (
        await db.execute(select(func.min(ProjectEvent.seq)).where(ProjectEvent.project_id == project_id))
    ).scalar_one()


async def purge_expired(db: AsyncSession) -> int:
    days = get_settings().event_retention_days
    result = await db.execute(
        delete(ProjectEvent).where(
            ProjectEvent.created_at < func.now() - text(f"interval '{int(days)} days'")
        )
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
