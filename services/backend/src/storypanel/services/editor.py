"""Editor operations: panel work requests, versions, balloons, layouts, reordering, exports.

Every mutation bumps project.edit_version so clients and exports can tell revisions apart.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from storypanel import storage
from storypanel.api import schemas as api
from storypanel.catalog import layouts
from storypanel.config import get_settings
from storypanel.db.models import (
    Asset,
    Balloon,
    Export,
    Page,
    Panel,
    PanelFeedback,
    PanelVersion,
    Project,
)  # fmt: skip
from storypanel.domain.states import PanelStatus
from storypanel.domain.states import ProjectStatus as S
from storypanel.domain.validation import word_count
from storypanel.errors import AppError, ErrorCode, invalid_state, not_found, version_conflict
from storypanel.obs import metrics
from storypanel.pipeline import lettering, moderation, qa
from storypanel.pipeline.stages_art import panel_rect, revision_sha, snapshot_revision
from storypanel.providers.base import CallContext
from storypanel.services import credits, events, outbox, repo
from storypanel.services.projects import balloon_out

EDITABLE = (S.COMPLETED.value, S.PARTIALLY_COMPLETED.value)


async def owned_panel(db: AsyncSession, panel_id: uuid.UUID, owner_id: uuid.UUID, *,
                      lock: bool = False) -> tuple[Panel, Project]:  # fmt: skip
    """Ownership is checked through the parent project on every panel-level route."""
    stmt = select(Panel).where(Panel.id == panel_id)
    if lock:
        await repo.lock_project_of(db, Panel, panel_id)
    panel = (await db.execute(stmt.with_for_update() if lock else stmt)).scalar_one_or_none()
    if panel is None:
        raise not_found("Panel")
    project = await repo.get_project(db, panel.project_id, owner_id=owner_id)
    return panel, project


async def owned_page(db: AsyncSession, page_id: uuid.UUID, owner_id: uuid.UUID) -> tuple[Page, Project]:
    await repo.lock_project_of(db, Page, page_id)
    page = (await db.execute(select(Page).where(Page.id == page_id).with_for_update())).scalar_one_or_none()
    if page is None:
        raise not_found("Page")
    return page, await repo.get_project(db, page.project_id, owner_id=owner_id)


def _require_editable(project: Project) -> None:
    if project.status not in EDITABLE:
        raise invalid_state("The editor opens once illustration has finished.")


async def _touch(db: AsyncSession, project: Project, type_: str, payload: dict[str, Any]) -> None:
    await db.execute(update(Project).where(Project.id == project.id)
                     .values(edit_version=Project.edit_version + 1))  # fmt: skip
    await events.emit(db, project.id, type_, payload)


# ------------------------------------------------------------------ panel work
async def request_panel_work(db: AsyncSession, *, panel_id: uuid.UUID, owner_id: uuid.UUID, kind: str,
                             instruction: str | None, key: str) -> str:  # fmt: skip
    s = get_settings()
    panel, project = await owned_panel(db, panel_id, owner_id, lock=True)
    _require_editable(project)
    if panel.status == PanelStatus.GENERATING.value:
        raise AppError(ErrorCode.CONFLICT, "This panel is already being redrawn.")
    if kind == "edit":
        if panel.current_version_id is None or panel.status == PanelStatus.BLOCKED.value:
            raise invalid_state("This panel has no image to edit. Regenerate it instead.")
        verdict = moderation.rule_check(instruction or "")
        if verdict is not None:
            raise AppError(ErrorCode.MODERATION_BLOCKED, verdict.user_message)
    request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"panel:{panel_id}:{kind}:{key}"))
    resv = await credits.reserve(
        db, owner_id, s.credits_edit if kind == "edit" else s.credits_regenerate, purpose=kind,
        project_id=project.id, idempotency_key=f"{kind}:{request_id}")  # fmt: skip
    # A failed/blocked panel being retried is an initial generation, not a paid redo of a
    # delivered image, but it is priced the same and charged only if it yields an image.
    work_kind = "edit" if kind == "edit" else "regenerate"
    panel.intent_seq += 1
    panel.status = PanelStatus.GENERATING.value
    metrics.manual_regenerations.labels(kind).inc()
    await events.emit(db, project.id, "panel.generating", {"panel_id": str(panel_id), "kind": kind})
    await outbox.enqueue(db, "start_workflow", f"start:panel:{request_id}", {
        "workflow": "PanelWorkWorkflow", "workflow_id": f"panel-{request_id}", "timeout_days": 1,
        "input": {"panel_id": str(panel_id), "page_id": str(panel.page_id), "kind": work_kind,
                  "request_key": request_id, "intent_seq": panel.intent_seq,
                  "instruction": instruction, "reservation_id": str(resv.id),
                  "max_qa_retries": s.max_panel_qa_retries}})  # fmt: skip
    return request_id


async def list_versions(db: AsyncSession, panel_id: uuid.UUID, owner_id: uuid.UUID) -> list[api.VersionOut]:
    panel, project = await owned_panel(db, panel_id, owner_id)
    names = {c.slug: c.name for c in await repo.cast_rows(db, project.id)}
    bible = await repo.latest_bible(db, project.id)
    rules = {r.id: r.text for r in (bible.continuity_rules if bible else [])}
    rows = (await db.execute(
        select(PanelVersion, Asset.storage_key, Asset.quarantined)
        .join(Asset, Asset.id == PanelVersion.image_asset_id)
        .where(PanelVersion.panel_id == panel_id).order_by(PanelVersion.created_at.desc()))).all()  # fmt: skip
    out = []
    for v, key, quarantined in rows:
        if v.qa_status in ("blocked", "pending") or quarantined:
            continue  # blocked versions are never listed, compared or restorable
        state, reasons = qa.qa_reasons(
            v.qa_status, (v.qa_scores or {}).get("problems", []), names=names, rules=rules
        )
        out.append(api.VersionOut(
            id=str(v.id), kind=v.kind, instruction=v.instruction, qa_status=v.qa_status,
            qa_problems=(v.qa_scores or {}).get("problems", []), qa_state=state, qa_reasons=reasons,  # type: ignore[arg-type]
            attempt=v.attempt,
            created_at=v.created_at, image_url=storage.signed_url(key),
            is_current=v.id == panel.current_version_id, model=v.model))  # fmt: skip
    return out


async def restore_version(db: AsyncSession, panel_id: uuid.UUID, version_id: uuid.UUID,
                          owner_id: uuid.UUID) -> None:  # fmt: skip
    panel, project = await owned_panel(db, panel_id, owner_id, lock=True)
    _require_editable(project)
    row = (await db.execute(
        select(PanelVersion, Asset.quarantined).join(Asset, Asset.id == PanelVersion.image_asset_id)
        .where(PanelVersion.id == version_id, PanelVersion.panel_id == panel_id))).one_or_none()  # fmt: skip
    if row is None or row[1] or row[0].qa_status not in ("pass", "fail", "review"):
        raise not_found("Version")
    panel.current_version_id = version_id
    panel.status = (PanelStatus.OK if row[0].qa_status == "pass" else PanelStatus.NEEDS_ATTENTION).value
    panel.intent_seq += 1  # an in-flight job for this panel must not overwrite this choice
    await _touch(db, project, "panel.updated", {
        "panel_id": str(panel_id), "page_id": str(panel.page_id), "status": panel.status,
        "version_id": str(version_id), "applied": True, "kind": "restore"})  # fmt: skip


async def feedback(db: AsyncSession, panel_id: uuid.UUID, owner_id: uuid.UUID, rating: int) -> None:
    from sqlalchemy.dialects.postgresql import insert

    panel, _ = await owned_panel(db, panel_id, owner_id)
    if panel.current_version_id is None:
        raise invalid_state("This panel has no image yet.")
    await db.execute(
        insert(PanelFeedback).values(id=uuid.uuid4(), user_id=owner_id,
                                     panel_version_id=panel.current_version_id, rating=rating)
        .on_conflict_do_update(index_elements=["user_id", "panel_version_id"],
                               set_={"rating": rating}))  # fmt: skip


# ------------------------------------------------------------------ balloons
def _clamp_geometry(g: dict[str, Any], page_size: tuple[int, int], kind: str, text: str) -> dict[str, Any]:
    """All lettering ink (shape, text, SFX outline, tail) inside the print safe area, never the
    trim edge or bleed, where it would be cut off."""
    return lettering.clamp_to_safe_area(kind, text, g, (float(page_size[0]), float(page_size[1])))


def _check_text(text: str) -> None:
    s = get_settings()
    if word_count(text) > s.max_words_per_balloon:
        raise AppError(ErrorCode.VALIDATION, f"Balloons hold at most {s.max_words_per_balloon} words.",
                       fields=[{"field": "text", "message": "Shorten this line or split it."}])  # fmt: skip
    missing = lettering.unsupported_chars(text)
    if missing:
        raise AppError(ErrorCode.VALIDATION, lettering.unsupported_message(missing),
                       fields=[{"field": "text", "message": lettering.unsupported_message(missing)}])  # fmt: skip
    verdict = moderation.rule_check(text)
    if verdict is not None:
        raise AppError(ErrorCode.MODERATION_BLOCKED, verdict.user_message)


async def patch_balloon(db: AsyncSession, balloon_id: uuid.UUID, owner_id: uuid.UUID,
                        body: api.PatchBalloon) -> api.BalloonOut:  # fmt: skip
    await repo.lock_project_of(db, Balloon, balloon_id)
    b = (
        await db.execute(select(Balloon).where(Balloon.id == balloon_id).with_for_update())
    ).scalar_one_or_none()
    if b is None:
        raise not_found("Balloon")
    project = await repo.get_project(db, b.project_id, owner_id=owner_id)
    _require_editable(project)
    if b.version != body.version:
        # Optimistic concurrency: the client edited an older copy (another tab, or a retry).
        raise version_conflict("This balloon", b.version)
    page_size = layouts()[(await db.execute(
        select(Page.layout_template_id).join(Panel, Panel.page_id == Page.id)
        .where(Panel.id == b.panel_id))).scalar_one()].page  # fmt: skip
    if body.text is not None:
        _check_text(body.text)
        b.text = " ".join(body.text.split())
    # Typesetting fields are server-owned: whatever the client sent is recomputed. Lettering is
    # kept inside the print safe area whether the box moved or its text (e.g. a wider SFX) changed.
    g = body.geometry.model_dump() if body.geometry is not None else dict(b.geometry)
    b.geometry, b.flags = lettering.typeset(b.type, b.text, _clamp_geometry(g, page_size, b.type, b.text))
    b.user_edited = True
    b.version += 1
    await _touch(db, project, "balloon.updated", {"balloon_id": str(b.id), "panel_id": str(b.panel_id),
                                                  "version": b.version})  # fmt: skip
    return balloon_out(b)


async def create_balloon(db: AsyncSession, panel_id: uuid.UUID, owner_id: uuid.UUID,
                         body: api.CreateBalloon) -> api.BalloonOut:  # fmt: skip
    s = get_settings()
    panel, project = await owned_panel(db, panel_id, owner_id, lock=True)
    _require_editable(project)
    _check_text(body.text)
    existing = list((await db.execute(select(Balloon).where(Balloon.panel_id == panel_id))).scalars())
    if len(existing) >= s.max_balloons_per_panel:
        raise AppError(ErrorCode.VALIDATION, f"A panel holds at most {s.max_balloons_per_panel} balloons.")
    speakers = {c["id"] for c in panel.spec.get("characters", [])}
    if body.type in ("speech", "thought") and body.speaker_id not in speakers:
        raise AppError(ErrorCode.VALIDATION, "Choose a speaker who appears in this panel.",
                       fields=[{"field": "speaker_id", "message": "Unknown speaker."}])  # fmt: skip
    page = (await db.execute(select(Page).where(Page.id == panel.page_id))).scalar_one()
    template = layouts()[page.layout_template_id]
    placed = lettering.layout_panel(
        panel_rect=panel_rect(page, panel), page_size=template.page,
        dialogue=[{"type": body.type, "speaker": body.speaker_id, "text": body.text}],
        characters=panel.spec.get("characters", []))[0]  # fmt: skip
    order = max((b.order_index for b in existing), default=-1) + 1
    b = Balloon(id=uuid.uuid4(), project_id=project.id, panel_id=panel_id, order_index=order,
                type=body.type, speaker_id=body.speaker_id, text=placed.text,
                geometry=placed.geometry(), flags=placed.flags, user_edited=True, style={},
                version=1)  # fmt: skip
    db.add(b)
    await db.flush()
    await _touch(db, project, "balloon.updated", {"balloon_id": str(b.id), "panel_id": str(panel_id)})
    return balloon_out(b)


async def delete_balloon(db: AsyncSession, balloon_id: uuid.UUID, owner_id: uuid.UUID) -> None:
    await repo.lock_project_of(db, Balloon, balloon_id)
    b = (
        await db.execute(select(Balloon).where(Balloon.id == balloon_id).with_for_update())
    ).scalar_one_or_none()
    if b is None:
        raise not_found("Balloon")
    project = await repo.get_project(db, b.project_id, owner_id=owner_id)
    _require_editable(project)
    await db.delete(b)
    await _touch(db, project, "balloon.deleted", {"balloon_id": str(balloon_id),
                                                  "panel_id": str(b.panel_id)})  # fmt: skip


# ------------------------------------------------------------------ layout & order
def _remap(g: dict[str, Any], old: tuple[float, ...], new: tuple[float, ...]) -> dict[str, Any]:
    """Carry a balloon from one panel rectangle to another, keeping its relative position.
    Size is scaled uniformly so the text keeps its proportions."""
    sx, sy = new[2] / old[2], new[3] / old[3]
    k = min(sx, sy, 1.25)
    w, h = g["w"] * k, g["h"] * k
    cx = new[0] + (g["x"] + g["w"] / 2 - old[0]) * sx
    cy = new[1] + (g["y"] + g["h"] / 2 - old[1]) * sy
    out = {**g, "x": round(cx - w / 2, 1), "y": round(cy - h / 2, 1), "w": round(w, 1), "h": round(h, 1)}
    if g.get("tail"):
        out["tail"] = {"x": round(new[0] + (g["tail"]["x"] - old[0]) * sx, 1),
                       "y": round(new[1] + (g["tail"]["y"] - old[1]) * sy, 1)}  # fmt: skip
    return out


async def _move_balloons(db: AsyncSession, panel: Panel, old: tuple[float, ...], new: tuple[float, ...],
                         page_size: tuple[int, int]) -> None:  # fmt: skip
    for b in (await db.execute(select(Balloon).where(Balloon.panel_id == panel.id))).scalars():
        b.geometry, b.flags = lettering.typeset(
            b.type, b.text, _clamp_geometry(_remap(b.geometry, old, new), page_size, b.type, b.text)
        )
        b.version += 1


async def change_layout(db: AsyncSession, page_id: uuid.UUID, owner_id: uuid.UUID, template_id: str) -> None:
    page, project = await owned_page(db, page_id, owner_id)
    _require_editable(project)
    panels = list((await db.execute(select(Panel).where(Panel.page_id == page_id)
                                    .order_by(Panel.index))).scalars())  # fmt: skip
    new = layouts().get(template_id)
    old = layouts()[page.layout_template_id]
    if new is None or new.format != old.format or new.panel_count != len(panels):
        raise AppError(ErrorCode.VALIDATION, "That layout doesn't fit this page.",
                       fields=[{"field": "layout_template_id", "message": "Pick a listed layout."}])  # fmt: skip
    for panel in panels:
        await _move_balloons(db, panel, tuple(map(float, old.rects[panel.index])),
                             tuple(map(float, new.rects[panel.index])), new.page)  # fmt: skip
    page.layout_template_id = template_id
    await _touch(db, project, "page.updated", {"page_id": str(page_id), "change": "layout"})


async def reorder_panels(db: AsyncSession, page_id: uuid.UUID, owner_id: uuid.UUID,
                         panel_ids: list[str]) -> None:  # fmt: skip
    page, project = await owned_page(db, page_id, owner_id)
    _require_editable(project)
    panels = {str(p.id): p for p in (await db.execute(
        select(Panel).where(Panel.page_id == page_id).with_for_update())).scalars()}  # fmt: skip
    if sorted(panel_ids) != sorted(panels):
        raise AppError(ErrorCode.VALIDATION, "List every panel on the page exactly once.")
    template = layouts()[page.layout_template_id]
    for new_index, pid in enumerate(panel_ids):
        panel = panels[pid]
        if panel.index != new_index:
            await _move_balloons(db, panel, tuple(map(float, template.rects[panel.index])),
                                 tuple(map(float, template.rects[new_index])), template.page)  # fmt: skip
            panel.index = new_index  # the unique (page, index) constraint is deferred to commit
    await _touch(db, project, "page.updated", {"page_id": str(page_id), "change": "order"})


# ------------------------------------------------------------------ exports
async def moderate_edited_dialogue(db: AsyncSession, project: Project) -> None:
    """Run the full moderation classifier (not just seed-word rules) over every balloon whose
    text changed since it was last moderated, before anything can be exported. The project
    row is locked by the caller, so balloon edits cannot slip in between check and snapshot."""
    pending = list((await db.execute(select(Balloon).where(Balloon.project_id == project.id)
                                     .order_by(Balloon.panel_id, Balloon.order_index))).scalars())  # fmt: skip
    pending = [b for b in pending if b.moderated_sha != moderation.text_sha(b.text)]
    if not pending:
        return
    bible = await repo.latest_bible(db, project.id)
    from storypanel.domain.schemas import AgeRating

    audience = bible.audience if bible else AgeRating.ALL_AGES
    verdict = await moderation.moderate_text(
        "\n".join(b.text for b in pending), audience=audience, surface="balloon_edit",
        ctx=CallContext("balloon_moderation", str(project.owner_id), str(project.id)))  # fmt: skip
    if not verdict.allowed:
        raise AppError(ErrorCode.MODERATION_BLOCKED,
                       "Some edited dialogue isn't allowed, so this comic can't be exported. "
                       + (verdict.user_message or "Please change the edited lines."),
                       meta={"balloons": len(pending)})  # fmt: skip
    for b in pending:
        b.moderated_sha = moderation.text_sha(b.text)


async def create_export(db: AsyncSession, project: Project, fmt: str, key: str) -> Export:
    _require_editable(project)
    existing = (await db.execute(select(Export).where(
        Export.idempotency_key == f"{project.id}:{key}"))).scalar_one_or_none()  # fmt: skip
    if existing:
        return existing
    running = (await db.execute(select(func.count()).select_from(Export).where(
        Export.project_id == project.id, Export.status.in_(("queued", "running"))))).scalar_one()  # fmt: skip
    if running >= 3:
        raise AppError(ErrorCode.QUOTA_EXCEEDED, "Wait for the current exports to finish first.")
    await moderate_edited_dialogue(db, project)
    await db.refresh(project)
    revision = await snapshot_revision(db, project)
    if not any(p["version_id"] for page in revision["pages"] for p in page["panels"]):
        raise invalid_state("There are no finished panels to export yet.")
    export = Export(id=uuid.uuid4(), project_id=project.id, owner_id=project.owner_id, format=fmt,
                    revision=revision, revision_sha256=revision_sha(revision), pages_done={},
                    idempotency_key=f"{project.id}:{key}")  # fmt: skip
    db.add(export)
    await db.flush()
    await events.emit(db, project.id, "export.updated", {"export_id": str(export.id),
                                                         "status": "queued", "format": fmt})  # fmt: skip
    await outbox.enqueue(db, "start_workflow", f"start:export:{export.id}", {
        "workflow": "ExportWorkflow", "workflow_id": f"export-{export.id}", "timeout_days": 1,
        "input": {"export_id": str(export.id)}})  # fmt: skip
    return export


async def retry_export(db: AsyncSession, export_id: uuid.UUID, owner_id: uuid.UUID) -> Export:
    """Re-run a failed export of the same frozen revision; finished pages are reused."""
    export = (await db.execute(select(Export).where(
        Export.id == export_id, Export.owner_id == owner_id).with_for_update())).scalar_one_or_none()  # fmt: skip
    if export is None:
        raise not_found("Export")
    if export.status != "failed":
        raise invalid_state("Only a failed export can be retried.")
    export.attempt += 1  # under the row lock: concurrent retries serialise, then see "queued"
    export.status, export.error = "queued", None
    await outbox.enqueue(db, "start_workflow", f"start:export:{export.id}:a{export.attempt}", {
        "workflow": "ExportWorkflow", "workflow_id": f"export-{export.id}-a{export.attempt}",
        "timeout_days": 1, "input": {"export_id": str(export.id), "attempt": export.attempt}})  # fmt: skip
    await events.emit(db, export.project_id, "export.updated", {"export_id": str(export.id),
                                                                "status": "queued"})  # fmt: skip
    return export
