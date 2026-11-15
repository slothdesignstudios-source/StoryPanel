"""Project lifecycle operations behind the API: create, gates, edits, cancel, resume."""

from __future__ import annotations

import hashlib
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
from storypanel.domain.schemas import BeatSheet, ComicFormat, ComicScript, ContinuityRule, StoryBible, slugify
from storypanel.domain.states import ACTIVE, PanelStatus
from storypanel.domain.states import ProjectStatus as S
from storypanel.domain.validation import has_errors, validate_script
from storypanel.errors import AppError, ErrorCode, invalid_state, not_found, version_conflict
from storypanel.obs import metrics
from storypanel.pipeline import lettering, moderation, qa, story
from storypanel.pipeline.stages_art import panel_rect
from storypanel.pipeline.stages_plan import initial_consistency, record_approval
from storypanel.services import credits, events, outbox, repo


def workflow_input(project: Project, **extra: Any) -> dict[str, Any]:
    s = get_settings()
    return {"project_id": str(project.id), "approval_ttl_days": s.approval_ttl_days,
            "answers_ttl_days": s.answers_ttl_days, "max_qa_retries": s.max_panel_qa_retries,
            **extra}  # fmt: skip


PANEL_CHARGE_POLICY = (
    "Each delivered panel is charged once, including panels our checks flag as needing attention. "
    "Automatic quality retries are free; panels that fail or are blocked by the safety check are "
    "not charged, and their credits are released."
)


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
    # The idea is planning input: it is never lettered itself, so characters the fonts cannot draw
    # are allowed here (the page shows a warning). Font coverage is enforced where text is
    # rendered: script dialogue (validate_script) and balloon edits. Nothing is transliterated.
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
            note=f"{s.credits_cast} are charged as soon as the cast sheets are drawn, before you review them, "
                 f"and {s.credits_script} when the script is written. Anything not used is released. "
                 f"Illustration is held later, when you approve the script: about "
                 f"{panels * s.credits_per_panel} more credits for roughly {panels} panels.")  # fmt: skip
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
        note=PANEL_CHARGE_POLICY)  # fmt: skip


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


def script_out_of(row: Script, rules: dict[str, str] | None = None) -> api.ScriptOut:
    return api.ScriptOut(version=row.version, source=row.source, beats=row.beats,  # type: ignore[arg-type]
                         content=row.content, issues=row.issues,  # type: ignore[arg-type]
                         has_errors=any(i.get("severity") == "error" for i in row.issues),
                         consistency=row.consistency, review_required=script_review(row, rules))  # fmt: skip


def _bound_key(base: str, *parts: object) -> str:
    """A review key bound to the content it confirms (see api.ReviewItem)."""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:12]
    return f"{base}#{digest}"


REVIEW_KINDS = {
    "rule_uncertain": "Couldn't confirm a continuity rule is kept",
    "beat_not_expressed": "Panel doesn't seem to show its beat",
    "beat_uncertain": "Couldn't confirm the panel shows its beat",
    "rule_conflict": "Continuity rules conflict",
    "consistency_incomplete": "Panel wasn't checked against the Story Bible",
    "consistency_unavailable": "Script wasn't checked against the Story Bible",
}
_ISSUE_PATH = re.compile(r"^pages\[(\d+)\]\.panels\[(\d+)\](?:\.rule:([a-z0-9_]+)|\.beat:(.+))?$")


def sheet_review(c: Character, cast_version: int = 0) -> api.ReviewItem | None:
    """What approving the cast acknowledges for this character's current sheet, if anything."""
    check = c.sheet_check or {}
    if check.get("sheet_version") != c.sheet_version or check.get("status") not in ("flagged", "unavailable"):
        return None
    if check["status"] == "unavailable":
        msg = f"{c.name}'s reference sheet wasn't reviewed (the checking service didn't answer)."
    else:
        bits = [
            f"{p['item']}: {p.get('note') or 'not drawn as described'}" for p in check.get("problems", [])
        ][:3]
        bits += [f"{u['item']}: uncertain" for u in check.get("uncertain", [])][:2]
        if check.get("whole_figures") == "no" or check.get("layout_unverified"):
            bits.append("a turnaround view may be cut off")
        msg = f"{c.name}'s reference sheet was flagged: " + "; ".join(bits or ["see the sheet review"]) + "."
    kind = "Reference sheet not reviewed" if check["status"] == "unavailable" else "Reference sheet flagged"
    return api.ReviewItem(key=_bound_key(f"sheet:{c.slug}:v{c.sheet_version}", cast_version, check.get("checked_at"), msg),
                          message=msg, kind=kind, character=c.name, issue=msg.split(": ", 1)[-1])  # fmt: skip


