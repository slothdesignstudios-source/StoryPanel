"""Shared persistence helpers used by API services and pipeline stages."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from storypanel import storage
from storypanel.db.models import (
    Approval,
    Asset,
    AuditLog,
    Character,
    CharacterRef,
    Job,
    Location,
    Page,
    Panel,
    Project,
    Script,
    StoryBibleRow,
    User,
)  # fmt: skip
from storypanel.domain.schemas import CharacterSpec, ComicScript, LocationSpec, StoryBible
from storypanel.domain.states import ProjectStatus, assert_transition
from storypanel.errors import not_found
from storypanel.services import credits, events


async def get_project(db: AsyncSession, project_id: uuid.UUID, *, owner_id: uuid.UUID | None = None,
                      lock: bool = False) -> Project:  # fmt: skip
    """Load a project. With owner_id this is the ownership check for every API path:
    someone else's project is indistinguishable from a missing one."""
    stmt = select(Project).where(Project.id == project_id, Project.deleted_at.is_(None))
    if owner_id is not None:
        stmt = stmt.where(Project.owner_id == owner_id)
    if lock:
        stmt = stmt.with_for_update()
    project = (await db.execute(stmt)).scalar_one_or_none()
    if project is None:
        raise not_found("Project")
    return project


async def set_status(db: AsyncSession, project: Project, target: ProjectStatus,
                     **extra: Any) -> None:  # fmt: skip
    if project.status == target.value and not extra:
        return
    assert_transition(project.status, target)
    project.status = target.value
    await events.emit(db, project.id, "project.status", {"status": target.value, **extra})


async def fail_project(db: AsyncSession, project: Project, *, code: str, message: str,
                       status: ProjectStatus = ProjectStatus.FAILED, resume_from: str | None = None,
                       **detail: Any) -> None:  # fmt: skip
    """Terminal transition with a user-safe reason. Holds on credits are released."""
    if project.status in (ProjectStatus.CANCELLED.value, ProjectStatus.BLOCKED.value):
        return
    previous = project.status
    project.failure = {"code": code, "message": message, "at": datetime.now(UTC).isoformat(),
                       "during": previous, **detail}  # fmt: skip
    project.resume_from = resume_from
    assert_transition(project.status, status)
    project.status = status.value
    await credits.close_for_project(db, project.id)
    await events.emit(db, project.id, "project.status",
                      {"status": status.value, "failure": project.failure})  # fmt: skip


async def save_image(db: AsyncSession, *, owner_id: uuid.UUID, project_id: uuid.UUID | None,
                     kind: str, image: storage.ValidatedImage) -> Asset:  # fmt: skip
    key = storage.new_key(owner_id, project_id, kind, "png")
    await storage.put_bytes(key, image.data, image.content_type)
    asset = Asset(id=uuid.uuid4(), owner_id=owner_id, project_id=project_id, kind=kind,
                  storage_key=key, content_type=image.content_type, width=image.width,
                  height=image.height, bytes=len(image.data), sha256=image.sha256)  # fmt: skip
    db.add(asset)
    await db.flush()
    return asset


async def save_blob(db: AsyncSession, *, owner_id: uuid.UUID, project_id: uuid.UUID | None,
                    kind: str, data: bytes, content_type: str, ext: str, sha256: str) -> Asset:  # fmt: skip
    key = storage.new_key(owner_id, project_id, kind, ext)
    await storage.put_bytes(key, data, content_type)
    asset = Asset(id=uuid.uuid4(), owner_id=owner_id, project_id=project_id, kind=kind,
                  storage_key=key, content_type=content_type, bytes=len(data), sha256=sha256)  # fmt: skip
    db.add(asset)
    await db.flush()
    return asset


async def asset_bytes(db: AsyncSession, asset_id: uuid.UUID) -> bytes:
    key = (await db.execute(select(Asset.storage_key).where(Asset.id == asset_id))).scalar_one()
    return await storage.get_bytes(key)


async def latest_bible(db: AsyncSession, project_id: uuid.UUID) -> StoryBible | None:
    row = (
        await db.execute(
            select(StoryBibleRow)
            .where(StoryBibleRow.project_id == project_id)
            .order_by(StoryBibleRow.version.desc())
            .limit(1)
        )  # fmt: skip
    ).scalar_one_or_none()
    return StoryBible.model_validate(row.content) if row else None


async def cast_rows(db: AsyncSession, project_id: uuid.UUID) -> list[Character]:
    return list(
        (
            await db.execute(
                select(Character).where(Character.project_id == project_id).order_by(Character.order_index)
            )  # fmt: skip
        ).scalars()
    )


async def cast_specs(db: AsyncSession, project_id: uuid.UUID) -> dict[str, CharacterSpec]:
    return {c.slug: CharacterSpec.model_validate(c.spec) for c in await cast_rows(db, project_id)}


