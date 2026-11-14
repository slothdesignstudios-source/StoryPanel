"""Project lifecycle operations behind the API: create, gates, edits, cancel, resume."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from storypanel import storage
from storypanel.api import schemas as api
from storypanel.catalog import layouts, styles
from storypanel.config import get_settings
from storypanel.db.models import (
    Approval,
    Asset,
    Balloon,
    Character,
    CharacterRef,
    Export,
    Job,
    Location,
    Page,
    Panel,
    PanelVersion,
    Project,
    Script,
    StoryBibleRow,
)  # fmt: skip
from storypanel.domain.schemas import ComicFormat, ComicScript, StoryBible
from storypanel.domain.states import ACTIVE, PanelStatus
from storypanel.domain.states import ProjectStatus as S
from storypanel.domain.validation import has_errors, validate_script
from storypanel.errors import AppError, ErrorCode, invalid_state, not_found, version_conflict
from storypanel.obs import metrics
from storypanel.pipeline import lettering, moderation, story
from storypanel.pipeline.stages_art import panel_rect
from storypanel.pipeline.stages_plan import record_approval
from storypanel.services import credits, events, outbox, repo


def workflow_input(project: Project, **extra: Any) -> dict[str, Any]:
    s = get_settings()
    return {"project_id": str(project.id), "approval_ttl_days": s.approval_ttl_days,
            "answers_ttl_days": s.answers_ttl_days, "max_qa_retries": s.max_panel_qa_retries,
            **extra}  # fmt: skip


def plan_credits() -> int:
    s = get_settings()
    return s.credits_cast + s.credits_script


def _url(key: str | None, quarantined: bool = False) -> str | None:
    """Signed, short-lived URL. Quarantined assets are never signed."""
    return storage.signed_url(key) if key and not quarantined else None


# ------------------------------------------------------------------ create
async def create_project(db: AsyncSession, *, user_id: uuid.UUID, plan: str, body: api.CreateProject,
                         key: str) -> Project:  # fmt: skip
    s = get_settings()
    prompt, _ = moderation.neutralise_style_references(body.prompt)
    verdict = moderation.rule_check(prompt)
    if verdict is not None:  # obvious violations are refused before anything is created
        metrics.moderation_results.labels("prompt", "blocked").inc()
        raise AppError(ErrorCode.MODERATION_BLOCKED, verdict.user_message,
                       fields=[{"field": "prompt", "message": verdict.suggested_rewrite or
                                "Please change the idea and try again."}])  # fmt: skip
    missing = lettering.unsupported_chars(body.prompt)
    if missing:
        # Refused before any paid call: a story in characters the fonts cannot draw could not be
        # lettered, and we never rewrite the user's words for them.
        raise AppError(ErrorCode.VALIDATION, lettering.unsupported_message(missing),
                       fields=[{"field": "prompt", "message": lettering.unsupported_message(missing)}])  # fmt: skip
    answers: dict[str, str] = {}
    if body.style_preset:
        if body.style_preset not in styles().allowed_for_plan(plan):
            raise AppError(ErrorCode.VALIDATION, "That style isn't available on your plan.",
                           fields=[{"field": "style_preset", "message": "Choose another style."}])  # fmt: skip
        answers["style"] = body.style_preset
    if body.audience:
        answers["audience"] = body.audience
    if body.format == "strip":
        answers["length"] = "A 4-panel strip"
    elif body.pages:
        answers["length"] = f"{min(body.pages, s.max_pages)} pages"
    active = (await db.execute(select(func.count()).select_from(Project).where(
        Project.owner_id == user_id, Project.deleted_at.is_(None),
        Project.status.in_([x.value for x in ACTIVE])))).scalar_one()  # fmt: skip
    if active >= 5:
        raise AppError(ErrorCode.QUOTA_EXCEEDED,
                       "You have 5 comics in progress. Finish or cancel one to start another.")  # fmt: skip
    image_provider, image_model = choose_image_provider(body.image_provider)
    project = Project(id=uuid.uuid4(), owner_id=user_id, prompt=body.prompt, answers=answers or None,
                      pages_requested=body.pages or s.default_pages,
                      format=body.format or "western_page", style_preset=body.style_preset,
                      image_provider=image_provider, image_model=image_model)  # fmt: skip
    project.workflow_id = f"comic-{project.id}"
    db.add(project)
    await db.flush()
    await credits.reserve(db, user_id, plan_credits(), purpose="plan", project_id=project.id,
                          idempotency_key=f"plan:{project.id}")  # fmt: skip
    await events.emit(db, project.id, "project.status", {"status": S.QUEUED.value})
    await outbox.enqueue(db, "start_workflow", f"start:{project.workflow_id}", {
        "workflow": "ComicWorkflow", "workflow_id": project.workflow_id,
        "input": workflow_input(project)})  # fmt: skip
    return project


async def estimate(db: AsyncSession, user_id: uuid.UUID, project: Project | None,
                   pages: int | None = None, fmt: str | None = None) -> api.EstimateOut:  # fmt: skip
    s = get_settings()
    bal = await credits.get_balance(db, user_id)
    if project is None or project.status in (S.QUEUED.value, S.INTAKE.value):
        n_pages = 1 if fmt == "strip" else (pages or s.default_pages)
        panels = 4 if fmt == "strip" else n_pages * s.estimated_panels_per_page
        breakdown = {"cast_and_references": s.credits_cast, "story_and_script": s.credits_script}
        total = plan_credits()
        return api.EstimateOut(
            step="start", credits=total, breakdown=breakdown, balance=bal.balance,
            available=bal.available, sufficient=bal.available >= total,
            note=f"Illustration is charged later, after you approve the script: about "
                 f"{panels * s.credits_per_panel} more credits for roughly {panels} panels. "
                 "The exact amount is shown before you approve.")  # fmt: skip
    row = await repo.script_row(db, project.id)
    if row is None:
        return api.EstimateOut(step="plan", credits=0, breakdown={}, balance=bal.balance,
                               available=bal.available, sufficient=True,
                               note="Cast and script are already covered.")  # fmt: skip
    panels = sum(len(p.panels) for p in repo.parse_script(row).pages)
    total = panels * s.credits_per_panel
    return api.EstimateOut(
        step="illustration", credits=total, breakdown={"panels": panels,
                                                       "credits_per_panel": s.credits_per_panel},
        balance=bal.balance, available=bal.available, sufficient=bal.available >= total,
        note="Automatic quality retries are free. Panels that fail or are blocked are not charged.")  # fmt: skip


# ------------------------------------------------------------------ gates
async def submit_answers(db: AsyncSession, project: Project, answers: dict[str, str], key: str) -> None:
    if project.status != S.AWAITING_ANSWERS.value:
        raise invalid_state("This comic isn't waiting for answers.")
    valid_ids = {q["id"] for q in (project.questions or [])}
    project.answers = {**(project.answers or {}),
                       **{k: v[:120] for k, v in answers.items() if k in valid_ids}}  # fmt: skip
    await outbox.enqueue(db, "signal", f"answers:{project.id}", {
        "workflow_id": project.workflow_id, "signal": "answers_submitted",
        "input": {"id": f"answers:{project.id}"}})  # fmt: skip


async def revise_character(db: AsyncSession, project: Project, slug: str, instruction: str,
                           key: str) -> str:  # fmt: skip
    s = get_settings()
    if project.status != S.AWAITING_CAST_APPROVAL.value:
        raise invalid_state("Characters can only be revised before the cast is approved.")
    verdict = moderation.rule_check(instruction)
    if verdict is not None:
        raise AppError(ErrorCode.MODERATION_BLOCKED, verdict.user_message)
    char = (await db.execute(select(Character).where(
        Character.project_id == project.id, Character.slug == slug).with_for_update())).scalar_one_or_none()  # fmt: skip
    if char is None:
        raise not_found("Character")
    if char.pending_revision or char.sheet_status != "ready":
        raise AppError(ErrorCode.CONFLICT, "This character is already being revised.")
    request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"revise:{char.id}:{key}"))
    resv = await credits.reserve(db, project.owner_id, s.credits_character_revision,
                                 purpose="revision", project_id=project.id,
                                 idempotency_key=f"revision:{request_id}")  # fmt: skip
    char.pending_revision = {"request_id": request_id, "instruction": instruction,
                             "reservation_id": str(resv.id)}  # fmt: skip
    char.sheet_status = "revising"
    await events.emit(db, project.id, "character.revising", {"slug": slug})
    await outbox.enqueue(db, "signal", f"revise:{request_id}", {
        "workflow_id": project.workflow_id, "signal": "revise_character",
        "input": {"id": request_id, "slug": slug}})  # fmt: skip
    return request_id


async def patch_bible(db: AsyncSession, project: Project, body: api.PatchBible) -> None:
    if project.status != S.AWAITING_CAST_APPROVAL.value:
        raise invalid_state("The Story Bible can only be edited before the cast is approved.")
    if body.cast_version != project.cast_version:
        raise version_conflict("The cast", project.cast_version)
    bible = await repo.latest_bible(db, project.id)
    assert bible is not None
    changes = body.model_dump(exclude_none=True, exclude={"cast_version"})
    text = " ".join(str(v) for v in changes.values())
    verdict = moderation.rule_check(text)
    if verdict is not None:
        raise AppError(ErrorCode.MODERATION_BLOCKED, verdict.user_message)
    updated = StoryBible.model_validate({**bible.model_dump(mode="json"), **changes})
    version = (await db.execute(select(func.max(StoryBibleRow.version)).where(
        StoryBibleRow.project_id == project.id))).scalar_one()  # fmt: skip
    db.add(StoryBibleRow(project_id=project.id, version=version + 1,
                         content=updated.model_dump(mode="json"), source="user"))  # fmt: skip
    project.title = updated.title[:200]
    project.cast_version += 1  # approvals are scoped to the exact version the user saw
    await repo.invalidate_approvals(db, project.id, "cast")
    await events.emit(db, project.id, "bible.updated", {"cast_version": project.cast_version})


async def approve_cast(db: AsyncSession, project: Project, version: int, user_id: uuid.UUID) -> None:
    if project.status != S.AWAITING_CAST_APPROVAL.value:
        if await repo.approval_is_current(db, project.id, "cast", version):
            return  # duplicate approval of an already-approved version: idempotent success
        raise invalid_state("The cast isn't waiting for approval.")
    if version != project.cast_version:
        raise version_conflict("The cast", project.cast_version)
    cast = await repo.cast_rows(db, project.id)
    if any(c.sheet_status != "ready" or c.pending_revision for c in cast):
        raise AppError(ErrorCode.CONFLICT, "Wait for all character sheets to finish first.")
    await record_approval(db, project.id, "cast", version, user_id)
    await events.emit(db, project.id, "cast.approved", {"cast_version": version})
    await outbox.enqueue(db, "signal", f"approve:cast:{project.id}:{version}", {
        "workflow_id": project.workflow_id, "signal": "approve",
        "input": {"id": f"cast:{project.id}:{version}", "kind": "cast", "version": version}})  # fmt: skip


async def patch_script(db: AsyncSession, project: Project, body: api.PatchScript) -> Script:
    if project.status != S.AWAITING_SCRIPT_APPROVAL.value:
        raise invalid_state("The script can only be edited while it is waiting for approval.")
    if body.base_version != project.script_version:
        raise version_conflict("The script", project.script_version)
    bible = await repo.latest_bible(db, project.id)
    current = await repo.script_row(db, project.id, project.script_version)
    assert bible is not None and current is not None
    text = " ".join(f"{p.action} " + " ".join(d.text for d in p.dialogue)
                    for page in body.script.pages for p in page.panels)  # fmt: skip
    verdict = moderation.rule_check(text)
    if verdict is not None:
        raise AppError(ErrorCode.MODERATION_BLOCKED, verdict.user_message)
    issues = validate_script(
        body.script, fmt=bible.format.type, expected_pages=len(body.script.pages)
        if bible.format.type is ComicFormat.WESTERN_PAGE else 1,
        character_ids=set((await repo.cast_specs(db, project.id)).keys()),
        location_ids={loc.id for loc in bible.locations}, limits=story.script_limits(bible))  # fmt: skip
    project.script_version += 1
    row = Script(project_id=project.id, version=project.script_version, beats=current.beats,
                 content=body.script.model_dump(), issues=[i.model_dump() for i in issues],
                 source="user")  # fmt: skip
    db.add(row)
    await repo.invalidate_approvals(db, project.id, "script")
    await events.emit(db, project.id, "script.updated", {
        "script_version": project.script_version, "has_errors": has_errors(issues)})  # fmt: skip
    return row


async def approve_script(db: AsyncSession, project: Project, version: int, user_id: uuid.UUID) -> None:
    s = get_settings()
    if project.status != S.AWAITING_SCRIPT_APPROVAL.value:
        if await repo.approval_is_current(db, project.id, "script", version):
            return
        raise invalid_state("The script isn't waiting for approval.")
    if version != project.script_version:
        raise version_conflict("The script", project.script_version)
    row = await repo.script_row(db, project.id, version)
    assert row is not None
    script = ComicScript.model_validate(row.content)
    if any(i.get("severity") == "error" for i in row.issues):
        raise AppError(ErrorCode.VALIDATION, "Fix the highlighted script problems before approving.",
                       fields=[{"field": i["path"], "message": i["message"]} for i in row.issues
                               if i.get("severity") == "error"][:20])  # fmt: skip
    panels = sum(len(p.panels) for p in script.pages)
    # Spending gate: the full illustration price is held now, charged per delivered panel.
    await credits.reserve(db, project.owner_id, panels * s.credits_per_panel, purpose="panels",
                          project_id=project.id,
                          idempotency_key=f"panels:{project.id}:{version}")  # fmt: skip
    await record_approval(db, project.id, "script", version, user_id)
    await events.emit(db, project.id, "script.approved", {"script_version": version})
    await outbox.enqueue(db, "signal", f"approve:script:{project.id}:{version}", {
        "workflow_id": project.workflow_id, "signal": "approve",
        "input": {"id": f"script:{project.id}:{version}", "kind": "script", "version": version}})  # fmt: skip


# ------------------------------------------------------------------ cancel / resume / delete
async def cancel(db: AsyncSession, project: Project) -> None:
    if project.status not in [x.value for x in ACTIVE]:
        raise invalid_state("This comic isn't running.")
    await outbox.enqueue(db, "cancel", f"cancel:{project.workflow_id}", {
        "workflow_id": project.workflow_id})  # fmt: skip
    await events.emit(db, project.id, "project.cancelling", {})


RESUME_STATUS = {"intake": S.QUEUED, "planning": S.PLANNING, "cast_gate": S.AWAITING_CAST_APPROVAL,
                 "scripting": S.SCRIPTING, "script_gate": S.AWAITING_SCRIPT_APPROVAL,
                 "illustrating": S.ILLUSTRATING}  # fmt: skip


async def resume(db: AsyncSession, project: Project, key: str) -> None:
    """Restart a failed, expired or partially finished comic from its last durable stage."""
    s = get_settings()
    stage = project.resume_from
    if project.status == S.PARTIALLY_COMPLETED.value:
        stage = "illustrating"
    elif project.status not in (S.FAILED.value, S.EXPIRED.value) or stage not in RESUME_STATUS:
        raise invalid_state("This comic can't be resumed.")
    run = (await db.execute(select(func.count()).select_from(Job).where(
        Job.project_id == project.id, Job.stage == "resume"))).scalar_one() + 1  # fmt: skip
    # Re-hold credits for what is still to do. Earlier charges carry dedupe keys, so work
    # already paid for is never charged again.
    order = list(RESUME_STATUS)
    if order.index(stage) <= order.index("scripting"):
        await credits.reserve(db, project.owner_id, plan_credits(), purpose="plan",
                              project_id=project.id, idempotency_key=f"plan:{project.id}:r{run}")  # fmt: skip
    if stage == "illustrating":
        todo = (await db.execute(select(func.count()).select_from(Panel).where(
            Panel.project_id == project.id,
            Panel.status.not_in([PanelStatus.OK.value, PanelStatus.NEEDS_ATTENTION.value])))).scalar_one()  # fmt: skip
        if todo == 0:
            raise invalid_state("Every panel is already drawn.")
        await credits.reserve(db, project.owner_id, todo * s.credits_per_panel, purpose="panels",
                              project_id=project.id, idempotency_key=f"panels:{project.id}:r{run}")  # fmt: skip
    project.workflow_id = f"comic-{project.id}-r{run}"
    project.failure, project.resume_from = None, None
    await repo.job_upsert(db, project_id=project.id, workflow_id=project.workflow_id, stage="resume",
                          target_id=stage, status="running")  # fmt: skip
    await repo.set_status(db, project, RESUME_STATUS[stage], resumed=True)
    await outbox.enqueue(db, "start_workflow", f"start:{project.workflow_id}", {
        "workflow": "ComicWorkflow", "workflow_id": project.workflow_id,
        "input": workflow_input(project, resume_from=stage, script_version=project.script_version,
                                run_key=f"run{run}")})  # fmt: skip


async def soft_delete(db: AsyncSession, project: Project) -> None:
    if project.status in [x.value for x in ACTIVE]:
        await outbox.enqueue(db, "cancel", f"cancel:{project.workflow_id}", {
            "workflow_id": project.workflow_id})  # fmt: skip
    project.deleted_at = datetime.now(UTC)
    await credits.close_for_project(db, project.id)


# ------------------------------------------------------------------ read model
def _layout_out(t: Any) -> api.LayoutOut:
    return api.LayoutOut(id=t.id, panel_count=t.panel_count, page=list(t.page),
                         rects=[list(r) for r in t.rects])  # fmt: skip


def summary(project: Project, cover_key: str | None = None) -> dict[str, Any]:
    return {"id": str(project.id), "title": project.title, "status": project.status,
            "format": project.format, "style_preset": project.style_preset,
            "pages_requested": project.pages_requested, "created_at": project.created_at,
            "updated_at": project.updated_at, "cover_url": _url(cover_key),
            "failure": _public_failure(project.failure), "image_provider": project.image_provider,
            "image_model": project.image_model}  # fmt: skip


PROVIDER_LABELS = {"openai": "OpenAI", "gemini": "Google Gemini", "fal": "fal"}


def image_provider_options() -> list[api.ImageProviderOut]:
    """Every image provider the product knows, with whether this deployment can use it and why not."""
    s = get_settings()
    out = []
    for pid in ("openai", "gemini", "fal"):
        reason = None
        if pid == "fal" and not s.fal_enabled:
            reason = "fal is disabled on this server."
        elif pid not in s.image_providers:
            reason = f"{PROVIDER_LABELS[pid]} is not enabled on this server."
        elif s.provider_mode == "live" and pid != "fal" and getattr(s, f"{pid}_api_key") is None:
            reason = f"No {PROVIDER_LABELS[pid]} API key is configured on the server."
        caps = ["generate", "reference_edit"] + (["mask_inpaint"] if pid == "fal" else [])
        out.append(api.ImageProviderOut(id=pid, label=PROVIDER_LABELS[pid], model=s.image_model_for(pid),
                                        available=reason is None, reason=reason, capabilities=caps))  # fmt: skip
    return out


def choose_image_provider(requested: str | None) -> tuple[str, str]:
    """The provider and model a new comic is created with (stored on the comic for its lifetime)."""
    s = get_settings()
    choice = requested or s.image_provider_default
    option = next(o for o in image_provider_options() if o.id == choice)
    if not option.available or option.model is None:
        raise AppError(ErrorCode.VALIDATION, option.reason or f"{option.label} is not available.",
                       fields=[{"field": "image_provider", "message": option.reason or "Choose another provider."}])  # fmt: skip
    return choice, option.model


def _public_failure(failure: dict[str, Any] | None) -> dict[str, Any] | None:
    if not failure:
        return None
    return {k: failure.get(k) for k in ("code", "message", "suggestion", "during") if failure.get(k)}


async def list_projects(db: AsyncSession, user_id: uuid.UUID) -> list[api.ProjectSummary]:
    rows = (await db.execute(
        select(Project, Asset.storage_key)
        .outerjoin(Page, (Page.project_id == Project.id) & (Page.index == 0))
        .outerjoin(Asset, Asset.id == Page.preview_asset_id)
        .where(Project.owner_id == user_id, Project.deleted_at.is_(None))
        .order_by(Project.created_at.desc()).limit(100))).all()  # fmt: skip
    return [api.ProjectSummary(**summary(p, key)) for p, key in rows]


async def serialize(db: AsyncSession, project: Project) -> api.ProjectOut:
    await db.flush()
    await db.refresh(project)  # server-side defaults and counters changed in this transaction
    pid = project.id
    bible = await repo.latest_bible(db, pid)
    # characters
    ref_rows = (await db.execute(
        select(Character.slug, CharacterRef.view, Asset.storage_key, Asset.quarantined)
        .join(CharacterRef, CharacterRef.character_id == Character.id)
        .join(Asset, Asset.id == CharacterRef.asset_id)
        .where(Character.project_id == pid, CharacterRef.sheet_version == Character.sheet_version,
               CharacterRef.view.in_(("portrait", "turnaround_sheet", "expression_sheet"))))).all()  # fmt: skip
    images: dict[str, dict[str, str]] = {}
    for slug, view, key, quarantined in ref_rows:
        url = _url(key, quarantined)
        if url:
            images.setdefault(slug, {})[view] = url
    characters = [
        api.CharacterOut(slug=c.slug, name=c.name, spec=c.spec, sheet_status=c.sheet_status,  # type: ignore[arg-type]
                         sheet_version=c.sheet_version, revising=bool(c.pending_revision),
                         images=images.get(c.slug, {}))
        for c in await repo.cast_rows(db, pid)]  # fmt: skip
    loc_rows = (await db.execute(
        select(Location, Asset.storage_key).outerjoin(Asset, Asset.id == Location.reference_asset_id)
        .where(Location.project_id == pid).order_by(Location.slug))).all()  # fmt: skip
    locations = [api.LocationOut(slug=loc.slug, name=loc.spec.get("name", loc.slug),
                                 description=loc.spec.get("description", ""), image_url=_url(key))
                 for loc, key in loc_rows]  # fmt: skip
    script_out = None
    row = await repo.script_row(db, pid, project.script_version or None)
    if row is not None:
        script_out = api.ScriptOut(version=row.version, source=row.source, beats=row.beats,  # type: ignore[arg-type]
                                   content=row.content, issues=row.issues,  # type: ignore[arg-type]
                                   has_errors=any(i.get("severity") == "error" for i in row.issues))  # fmt: skip
    approved = dict((await db.execute(
        select(Approval.kind, func.max(Approval.content_version))
        .where(Approval.project_id == pid, Approval.status == "approved").group_by(Approval.kind))).all())  # fmt: skip

    # pages / panels / balloons
    version_rows = (await db.execute(
        select(Panel.id, PanelVersion.qa_scores, Asset.storage_key, Asset.quarantined)
        .join(PanelVersion, PanelVersion.id == Panel.current_version_id)
        .join(Asset, Asset.id == PanelVersion.image_asset_id).where(Panel.project_id == pid))).all()  # fmt: skip
    current = {r[0]: r for r in version_rows}
    balloons: dict[uuid.UUID, list[api.BalloonOut]] = {}
    for b in (await db.execute(select(Balloon).where(Balloon.project_id == pid)
                               .order_by(Balloon.order_index))).scalars():  # fmt: skip
        balloons.setdefault(b.panel_id, []).append(balloon_out(b))
    busy = set((await db.execute(select(Job.target_id).where(
        Job.project_id == pid, Job.stage == "panel", Job.status == "running"))).scalars())  # fmt: skip
    preview_keys = dict((await db.execute(
        select(Page.id, Asset.storage_key).join(Asset, Asset.id == Page.preview_asset_id)
        .where(Page.project_id == pid))).all())  # fmt: skip
    fmt = ComicFormat(project.format)
    pages_out: list[api.PageOut] = []
    total = done = 0
    for page, panels in await repo.pages_with_panels(db, pid):
        template = layouts()[page.layout_template_id]
        panel_out = []
        for panel in panels:
            total += 1
            done += panel.status in (PanelStatus.OK.value, PanelStatus.NEEDS_ATTENTION.value,
                                     PanelStatus.BLOCKED.value, PanelStatus.FAILED.value)  # fmt: skip
            cur = current.get(panel.id)
            visible = cur is not None and panel.status != PanelStatus.BLOCKED.value
            panel_out.append(api.PanelOut(
                id=str(panel.id), index=panel.index, rect=list(panel_rect(page, panel)),
                status=panel.status, spec=panel.spec,
                current_version_id=str(panel.current_version_id) if panel.current_version_id else None,
                image_url=_url(cur[2], cur[3]) if visible and cur else None,
                qa_problems=((cur[1] or {}).get("problems", []) if cur else []),
                balloons=balloons.get(panel.id, []),
                busy=str(panel.id) in busy or panel.status == PanelStatus.GENERATING.value))  # fmt: skip
        options = [_layout_out(t) for t in sorted(layouts().values(), key=lambda t: t.id)
                   if t.format is fmt and t.panel_count == len(panels)]  # fmt: skip
        pages_out.append(api.PageOut(
            id=str(page.id), index=page.index, layout_template_id=page.layout_template_id,
            size=list(template.page), status=page.status, preview_url=_url(preview_keys.get(page.id)),
            layout_options=options, panels=panel_out))  # fmt: skip
    exports = await list_exports(db, pid)
    cast_rows = await repo.cast_rows(db, pid)
    cover = preview_keys.get(uuid.UUID(pages_out[0].id)) if pages_out else None
    return api.ProjectOut(
        **summary(project, cover), prompt=project.prompt, brief=project.brief,  # type: ignore[arg-type]
        questions=project.questions or [], bible=bible, cast_version=project.cast_version,
        script_version=project.script_version, edit_version=project.edit_version,
        event_seq=project.event_seq, approved_cast_version=approved.get("cast"),
        approved_script_version=approved.get("script"), resume_from=project.resume_from,
        characters=characters, locations=locations, script=script_out, pages=pages_out,
        exports=exports,
        progress=api.ProgressOut(
            panels_total=total, panels_done=done, pages_total=len(pages_out),
            pages_ready=sum(1 for p in pages_out if p.status == "ready"),
            characters_total=len(cast_rows),
            characters_ready=sum(1 for c in cast_rows if c.sheet_status == "ready")))  # fmt: skip


def balloon_out(b: Balloon) -> api.BalloonOut:
    return api.BalloonOut(id=str(b.id), panel_id=str(b.panel_id), order_index=b.order_index,
                          type=b.type, speaker_id=b.speaker_id, text=b.text, geometry=b.geometry,
                          flags=b.flags, user_edited=b.user_edited, version=b.version)  # fmt: skip


async def list_exports(db: AsyncSession, project_id: uuid.UUID) -> list[api.ExportOut]:
    rows = (await db.execute(
        select(Export, Asset.storage_key).outerjoin(Asset, Asset.id == Export.asset_id)
        .where(Export.project_id == project_id).order_by(Export.created_at.desc()).limit(20))).all()  # fmt: skip
    out = []
    for e, key in rows:
        title = re.sub(r"[^A-Za-z0-9 _-]", "", e.revision.get("title", "comic")).strip() or "comic"
        ext = key.rsplit(".", 1)[-1] if key else e.format
        url = storage.signed_url(key, filename=f"{title}.{ext}") if key and e.status == "ready" else None
        out.append(api.ExportOut(id=str(e.id), format=e.format, status=e.status,
                                 created_at=e.created_at, download_url=url, error=e.error,
                                 edit_version=e.revision.get("edit_version", 0),
                                 print_quality=e.print_quality))  # fmt: skip
    return out