def script_review(row: Script, rules: dict[str, str] | None = None) -> list[api.ReviewItem]:
    """Review-level items of one script version that approval must acknowledge explicitly, each
    labelled with page, panel, rule or beat, and keyed to this version and check result."""
    rules = rules or {}
    checked = (row.consistency or {}).get("checked_at")
    items = []
    for i in row.issues:
        if i.get("severity") != "review":
            continue
        m = _ISSUE_PATH.match(i["path"])
        page, panel, rule_id, beat = (
            (int(m[1]) + 1, int(m[2]) + 1, m[3], m[4]) if m else (None, None, None, None)
        )
        message = i["message"]
        issue = message.split(": ", 1)[1] if m and ": " in message else message
        items.append(api.ReviewItem(
            key=_bound_key(f"{i['code']}@{i['path']}", row.version, checked, message), message=message,
            kind=REVIEW_KINDS.get(i["code"], i["code"].replace("_", " ")), page=page, panel=panel,
            rule_id=rule_id, rule_text=rules.get(rule_id or ""), beat=beat, issue=issue))  # fmt: skip
    if row.consistency is None:  # written before the check existed: not checked
        msg = "This version wasn't checked against the Story Bible (it was written before the check existed)."
        items.append(api.ReviewItem(key=_bound_key("consistency_unavailable@script", row.version, msg), message=msg,
                                    kind=REVIEW_KINDS["consistency_unavailable"], issue=msg))  # fmt: skip
    return items


def _acknowledge(required: list[api.ReviewItem], acknowledged: list[str], what: str) -> list[dict[str, str]]:
    """Every current review item must be confirmed by its exact key. Keys from an older version or
    check result never match, so a stale confirmation cannot approve changed content."""
    missing = [r for r in required if r.key not in set(acknowledged)]
    if missing:
        stale = bool(set(acknowledged) - {r.key for r in required})
        raise AppError(ErrorCode.VALIDATION,
                       ("These confirmations are out of date: the content or its check changed. Review the items "
                        f"again to approve {what}.") if stale else
                       f"Review the flagged items and confirm to approve {what} anyway.",
                       fields=[{"field": "acknowledged", "message": r.message} for r in missing][:20])  # fmt: skip
    return [{"key": r.key, "message": r.message} for r in required]


RULE_EDIT_STATUSES = (S.AWAITING_CAST_APPROVAL.value, S.COMPLETED.value, S.PARTIALLY_COMPLETED.value)


async def put_continuity_rules(db: AsyncSession, project: Project, body: api.PutContinuityRules) -> None:
    """Replace the comic's continuity rules (a new Story Bible version).

    Allowed before the cast is approved (approvals are bound to the new version) and after the
    comic is drawn (rules then apply to later redraws and edits). A rule classified from a world
    rule keeps that rule's exact wording: the server refuses a changed text, so nothing is invented.
    """
    if project.status not in RULE_EDIT_STATUSES:
        raise invalid_state(
            "Continuity rules can be changed before the cast is approved, or once the comic is drawn."
        )
    if body.cast_version != project.cast_version:
        raise version_conflict("The cast", project.cast_version)
    bible = await repo.latest_bible(db, project.id)
    assert bible is not None
    cast_ids = set(bible.cast)
    loc_ids = {loc.id for loc in bible.locations}
    existing = {r.id: r for r in bible.continuity_rules}
    rules: list[ContinuityRule] = []
    used: set[str] = set()
    problems: list[dict[str, str]] = []
    for n, r in enumerate(body.rules):
        field = f"rules.{n}"
        if r.applies_to == "every_panel" and r.target:
            problems.append({"field": f"{field}.target", "message": "A rule for every panel has no target."})
        if r.applies_to == "character" and r.target not in cast_ids:
            problems.append({"field": f"{field}.target", "message": "Choose a character from the cast."})
        if r.applies_to == "location" and r.target not in loc_ids:
            problems.append({"field": f"{field}.target", "message": "Choose one of the comic's locations."})
        source = r.source
        if source == "world_rule":
            idx = r.world_rule_index
            if idx is None or idx >= len(bible.world_rules) or bible.world_rules[idx] != r.text:
                problems.append({"field": f"{field}.text",
                                 "message": "A classified world rule keeps its original wording. Add a new rule to "
                                            "word it differently."})  # fmt: skip
        elif source == "model" and (r.id not in existing or existing[r.id].text != r.text):
            source = "user"  # edited by the user
        base = slugify(r.id or r.text)[:34]
        rid, k = base, 2
        while rid in used:
            rid, k = f"{base}_{k}", k + 1
        used.add(rid)
        rules.append(ContinuityRule(id=rid, text=r.text, applies_to=r.applies_to,
                                    target=r.target if r.applies_to != "every_panel" else None,
                                    strength=r.strength, visual=r.visual, source=source,
                                    world_rule_index=r.world_rule_index if source == "world_rule" else None))  # fmt: skip
    if problems:
        raise AppError(ErrorCode.VALIDATION, "Some continuity rules need changes.", fields=problems[:25])
    verdict = moderation.rule_check(" ".join(r.text for r in rules))
    if verdict is not None:
        raise AppError(ErrorCode.MODERATION_BLOCKED, verdict.user_message)
    updated = bible.model_copy(update={"continuity_rules": rules})
    version = (await db.execute(select(func.max(StoryBibleRow.version)).where(
        StoryBibleRow.project_id == project.id))).scalar_one()  # fmt: skip
    db.add(StoryBibleRow(project_id=project.id, version=version + 1,
                         content=updated.model_dump(mode="json"), source="user"))  # fmt: skip
    if project.status == S.AWAITING_CAST_APPROVAL.value:
        project.cast_version += 1  # approval covers the rules too
        await repo.invalidate_approvals(db, project.id, "cast")
    await events.emit(db, project.id, "bible.updated", {"cast_version": project.cast_version})