async def location_rows(db: AsyncSession, project_id: uuid.UUID) -> list[Location]:
    return list(
        (
            await db.execute(
                select(Location).where(Location.project_id == project_id).order_by(Location.slug)
            )
        ).scalars()  # fmt: skip
    )


async def location_specs(db: AsyncSession, project_id: uuid.UUID) -> dict[str, LocationSpec]:
    return {r.slug: LocationSpec.model_validate(r.spec) for r in await location_rows(db, project_id)}


async def character_ref_assets(db: AsyncSession, project_id: uuid.UUID) -> dict[str, dict[str, str]]:
    """slug -> view -> asset id, for each character's current sheet version."""
    rows = (
        await db.execute(
            select(Character.slug, CharacterRef.view, CharacterRef.asset_id)
            .join(CharacterRef, CharacterRef.character_id == Character.id)
            .where(Character.project_id == project_id, CharacterRef.sheet_version == Character.sheet_version)
        )  # fmt: skip
    ).all()
    out: dict[str, dict[str, str]] = {}
    for slug, view, asset_id in rows:
        out.setdefault(slug, {})[view] = str(asset_id)
    return out


async def script_row(db: AsyncSession, project_id: uuid.UUID, version: int | None = None) -> Script | None:
    stmt = select(Script).where(Script.project_id == project_id)
    stmt = stmt.where(Script.version == version) if version else stmt.order_by(Script.version.desc())
    return (await db.execute(stmt.limit(1))).scalar_one_or_none()


def parse_script(row: Script) -> ComicScript:
    return ComicScript.model_validate(row.content)


async def invalidate_approvals(db: AsyncSession, project_id: uuid.UUID, kind: str) -> None:
    await db.execute(
        update(Approval)
        .where(Approval.project_id == project_id, Approval.kind == kind, Approval.status == "approved")
        .values(status="invalidated")  # fmt: skip
    )


async def approval_is_current(db: AsyncSession, project_id: uuid.UUID, kind: str, version: int) -> bool:
    """True only if this exact content version is approved *and* still the latest."""
    project = await get_project(db, project_id)
    current = project.cast_version if kind == "cast" else project.script_version
    if current != version:
        return False
    return (
        await db.execute(
            select(Approval.id).where(
                Approval.project_id == project_id,
                Approval.kind == kind,
                Approval.content_version == version,
                Approval.status == "approved",
            )  # fmt: skip
        )
    ).first() is not None


async def job_upsert(db: AsyncSession, *, project_id: uuid.UUID, workflow_id: str, stage: str,
                     target_id: str | None, status: str, attempt: int = 1,
                     error: dict[str, Any] | None = None, timings: dict[str, Any] | None = None) -> None:  # fmt: skip
    stmt = insert(Job).values(id=uuid.uuid4(), project_id=project_id, workflow_id=workflow_id,
                              stage=stage, target_id=target_id or "", status=status,
                              attempt=attempt, error=error, timings=timings or {})  # fmt: skip
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=["workflow_id", "stage", "target_id"],
            set_={
                "status": status,
                "attempt": attempt,
                "error": error,
                "timings": timings or {},
                "updated_at": datetime.now(UTC),
            },
        )  # fmt: skip
    )


async def audit(db: AsyncSession, *, user_ref: str | None, action: str, target_type: str | None = None,
                target_id: object | None = None, request_id: str | None = None,
                **meta: Any) -> None:  # fmt: skip
    db.add(AuditLog(user_ref=user_ref, action=action, target_type=target_type,
                    target_id=str(target_id) if target_id else None, meta=meta,
                    request_id=request_id))  # fmt: skip


async def owner_plan(db: AsyncSession, owner_id: uuid.UUID) -> str:
    return (await db.execute(select(User.plan).where(User.id == owner_id))).scalar_one()


async def pages_with_panels(db: AsyncSession, project_id: uuid.UUID) -> list[tuple[Page, list[Panel]]]:
    pages = list((await db.execute(select(Page).where(Page.project_id == project_id)
                                   .order_by(Page.index))).scalars())  # fmt: skip
    panels = list((await db.execute(select(Panel).where(Panel.project_id == project_id)
                                    .order_by(Panel.index))).scalars())  # fmt: skip
    by_page: dict[uuid.UUID, list[Panel]] = {}
    for panel in panels:
        by_page.setdefault(panel.page_id, []).append(panel)
    return [(page, by_page.get(page.id, [])) for page in pages]


async def lock_project_of(db: AsyncSession, model: Any, row_id: uuid.UUID) -> None:
    """Lock the parent project before locking one of its rows.

    Lock order is always project -> child row -> credits. Every transaction that emits an
    event updates the project row, so taking it first keeps concurrent panel, page and
    balloon work from deadlocking against each other.
    """
    project_id = (await db.execute(select(model.project_id).where(model.id == row_id))).scalar_one_or_none()
    if project_id is not None:
        await db.execute(select(Project.id).where(Project.id == project_id).with_for_update())