async def approve_cast(db: AsyncSession, project: Project, version: int, user_id: uuid.UUID,
                       acknowledged: list[str] | None = None) -> None:  # fmt: skip
    if project.status != S.AWAITING_CAST_APPROVAL.value:
        if await repo.approval_is_current(db, project.id, "cast", version):
            return  # duplicate approval of an already-approved version: idempotent success
        raise invalid_state("The cast isn't waiting for approval.")
    if version != project.cast_version:
        raise version_conflict("The cast", project.cast_version)
    cast = await repo.cast_rows(db, project.id)
    if any(c.sheet_status != "ready" or c.pending_revision for c in cast):
        raise AppError(ErrorCode.CONFLICT, "Wait for all character sheets to finish first.")
    if any((c.sheet_check or {}).get("status") == "pending" and (c.sheet_check or {}).get("sheet_version")
           == c.sheet_version for c in cast):  # fmt: skip
        raise AppError(ErrorCode.CONFLICT, "Still reviewing the reference sheets. Try again in a moment.")
    overrides = _acknowledge([r for c in cast if (r := sheet_review(c, project.cast_version))], acknowledged or [],
                             "the cast")  # fmt: skip
    await record_approval(db, project.id, "cast", version, user_id, overrides=overrides or None)
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
        location_ids={loc.id for loc in bible.locations}, limits=story.script_limits(bible),
        beats=BeatSheet.model_validate(current.beats) if current.beats else None)  # fmt: skip
    project.script_version += 1
    row = Script(project_id=project.id, version=project.script_version, beats=current.beats,
                 content=body.script.model_dump(), issues=[i.model_dump() for i in issues],
                 source="user", consistency=initial_consistency(body.script, bible))  # fmt: skip
    db.add(row)
    await repo.invalidate_approvals(db, project.id, "script")
    if row.consistency["status"] == "pending" and not has_errors(issues):  # type: ignore[index]
        # Checked against the Story Bible by the workflow (metered, retried); approval waits for it.
        await outbox.enqueue(db, "signal", f"check:{project.id}:{project.script_version}", {
            "workflow_id": project.workflow_id, "signal": "check_script",
            "input": {"id": f"check:{project.id}:{project.script_version}", "version": project.script_version}})  # fmt: skip
    elif row.consistency["status"] == "pending":  # type: ignore[index]
        row.consistency = {"status": "skipped", "reason": "fix the listed errors first"}
    await events.emit(db, project.id, "script.updated", {
        "script_version": project.script_version, "has_errors": has_errors(issues)})  # fmt: skip
    return row


async def approve_script(db: AsyncSession, project: Project, version: int, user_id: uuid.UUID,
                         acknowledged: list[str] | None = None) -> None:  # fmt: skip
    s = get_settings()
    if project.status != S.AWAITING_SCRIPT_APPROVAL.value:
        if await repo.approval_is_current(db, project.id, "script", version):
            return
        raise invalid_state("The script isn't waiting for approval.")
    if version != project.script_version:
        raise version_conflict("The script", project.script_version)
    rejected = (await db.execute(select(Approval.id).where(
        Approval.project_id == project.id, Approval.kind == "script", Approval.content_version == version,
        Approval.status == "rejected"))).scalar_one_or_none()  # fmt: skip
    if rejected is not None:
        raise AppError(ErrorCode.VALIDATION, "This version didn't pass the content check. Edit the script "
                       "and save it as a new version before approving.")  # fmt: skip
    row = await repo.script_row(db, project.id, version)
    assert row is not None
    script = ComicScript.model_validate(row.content)
    if any(i.get("severity") == "error" for i in row.issues):
        raise AppError(ErrorCode.VALIDATION, "Fix the highlighted script problems before approving.",
                       fields=[{"field": i["path"], "message": i["message"]} for i in row.issues
                               if i.get("severity") == "error"][:20])  # fmt: skip
    if (row.consistency or {}).get("status") == "pending":
        raise AppError(
            ErrorCode.CONFLICT, "Still checking this version against the Story Bible. Try again in a moment."
        )
    bible = await repo.latest_bible(db, project.id)
    rules = {r.id: r.text for r in (bible.continuity_rules if bible else [])}
    overrides = _acknowledge(script_review(row, rules), acknowledged or [], "this script")
    panels = sum(len(p.panels) for p in script.pages)
    # Spending gate: the full illustration price is held now, charged per delivered panel.
    await credits.reserve(db, project.owner_id, panels * s.credits_per_panel, purpose="panels",
                          project_id=project.id,
                          idempotency_key=f"panels:{project.id}:{version}")  # fmt: skip
    await record_approval(db, project.id, "script", version, user_id, overrides=overrides or None)
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


def summary(
    project: Project, cover_key: str | None = None, counts: dict[str, int] | None = None
) -> dict[str, Any]:
    return {"id": str(project.id), "title": project.title, "status": project.status,
            "panel_counts": counts or {},
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


def _qa_fields(cur: Any, names: dict[str, str], rules: dict[str, str]) -> dict[str, Any]:
    if cur is None:
        return {"qa_state": None, "qa_reasons": []}
    state, reasons = qa.qa_reasons(cur[4], (cur[1] or {}).get("problems", []), names=names, rules=rules)
    return {"qa_state": state, "qa_reasons": reasons}


async def panel_counts(db: AsyncSession, project_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, int]]:
    rows = (await db.execute(select(Panel.project_id, Panel.status, func.count()).where(
        Panel.project_id.in_(project_ids)).group_by(Panel.project_id, Panel.status))).all()  # fmt: skip
    out: dict[uuid.UUID, dict[str, int]] = {}
    for pid, status, n in rows:
        out.setdefault(pid, {})[status] = n
    return out


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
    counts = await panel_counts(db, [p.id for p, _ in rows])
    return [api.ProjectSummary(**summary(p, key, counts.get(p.id))) for p, key in rows]


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
                         images=images.get(c.slug, {}),
                         sheet_check=c.sheet_check if (c.sheet_check or {}).get("sheet_version") == c.sheet_version else None,
                         review=sheet_review(c, project.cast_version))
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
        script_out = script_out_of(row, {r.id: r.text for r in (bible.continuity_rules if bible else [])})
    approved = dict((await db.execute(
        select(Approval.kind, func.max(Approval.content_version))
        .where(Approval.project_id == pid, Approval.status == "approved").group_by(Approval.kind))).all())  # fmt: skip

    # pages / panels / balloons
    version_rows = (await db.execute(
        select(Panel.id, PanelVersion.qa_scores, Asset.storage_key, Asset.quarantined, PanelVersion.qa_status)
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
    names = {c.slug: c.name for c in await repo.cast_rows(db, pid)}
    rule_text = {r.id: r.text for r in (bible.continuity_rules if bible else [])}
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
                **_qa_fields(cur, names, rule_text),
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
        **summary(project, cover, (await panel_counts(db, [project.id])).get(project.id)),
        prompt=project.prompt, brief=project.brief,  # type: ignore[arg-type]
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
    flags = list(b.flags or [])
    if b.type != "sfx" and "overflow" not in flags and lettering.below_readable(b.geometry or {}):
        flags.append("overflow")  # set below the readable floor before it existed: shown, not re-shrunk
    return api.BalloonOut(id=str(b.id), panel_id=str(b.panel_id), order_index=b.order_index,
                          type=b.type, speaker_id=b.speaker_id, text=b.text, geometry=b.geometry,
                          flags=flags, user_edited=b.user_edited, version=b.version)  # fmt: skip


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
