"""Draw-phase stages: page planning, panel generation, QA, lettering, finish and export."""

from __future__ import annotations

import hashlib
import io
import json
import math
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update

from storypanel import storage
from storypanel.catalog import PROMPT_VERSION, layouts, styles
from storypanel.config import get_settings
from storypanel.db.models import (
    Asset,
    Balloon,
    Character,
    CharacterRef,
    Export,
    Page,
    Panel,
    PanelVersion,
    Project,
)  # fmt: skip
from storypanel.db.session import transaction
from storypanel.domain.schemas import AgeRating, ComicFormat, PanelSpec
from storypanel.domain.states import PanelStatus
from storypanel.domain.states import ProjectStatus as S
from storypanel.errors import AppError, ErrorCode
from storypanel.obs import metrics
from storypanel.pipeline import art, consistency, lettering, moderation, qa, render, repair
from storypanel.providers import metering
from storypanel.providers.base import CallContext, ImageInput, ImageRequest
from storypanel.providers.registry import get_providers
from storypanel.services import credits, events, repo

PREVIEW_WIDTH = 1000
EXPORT_WIDTH = 2000


# ------------------------------------------------------------------ planning
async def stage_plan_pages(project_id: uuid.UUID, script_version: int) -> dict[str, Any]:
    """Create page and panel rows from the approved script, with deterministic layouts.
    Returns the work plan: per page, chains of panels that must be drawn in order."""
    async with transaction() as db:
        project = await repo.get_project(db, project_id, lock=True)
        if (project.status == S.AWAITING_SCRIPT_APPROVAL.value
                and not await repo.approval_is_current(db, project_id, "script", script_version)):  # fmt: skip
            # A newer save (or a rejection) landed after the approval was checked: never draw a
            # version that is no longer the approved one. Release its hold; back to the gate.
            from storypanel.pipeline.stages_plan import release_panel_hold

            await release_panel_hold(db, project_id, script_version)
            return {"stale": True, "pages": []}
        existing = await repo.pages_with_panels(db, project_id)
        if not existing:
            row = await repo.script_row(db, project_id, script_version)
            assert row is not None
            script = repo.parse_script(row)
            fmt = ComicFormat(project.format)
            for pi, page_spec in enumerate(script.pages):
                template = art.choose_template(fmt, page_spec)
                page = Page(id=uuid.uuid4(), project_id=project_id, index=pi,
                            layout_template_id=template.id)  # fmt: skip
                db.add(page)
                for ni, panel_spec in enumerate(page_spec.panels):
                    db.add(Panel(project_id=project_id, page_id=page.id, index=ni,
                                 spec=panel_spec.model_dump()))  # fmt: skip
            await db.flush()
            existing = await repo.pages_with_panels(db, project_id)
        if project.status == S.AWAITING_SCRIPT_APPROVAL.value:
            project.script_approved_at = datetime.now(UTC)
            total = sum(len(p) for _, p in existing)
            await repo.set_status(db, project, S.ILLUSTRATING, pages=len(existing), panels=total)
        await events.notify_after_commit(db)
        plan = []
        for page, panels in existing:
            chains: list[list[str]] = []
            last_loc = None
            for panel in panels:
                # Consecutive panels in one location form a chain: each uses the previous
                # panel as a continuity reference, so they are drawn in order. Chains are
                # independent of each other and run in parallel.
                if panel.status in (PanelStatus.OK.value, PanelStatus.NEEDS_ATTENTION.value):
                    last_loc = None  # already drawn (resumed run): nothing to do
                    continue
                loc = panel.spec["location_id"]
                if loc == last_loc and chains:
                    chains[-1].append(str(panel.id))
                else:
                    chains.append([str(panel.id)])
                last_loc = loc
            plan.append({"page_id": str(page.id), "index": page.index, "chains": chains})
        return {"pages": plan}


# ------------------------------------------------------------------ panels
async def _panel_context(db: Any, panel_id: uuid.UUID) -> dict[str, Any]:
    panel = (await db.execute(select(Panel).where(Panel.id == panel_id))).scalar_one()
    page = (await db.execute(select(Page).where(Page.id == panel.page_id))).scalar_one()
    project = await repo.get_project(db, panel.project_id)
    return {"panel": panel, "page": page, "project": project,
            "audience": AgeRating((project.brief or {}).get("audience", "all_ages"))}  # fmt: skip


async def stage_generate_panel(
    panel_id: uuid.UUID, *, request_key: str, attempt: int, kind: str = "generate",
    fix_notes: list[str] | None = None, instruction: str | None = None, workflow_id: str = "",
) -> dict[str, Any]:  # fmt: skip
    """Produce one candidate image for a panel (no QA yet). Idempotent per attempt."""
    s = get_settings()
    key = f"panel:{panel_id}:{request_key}:a{attempt}"
    async with transaction() as db:
        done = (await db.execute(select(PanelVersion.id).where(
            PanelVersion.idempotency_key == key))).scalar_one_or_none()  # fmt: skip
        if done:
            return {"version_id": str(done)}
        c = await _panel_context(db, panel_id)
        panel, page, project = c["panel"], c["page"], c["project"]
        refusal = await metering.project_refuses_paid_work(db, project.id)
        if refusal:  # cancelled, ended or deleted: no new image, and the panel is not marked as drawing
            raise AppError(ErrorCode.INVALID_STATE, refusal, retryable=False)
        spec = PanelSpec.model_validate(panel.spec)
        cast = await repo.cast_specs(db, project.id)
        locations = await repo.location_specs(db, project.id)
        char_refs = await repo.character_ref_assets(db, project.id)
        bible_now = await repo.latest_bible(db, project.id)
        rules = bible_now.continuity_rules if bible_now else []
        loc_refs = {r.slug: str(r.reference_asset_id) for r in await repo.location_rows(db, project.id)
                    if r.reference_asset_id}  # fmt: skip
        template = layouts()[page.layout_template_id]
        aspect = art.panel_aspect(template, panel.index)
        preset = project.style_preset or styles().default
        continuity = None
        previous_spec: PanelSpec | None = None
        if panel.index > 0 and kind != "edit":
            prev = (await db.execute(select(Panel).where(Panel.page_id == page.id,
                                                         Panel.index == panel.index - 1))).scalar_one_or_none()  # fmt: skip
            if prev and prev.current_version_id and prev.spec["location_id"] == spec.location_id:
                asset_id = (await db.execute(select(PanelVersion.image_asset_id).where(
                    PanelVersion.id == prev.current_version_id))).scalar_one()  # fmt: skip
                continuity = (str(prev.id), str(asset_id))
                previous_spec = PanelSpec.model_validate(prev.spec)
        current_image: bytes | None = None
        if kind == "edit":
            if not panel.current_version_id:
                raise AppError(ErrorCode.INVALID_STATE, "This panel has no image to edit yet.")
            asset_id = (await db.execute(select(PanelVersion.image_asset_id).where(
                PanelVersion.id == panel.current_version_id))).scalar_one()  # fmt: skip
            current_image = await repo.asset_bytes(db, asset_id)
        # Targeted repair: if the previous attempt failed QA only locally (one character),
        # repaint that region instead of paying for, and re-rolling, the whole panel.
        repair_of: tuple[bytes, dict[str, Any]] | None = None
        # Mask inpainting is a fal capability; comics drawn with OpenAI or Gemini repair by
        # reference edit (instruction edit + local compositing), never by a provider mask.
        repair_available = s.repair_mode == "reference_edit" or (
            s.repair_mode == "inpaint" and project.image_provider in (None, "fal")
            and (s.inpaint_model or s.provider_mode == "fake"))  # fmt: skip
        if attempt > 0 and kind != "edit" and repair_available:
            prev_version = (await db.execute(select(PanelVersion).where(
                PanelVersion.idempotency_key == f"panel:{panel_id}:{request_key}:a{attempt - 1}"))).scalar_one_or_none()  # fmt: skip
            target = (prev_version.qa_scores or {}).get("repair") if prev_version else None
            if prev_version is not None and prev_version.qa_status == "fail" and target:
                repair_of = (await repo.asset_bytes(db, prev_version.image_asset_id), target)
        spec_version, owner, project_id = panel.spec_version, project.owner_id, project.id
        # The comic's own provider and model, for the first image, every repair and every edit.
        choice, image_model = project.image_provider, project.image_model
        if attempt == 0:
            panel.status = PanelStatus.GENERATING.value
            await events.emit(db, project_id, "panel.generating", {"panel_id": str(panel_id)})
            await repo.job_upsert(db, project_id=project_id, workflow_id=workflow_id, stage="panel",
                                  target_id=str(panel_id), status="running")  # fmt: skip
        await events.notify_after_commit(db)

    ctx = CallContext("panel_generate", str(owner), str(project_id), target_id=str(panel_id),
                      attempt=attempt, prompt_version=PROMPT_VERSION)  # fmt: skip
    seed = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % (2**31)

    mask: bytes | None = None
    used: list[dict[str, Any]]
    portrait_id = char_refs.get(repair_of[1]["character"], {}).get("portrait") if repair_of else None
    if repair_of is not None and s.repair_mode == "reference_edit" and portrait_id:
        base_image, target = repair_of
        who = cast[target["character"]]
        mask = repair.build_mask(base_image, target["box"])
        async with transaction() as db:
            portrait = await repo.asset_bytes(db, uuid.UUID(portrait_id))
        prompt = art.compile_reference_repair_prompt(who, preset, tuple(fix_notes or ()), target["box"])
        prompt_sha = hashlib.sha256(prompt.encode()).hexdigest()
        used = [{"role": "repair_base", "key": str(panel_id), "box": target["box"], "character": target["character"],
                 "mode": "reference_edit"},
                {"role": "character", "key": target["character"], "view": "portrait", "asset_id": portrait_id}]  # fmt: skip
        req = ImageRequest(prompt, aspect, (ImageInput(base_image), ImageInput(portrait)), mode="edit", seed=seed,
                           audience=c["audience"].value,
                           inputs={"kind": "repair", "character": target["character"]})  # fmt: skip
    elif repair_of is not None and choice in (None, "fal"):
        base_image, target = repair_of
        who = cast[target["character"]]
        mask = repair.build_mask(base_image, target["box"])
        prompt = art.compile_repair_prompt(who, preset, tuple(fix_notes or ()))
        prompt_sha = hashlib.sha256(prompt.encode()).hexdigest()
        used = [{"role": "repair_base", "key": str(panel_id), "box": target["box"],
                 "character": target["character"], "mode": "inpaint"}]  # fmt: skip
        req = ImageRequest(prompt, aspect, mode="inpaint", seed=seed, image=base_image, mask=mask,
                           audience=c["audience"].value)  # fmt: skip
    elif kind == "edit":
        assert instruction is not None and current_image is not None
        verdict = await moderation.moderate_text(instruction, audience=c["audience"],
                                                 surface="panel_edit", ctx=ctx)  # fmt: skip
        if not verdict.allowed:
            raise AppError(ErrorCode.MODERATION_BLOCKED, verdict.user_message)
        refs = [ImageInput(current_image)]
        names: list[tuple[str, int]] = []
        used = [{"role": "base", "key": str(panel_id)}]
        async with transaction() as db:
            for ch in spec.characters[: s.image_max_references - 1]:
                asset = char_refs.get(ch.id, {}).get("portrait")
                if asset:
                    refs.append(ImageInput(await repo.asset_bytes(db, uuid.UUID(asset))))
                    names.append((cast[ch.id].name, len(refs)))
                    used.append({"role": "character", "key": ch.id, "view": "portrait",
                                 "asset_id": asset})  # fmt: skip
        prompt = art.compile_edit_prompt(instruction, preset, names, art.rules_for_panel(rules, spec)[0])
        prompt_sha = hashlib.sha256(prompt.encode()).hexdigest()
        req = ImageRequest(prompt, aspect, tuple(refs), mode="edit", seed=seed,
                           inputs={"kind": "panel", "characters": [
                               {"position": c2.position, "palette": cast[c2.id].visual.palette}
                               for c2 in spec.characters]})  # fmt: skip
    else:
        compiled = art.compile_panel(
            panel=spec, aspect=aspect, style_preset=preset, cast=cast, locations=locations,
            character_refs=char_refs, location_refs=loc_refs, continuity=continuity,
            max_references=s.image_max_references, fix_notes=tuple(fix_notes or ()), rules=rules,
            panel_size=(float(template.rects[panel.index][2]), float(template.rects[panel.index][3])),
            previous=previous_spec,
        )  # fmt: skip
        async with transaction() as db:
            refs = [ImageInput(await repo.asset_bytes(db, uuid.UUID(slot.asset_id)))
                    for slot in compiled.references]  # fmt: skip
        used = [{"role": r.role, "key": r.key, "view": r.view, "asset_id": r.asset_id}
                for r in compiled.references]  # fmt: skip
        prompt, prompt_sha = compiled.prompt, compiled.prompt_sha256
        req = ImageRequest(prompt, aspect, tuple(refs), seed=seed, inputs=compiled.inputs,
                           audience=c["audience"].value)  # fmt: skip

    result, usage = await metering.draw(req, ctx, key=key, choice=choice, model=image_model)
    data = result.data
    if repair_of is not None and mask is not None:
        data = repair.composite(repair_of[0], storage.validate_image(result.data).data, mask)
    image = storage.validate_image(data)

    async with transaction() as db:
        asset = await repo.save_image(db, owner_id=owner, project_id=project_id, kind="panel",  # type: ignore[assignment]
                                      image=image)  # fmt: skip
        version = PanelVersion(
            id=uuid.uuid4(), panel_id=panel_id, image_asset_id=asset.id, kind=kind,  # type: ignore[union-attr]
            instruction=instruction, provider=result.provider, model=result.model,
            prompt_version=PROMPT_VERSION, prompt_sha256=prompt_sha, seed=result.seed or seed,
            references=used, spec_version=spec_version, attempt=attempt, cost_usd=usage.cost_usd,
            provider_request_id=result.request_id, idempotency_key=key,
        )  # fmt: skip
        db.add(version)
        return {"version_id": str(version.id)}


async def stage_qa_panel(version_id: uuid.UUID, attempt: int) -> dict[str, Any]:
    async with transaction() as db:
        version = (await db.execute(select(PanelVersion).where(PanelVersion.id == version_id))).scalar_one()
        if version.qa_status != "pending":
            return {"status": version.qa_status,
                    "fix_notes": (version.qa_scores or {}).get("fix_notes", [])}  # fmt: skip
        c = await _panel_context(db, version.panel_id)
        project = c["project"]
        spec = PanelSpec.model_validate(c["panel"].spec)
        cast = await repo.cast_specs(db, project.id)
        image = await repo.asset_bytes(db, version.image_asset_id)
        rows = (await db.execute(
            select(Character.slug, CharacterRef.view, CharacterRef.asset_id, CharacterRef.embedding)
            .join(CharacterRef, CharacterRef.character_id == Character.id)
            .where(Character.project_id == project.id,
                   CharacterRef.sheet_version == Character.sheet_version,
                   Character.slug.in_([ch.id for ch in spec.characters])))).all()  # fmt: skip
        bible_now = await repo.latest_bible(db, project.id)
        required = art.rules_for_panel(bible_now.continuity_rules if bible_now else [], spec)[0]
        ref_images: dict[str, bytes] = {}
        ref_vectors: dict[str, list[list[float]]] = {}
        identity_refs: dict[str, list[bytes]] = {}  # approved portrait + front view, for visual QA
        for slug, view, asset_id, embedding in rows:
            if view == "portrait":
                ref_images[slug] = await repo.asset_bytes(db, asset_id)
            if view in ("portrait", "front"):
                identity_refs.setdefault(slug, []).append(await repo.asset_bytes(db, asset_id))
            if embedding is not None:
                ref_vectors.setdefault(slug, []).append([float(x) for x in embedding])
    ctx = CallContext("panel_qa", str(project.owner_id), str(project.id),
                      target_id=str(version.panel_id), attempt=attempt)  # fmt: skip
    outcome = await qa.evaluate_panel(image=image, panel=spec, cast=cast, audience=c["audience"], required=required,
                                      reference_images=ref_images,
                                      reference_embeddings=ref_vectors, identity_references=identity_refs,
                                      elements=[(e.element, e.state) for e in spec.element_states]
                                      if version.prompt_version not in ("v1", "v2", "v3") else [],
                                      ctx=ctx)  # fmt: skip
    async with transaction() as db:
        version = (await db.execute(select(PanelVersion).where(PanelVersion.id == version_id)
                                    .with_for_update())).scalar_one()  # fmt: skip
        repeat = False
        if outcome.status == "fail" and attempt > 0 and version.idempotency_key.endswith(f":a{attempt}"):
            # The same problems survived a repair aimed at them: another paid attempt with the same
            # instructions is unlikely to help, so the panel goes to the user instead.
            prev_key = version.idempotency_key[: -len(f"a{attempt}")] + f"a{attempt - 1}"
            prev = (await db.execute(select(PanelVersion.qa_scores).where(
                PanelVersion.idempotency_key == prev_key))).scalar_one_or_none() or {}  # fmt: skip
            repeat = qa.repeated_after_repair(outcome.scores.get("problems", []), prev.get("problems", []))
        if repeat:
            outcome.scores["repeated_after_repair"] = True
        version.qa_status = outcome.status
        version.qa_scores = {**outcome.scores, "fix_notes": outcome.fix_notes,
                             "head_boxes": outcome.head_boxes, "action_box": outcome.action_box,
                             "rules_sha": consistency.rules_fingerprint(required)}  # fmt: skip
        if outcome.status == "blocked":
            # Quarantine: the asset can never be signed for download or used as a reference.
            await db.execute(update(Asset).where(Asset.id == version.image_asset_id)
                             .values(quarantined=True))  # fmt: skip
    return {"status": outcome.status, "fix_notes": outcome.fix_notes, "stop": repeat}


async def stage_finalize_panel(
    panel_id: uuid.UUID, *, version_ids: list[str], kind: str, request_key: str, intent_seq: int,
    reservation_id: str | None = None, workflow_id: str = "",
) -> dict[str, Any]:  # fmt: skip
    """Pick the result, apply it unless the user has moved on, charge, and announce it."""
    s = get_settings()
    async with transaction() as db:
        await repo.lock_project_of(db, Panel, panel_id)
        panel = (await db.execute(select(Panel).where(Panel.id == panel_id).with_for_update())).scalar_one()
        versions = list((await db.execute(select(PanelVersion).where(
            PanelVersion.id.in_([uuid.UUID(v) for v in version_ids]))
            .order_by(PanelVersion.attempt))).scalars())  # fmt: skip
        passed = [v for v in versions if v.qa_status == "pass"]
        review = [v for v in versions if v.qa_status == "review"]  # identity uncertain: a person decides
        failed = [v for v in versions if v.qa_status == "fail"]
        # QA could not finish (provider error after retries): kept for review, never counted as passed.
        unchecked = [v for v in versions if v.qa_status == "pending"]
        # Safety-blocked versions are never candidates, whatever the retry count.
        chosen = (passed[-1] if passed else review[-1] if review else failed[-1] if failed
                  else unchecked[-1] if unchecked else None)  # fmt: skip
        initial = kind == "generate"
        superseded = panel.intent_seq != intent_seq
        if chosen is not None:
            status = PanelStatus.OK if passed else PanelStatus.NEEDS_ATTENTION
            if not superseded:
                panel.current_version_id, panel.status = chosen.id, status.value
            unit = {"generate": s.credits_per_panel, "regenerate": s.credits_regenerate,
                    "edit": s.credits_edit}[kind]  # fmt: skip
            resv_id = uuid.UUID(reservation_id) if reservation_id else None
            if resv_id is None and initial:
                resv = await credits.open_reservation(db, panel.project_id, "panels")
                resv_id = resv.id if resv else None
            # A result that lands after the user cancelled is kept but never charged, whether it
            # is finalized before or after the cancellation releases the hold.
            late = (await metering.project_refuses_paid_work(db, panel.project_id)) is not None
            if resv_id and not late:
                await credits.charge(db, resv_id, unit, reason=f"Panel {kind}",
                                     dedupe_key=f"panel:{panel_id}:{request_key}",
                                     job_ref=workflow_id)  # fmt: skip
        else:
            all_blocked = bool(versions) and all(v.qa_status == "blocked" for v in versions)
            status = PanelStatus.BLOCKED if all_blocked else PanelStatus.FAILED
            if initial and not superseded:
                panel.status = status.value
            elif not superseded:  # a failed edit/regenerate leaves the previous image in place
                panel.status = (PanelStatus.OK.value if panel.current_version_id
                                else status.value)  # fmt: skip
        metrics.panel_outcomes.labels(status.value).inc()
        await repo.job_upsert(db, project_id=panel.project_id, workflow_id=workflow_id, stage="panel",
                              target_id=str(panel_id), status="succeeded" if chosen else "failed",
                              attempt=len(versions))  # fmt: skip
        await events.emit(db, panel.project_id, "panel.updated", {
            "panel_id": str(panel_id), "page_id": str(panel.page_id), "status": panel.status,
            "result": status.value, "kind": kind, "version_id": str(chosen.id) if chosen else None,
            "applied": chosen is not None and not superseded, "attempts": len(versions)})  # fmt: skip
        await events.notify_after_commit(db)
        return {"status": status.value, "applied": chosen is not None and not superseded}


async def stage_panel_failed(panel_id: uuid.UUID, *, kind: str, code: str, message: str,
                             workflow_id: str = "") -> None:  # fmt: skip
    """A panel's work ended in an error (not a QA verdict). Keep any existing image."""
    async with transaction() as db:
        await repo.lock_project_of(db, Panel, panel_id)
        panel = (await db.execute(select(Panel).where(Panel.id == panel_id).with_for_update())).scalar_one()
        if panel.current_version_id is None:
            panel.status = PanelStatus.FAILED.value
        elif panel.status == PanelStatus.GENERATING.value:
            panel.status = PanelStatus.OK.value
        metrics.panel_outcomes.labels("failed").inc()
        await repo.job_upsert(db, project_id=panel.project_id, workflow_id=workflow_id, stage="panel",
                              target_id=str(panel_id), status="failed",
                              error={"code": code, "message": message})  # fmt: skip
        await events.emit(db, panel.project_id, "panel.updated", {
            "panel_id": str(panel_id), "page_id": str(panel.page_id), "status": panel.status,
            "result": "failed", "kind": kind, "error": {"code": code, "message": message}})  # fmt: skip
        await events.notify_after_commit(db)


# ------------------------------------------------------------------ lettering
def panel_rect(page: Page, panel: Panel) -> tuple[float, float, float, float]:
    x, y, w, h = layouts()[page.layout_template_id].rects[panel.index]
    return (float(x), float(y), float(w), float(h))


async def page_doc(db: Any, page: Page, panels: list[Panel], *, watermark: bool,
                   preset: str) -> render.PageDoc:  # fmt: skip
    style = styles().get(preset)
    template = layouts()[page.layout_template_id]
    docs = []
    for panel in panels:
        image = None
        if panel.current_version_id and panel.status != PanelStatus.BLOCKED.value:
            row = (await db.execute(
                select(Asset.storage_key, Asset.quarantined)
                .join(PanelVersion, PanelVersion.image_asset_id == Asset.id)
                .where(PanelVersion.id == panel.current_version_id))).one()  # fmt: skip
            if not row[1]:
                image = await storage.get_bytes(row[0])
        balloons = list((await db.execute(select(Balloon).where(Balloon.panel_id == panel.id)
                                          .order_by(Balloon.order_index))).scalars())  # fmt: skip
        docs.append(render.PanelDoc(panel_rect(page, panel), image, tuple(
            {"type": b.type, "text": b.text, "geometry": b.geometry, "order_index": b.order_index}
            for b in balloons)))  # fmt: skip
    return render.PageDoc(template.page, tuple(docs), style.font, style.sfx_font, watermark)


async def stage_letter_page(page_id: uuid.UUID) -> dict[str, Any]:
    """Place balloons for panels that have none yet, then render the page preview.
    Balloons a user has already edited are never recomputed."""
    async with transaction() as db:
        await repo.lock_project_of(db, Page, page_id)
        page = (await db.execute(select(Page).where(Page.id == page_id).with_for_update())).scalar_one()
        project = await repo.get_project(db, page.project_id, lock=True)
        panels = list((await db.execute(select(Panel).where(Panel.page_id == page_id)
                                        .order_by(Panel.index))).scalars())  # fmt: skip
        template = layouts()[page.layout_template_id]
        preset = project.style_preset or styles().default
        flags: list[str] = []
        for panel in panels:
            has = (await db.execute(select(func.count()).select_from(Balloon).where(
                Balloon.panel_id == panel.id))).scalar_one()  # fmt: skip
            spec = PanelSpec.model_validate(panel.spec)
            if has or not spec.dialogue:
                continue
            scores: dict[str, Any] = {}
            zone = None
            if panel.current_version_id:
                row = (await db.execute(select(PanelVersion.qa_scores, PanelVersion.prompt_version, PanelVersion.kind)
                                        .where(PanelVersion.id == panel.current_version_id))).one()  # fmt: skip
                scores = row[0] or {}
                if (
                    row[1] not in ("v1", "v2") and row[2] != "edit"
                ):  # the picture was asked to leave this space free
                    _, _, rw, rh = panel_rect(page, panel)
                    plan_zone = art.lettering_plan(spec, (rw, rh)).zone
                    zone = dict(zip(("x", "y", "w", "h"), plan_zone, strict=True)) if plan_zone else None
            placed = lettering.layout_panel(
                panel_rect=panel_rect(page, panel), page_size=template.page,
                dialogue=[d.model_dump() for d in spec.dialogue],
                characters=[ch.model_dump() for ch in spec.characters],
                head_boxes=scores.get("head_boxes"), action_box=scores.get("action_box"),
                body_boxes=scores.get("body_boxes"), prop_boxes=scores.get("prop_boxes"), zone=zone,
                font=styles().get(preset).font,
            )  # fmt: skip
            for b in placed:
                flags += b.flags
                db.add(Balloon(project_id=project.id, panel_id=panel.id, order_index=b.index,
                               type=b.type, speaker_id=b.speaker, text=b.text,
                               moderated_sha=moderation.text_sha(b.text),  # moderated with the script
                               geometry=b.geometry(styles().get(preset).font), flags=b.flags))  # fmt: skip
        await db.flush()
        for flag in flags:
            metrics.lettering_flags.labels(flag).inc()
        plan = await repo.owner_plan(db, project.owner_id)
        doc = await page_doc(db, page, panels, watermark=plan == "free", preset=preset)
        owner, project_id = project.owner_id, project.id
    png = render.render_page(doc, PREVIEW_WIDTH)
    image = storage.validate_image(png)
    async with transaction() as db:
        await repo.lock_project_of(db, Page, page_id)
        page = (await db.execute(select(Page).where(Page.id == page_id).with_for_update())).scalar_one()
        asset = await repo.save_image(db, owner_id=owner, project_id=project_id, kind="page_preview",
                                      image=image)  # fmt: skip
        page.preview_asset_id, page.status, page.lettered_at = asset.id, "ready", datetime.now(UTC)
        project = await repo.get_project(db, project_id, lock=True)
        if project.first_page_at is None and project.script_approved_at:
            project.first_page_at = datetime.now(UTC)
            metrics.time_to_first_page.observe(
                (project.first_page_at - project.script_approved_at).total_seconds())  # fmt: skip
        await events.emit(db, project_id, "page.ready", {"page_id": str(page_id), "index": page.index,
                                                          "flags": sorted(set(flags))})  # fmt: skip
        await events.notify_after_commit(db)
    return {"flags": sorted(set(flags))}


async def stage_finish(project_id: uuid.UUID) -> dict[str, Any]:
    async with transaction() as db:
        project = await repo.get_project(db, project_id, lock=True)
        if project.status != S.ILLUSTRATING.value:
            return {"status": project.status}
        counts = dict((await db.execute(select(Panel.status, func.count()).where(
            Panel.project_id == project_id).group_by(Panel.status))).all())  # fmt: skip
        total = sum(counts.values())
        clean = counts.get(PanelStatus.OK.value, 0) + counts.get(PanelStatus.NEEDS_ATTENTION.value, 0)
        target = S.COMPLETED if clean == total else S.PARTIALLY_COMPLETED
        project.completed_at = datetime.now(UTC)
        await credits.close_for_project(db, project_id)  # undrawn panels are not charged
        await repo.set_status(db, project, target, panels=counts)
        if project.script_approved_at:
            metrics.time_to_complete.observe(
                (project.completed_at - project.script_approved_at).total_seconds())  # fmt: skip
        metrics.workflow_finished.labels("ComicWorkflow", target.value).inc()
        await events.notify_after_commit(db)
        return {"status": target.value}


async def stage_end(project_id: uuid.UUID, *, status: str, code: str, message: str,
                    resume_from: str | None = None) -> None:  # fmt: skip
    """Record a failed, cancelled or expired run and release its credit holds."""
    async with transaction() as db:
        project = await repo.get_project(db, project_id, lock=True)
        if project.status in (S.COMPLETED.value, S.BLOCKED.value, S.CANCELLED.value):
            return
        await repo.fail_project(db, project, code=code, message=message, status=S(status),
                                resume_from=resume_from)  # fmt: skip
        await db.execute(update(Panel).where(Panel.project_id == project_id,
                                             Panel.status == PanelStatus.GENERATING.value)
                         .values(status=PanelStatus.PENDING.value))  # fmt: skip
        metrics.workflow_finished.labels("ComicWorkflow", status).inc()
        await events.notify_after_commit(db)


# ------------------------------------------------------------------ export
async def snapshot_revision(db: Any, project: Project) -> dict[str, Any]:
    """Freeze everything an export needs. Later edits cannot change this document."""
    plan = await repo.owner_plan(db, project.owner_id)
    style = styles().get(project.style_preset or styles().default)
    pages = []
    for page, panels in await repo.pages_with_panels(db, project.id):
        template = layouts()[page.layout_template_id]
        panel_docs = []
        for panel in panels:
            version_id = None
            if panel.current_version_id and panel.status in (PanelStatus.OK.value,
                                                             PanelStatus.NEEDS_ATTENTION.value):  # fmt: skip
                version_id = str(panel.current_version_id)
            balloons = list((await db.execute(select(Balloon).where(Balloon.panel_id == panel.id)
                                              .order_by(Balloon.order_index))).scalars())  # fmt: skip
            panel_docs.append({
                "rect": list(panel_rect(page, panel)), "version_id": version_id,
                "balloons": [{"type": b.type, "text": b.text, "geometry": b.geometry,
                              "order_index": b.order_index, "flags": list(b.flags or [])} for b in balloons]})  # fmt: skip
        pages.append({"index": page.index, "size": list(template.page), "panels": panel_docs})
    return {"title": project.title, "edit_version": project.edit_version, "font": style.font,
            "sfx_font": style.sfx_font, "watermark": plan == "free", "pages": pages}  # fmt: skip


def revision_sha(revision: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(revision, sort_keys=True).encode()).hexdigest()


async def stage_export_begin(export_id: uuid.UUID) -> dict[str, Any]:
    async with transaction() as db:
        export = (
            await db.execute(select(Export).where(Export.id == export_id).with_for_update())
        ).scalar_one()
        if export.status == "queued":
            export.status = "running"
            await events.emit(db, export.project_id, "export.updated",
                              {"export_id": str(export_id), "status": "running"})  # fmt: skip
            await events.notify_after_commit(db)
        return {"pages": len(export.revision["pages"]), "status": export.status}


async def stage_export_page(export_id: uuid.UUID, page_index: int) -> dict[str, Any]:
    """Upscale this page's panels (cached per panel version) and render the final page.
    Resumable: a page already rendered for this export is skipped."""
    async with transaction() as db:
        export = (await db.execute(select(Export).where(Export.id == export_id))).scalar_one()
        if str(page_index) in (export.pages_done or {}):
            return {"skipped": True}
        revision, owner, project_id = export.revision, export.owner_id, export.project_id
        export_format = export.format
    page = revision["pages"][page_index]
    ctx = CallContext("export_upscale", str(owner), str(project_id), target_id=str(export_id))
    # Local resampling (default): the stored image is scaled by the renderer (Lanczos); nothing is
    # sent to any service. "fal" re-enables the external ESRGAN upscaler when fal is enabled.
    external_upscale = get_settings().export_upscale == "fal" and get_providers().upscale is not None
    quality: list[dict[str, Any]] = []
    panels = []
    for p in page["panels"]:
        image: bytes | None = None
        if p["version_id"]:
            vid = uuid.UUID(p["version_id"])
            async with transaction() as db:
                version = (await db.execute(select(PanelVersion).where(PanelVersion.id == vid))).scalar_one()
                source_id = version.upscaled_asset_id or version.image_asset_id
                quarantined = (await db.execute(select(Asset.quarantined).where(
                    Asset.id == version.image_asset_id))).scalar_one()  # fmt: skip
                data = None if quarantined else await repo.asset_bytes(db, source_id)
                upscaled = version.upscaled_asset_id is not None
            if data is not None:
                quality.append(
                    print_resolution(page_index, len(quality), data, tuple(p["rect"]), tuple(page["size"]))
                )
            if data is not None and not upscaled and external_upscale:
                result = await metering.upscale_image(data, ctx, key=f"upscale:{vid}")
                big = storage.validate_image(result.data)
                async with transaction() as db:
                    v = (await db.execute(select(PanelVersion).where(PanelVersion.id == vid)
                                          .with_for_update())).scalar_one()  # fmt: skip
                    if v.upscaled_asset_id is None:
                        asset = await repo.save_image(db, owner_id=owner, project_id=project_id,
                                                      kind="panel_upscaled", image=big)  # fmt: skip
                        v.upscaled_asset_id = asset.id
                data = big.data
            image = data
        panels.append(render.PanelDoc(tuple(p["rect"]), image, tuple(p["balloons"])))
    doc = render.PageDoc(tuple(page["size"]), tuple(panels), revision["font"], revision["sfx_font"],
                         revision["watermark"])  # fmt: skip
    # PDF draws lettering as vectors at assembly, so its page images are the art layer only.
    rendered = storage.validate_image(render.render_page(doc, EXPORT_WIDTH, lettering=export_format != "pdf"))
    async with transaction() as db:
        export = (
            await db.execute(select(Export).where(Export.id == export_id).with_for_update())
        ).scalar_one()
        if str(page_index) not in (export.pages_done or {}):
            asset = await repo.save_image(db, owner_id=owner, project_id=project_id,
                                          kind="export_page", image=rendered)  # fmt: skip
            export.pages_done = {**(export.pages_done or {}), str(page_index): str(asset.id)}
            export.print_quality = merge_print_quality(export.print_quality, page_index, quality,
                                                      "external_upscale" if external_upscale else "local_resample",
                                                      letters=lettering_sizes(page_index, page),
                                                      flagged=lettering_problems(page_index, page))  # fmt: skip
    for q in quality:
        metrics.provider_print_dpi.observe(q["dpi"])
    return {"skipped": False}


PRINT_WIDTH_IN = 6.0  # the PDF trim width (render.PDF_TRIM_WIDTH_PT = 432 pt)
DPI_GOOD, DPI_LOW = 300, 150


def print_resolution(page_index: int, n: int, image: bytes, rect: tuple[float, ...],
                     page_size: tuple[float, ...]) -> dict[str, Any]:  # fmt: skip
    """Effective print resolution of one panel: source pixels per printed inch along the axis the
    cover crop scales most (the lower of the two). Resampling cannot add detail beyond this."""
    from PIL import Image as PILImage

    with PILImage.open(io.BytesIO(image)) as im:
        w, h = im.size
    inch_per_unit = PRINT_WIDTH_IN / page_size[0]
    pw_in, ph_in = rect[2] * inch_per_unit, rect[3] * inch_per_unit
    dpi = min(w / pw_in, h / ph_in)
    return {
        "page": page_index + 1,
        "panel": n + 1,
        "source_px": [w, h],
        "printed_in": [round(pw_in, 2), round(ph_in, 2)],
        "dpi": round(dpi),
    }


def lettering_sizes(page_index: int, page: dict[str, Any]) -> list[dict[str, Any]]:
    """Smallest lettering per panel at the export sizes: points when printed at the PDF trim width,
    pixels in the exported PNG/CBZ page."""
    from storypanel.pipeline import lettering

    width = float(page["size"][0])
    rows = []
    for n, p in enumerate(page["panels"]):
        sizes = [float(b["geometry"]["text_size"]) for b in p["balloons"]
                 if b["type"] != "sfx" and b.get("geometry", {}).get("text_size") is not None]  # fmt: skip
        if sizes:
            rows.append({"page": page_index + 1, "panel": n + 1, "text_size": min(sizes),
                         "pt": lettering.printed_pt(min(sizes), width),
                         "px": round(min(sizes) * EXPORT_WIDTH / width, 1)})  # fmt: skip
    return rows


# Lettering problems worth a warning on the export, with what each needs from the user.
# Confirmed: measured against positions the check reported. Uncertain: a face position was only
# guessed from the script, so a problem can't be ruled out; the user should look.
EXPORT_FLAG_TEXT = {
    "covers_face": "a balloon covers a face",
    "covers_prop": "a balloon covers an important object",
    "overflow": "text doesn't fit at a readable size",
    "reading_order": "balloons may read out of order",
    "overlaps_lettering": "lettering overlaps other lettering",
    "tail_ambiguous": "a tail points nearer another character than its speaker",
    "sfx_covers_character": "a sound effect covers a character",
    "face_position_estimated": "possibly: a balloon may cover a face (faces weren't located automatically; please check)",
    "tail_target_estimated": "possibly: a tail may not point at its speaker (faces weren't located automatically)",
}
UNCERTAIN_FLAGS = frozenset({"face_position_estimated", "tail_target_estimated"})


def lettering_problems(page_index: int, page: dict[str, Any]) -> list[dict[str, Any]]:
    """Unresolved lettering flags per panel, from the export's own snapshot of the balloons."""
    rows = []
    for n, p in enumerate(page["panels"]):
        flags = sorted({f for b in p["balloons"] for f in b.get("flags") or [] if f in EXPORT_FLAG_TEXT})
        if flags:
            rows.append({"page": page_index + 1, "panel": n + 1, "flags": flags})
    return rows


def merge_print_quality(current: dict[str, Any] | None, page_index: int, panels: list[dict[str, Any]],
                        method: str, letters: list[dict[str, Any]] | None = None,
                        flagged: list[dict[str, Any]] | None = None) -> dict[str, Any]:  # fmt: skip
    from storypanel.pipeline.lettering import MIN_PRINT_PT

    lrows = [r for r in (current or {}).get("lettering", []) if r["page"] != page_index + 1] + (letters or [])
    lrows.sort(key=lambda r: (r["page"], r["panel"]))
    small = [r for r in lrows if r["pt"] < MIN_PRINT_PT]
    rows = [r for r in (current or {}).get("panels", []) if r["page"] != page_index + 1] + panels
    rows.sort(key=lambda r: (r["page"], r["panel"]))
    low = [r for r in rows if r["dpi"] < DPI_LOW]
    soft = [r for r in rows if DPI_LOW <= r["dpi"] < DPI_GOOD]
    warnings = []
    if low:
        warnings.append(f"{len(low)} panel(s) below {DPI_LOW} DPI at a {PRINT_WIDTH_IN:g}-inch print width: "
                        "they will look blurry in print. Fine on screen.")  # fmt: skip
    if soft:
        warnings.append(f"{len(soft)} panel(s) between {DPI_LOW} and {DPI_GOOD} DPI: may look soft in print.")
    for page_no in sorted({r["page"] for r in small}):
        # One warning per page, from that page's own smallest lettering: a fixed width does not fit every page.
        on_page = [r for r in small if r["page"] == page_no]
        low_pt = min(r["pt"] for r in on_page)
        needed = (
            math.ceil(PRINT_WIDTH_IN * MIN_PRINT_PT / low_pt * 10 - 1e-9) / 10
        )  # rounded up: never short of the floor
        panels_txt = ", ".join(str(r["panel"]) for r in on_page)
        warnings.append(f"Page {page_no}: lettering in panel(s) {panels_txt} prints at {low_pt:g} pt at the "
                        f"{PRINT_WIDTH_IN:g}-inch print width, below the {MIN_PRINT_PT:g} pt readable size. Fine on "
                        f"screen. This page needs at least {needed:.1f} in of print width for its smallest text to "
                        f"reach {MIN_PRINT_PT:g} pt.")  # fmt: skip
    frows = [r for r in (current or {}).get("lettering_flags", []) if r["page"] != page_index + 1] + (
        flagged or []
    )
    frows.sort(key=lambda r: (r["page"], r["panel"]))
    for r in frows:
        sure = [EXPORT_FLAG_TEXT[f] for f in r["flags"] if f not in UNCERTAIN_FLAGS]
        maybe = [EXPORT_FLAG_TEXT[f] for f in r["flags"] if f in UNCERTAIN_FLAGS]
        if sure:
            warnings.append(f"Page {r['page']}, panel {r['panel']}: {'; '.join(sure)}. In the editor: move the balloon, "
                            "shorten the text, or redraw the panel to leave room.")  # fmt: skip
        if maybe:
            warnings.append(f"Page {r['page']}, panel {r['panel']}: {'; '.join(maybe)}.")
    return {"lettering": lrows, "lettering_flags": frows, "min_letter_pt": min((r["pt"] for r in lrows), default=None),
            "min_letter_px": min((r["px"] for r in lrows), default=None), "method": method, "print_width_in": PRINT_WIDTH_IN,
            "note": "Panels are resampled locally (Lanczos); this does not add detail." if method == "local_resample"
            else "Panels were upscaled by an external model.",
            "min_dpi": min((r["dpi"] for r in rows), default=None), "panels": rows, "warnings": warnings}  # fmt: skip


async def stage_export_assemble(export_id: uuid.UUID) -> dict[str, Any]:
    async with transaction() as db:
        export = (await db.execute(select(Export).where(Export.id == export_id))).scalar_one()
        if export.status == "ready":
            return {"status": "ready"}
        revision, fmt = export.revision, export.format
        order = sorted(revision["pages"], key=lambda p: p["index"])
        pages = [await repo.asset_bytes(db, uuid.UUID(export.pages_done[str(n)]))
                 for n in range(len(order))]  # fmt: skip
        owner, project_id = export.owner_id, export.project_id
    title = revision["title"]
    # Provenance: every exported page carries a machine-readable AI-generation label.
    pages = [render.label_png(p, title) for p in pages]
    if fmt == "pdf":
        docs = [render.PageDoc(tuple(pg["size"]), tuple(render.PanelDoc(tuple(p["rect"]), None, tuple(p["balloons"]))
                                                        for p in pg["panels"]),
                               revision["font"], revision["sfx_font"], revision["watermark"]) for pg in order]  # fmt: skip
        data, ctype, ext = render.build_pdf(docs, pages, title), "application/pdf", "pdf"
    elif fmt == "cbz":
        data, ctype, ext = render.build_cbz(pages, title), "application/vnd.comicbook+zip", "cbz"
    else:
        data, ctype, ext = render.build_png_bundle(pages)
    async with transaction() as db:
        export = (
            await db.execute(select(Export).where(Export.id == export_id).with_for_update())
        ).scalar_one()
        asset = await repo.save_blob(db, owner_id=owner, project_id=project_id, kind=f"export_{fmt}",
                                     data=data, content_type=ctype, ext=ext,
                                     sha256=hashlib.sha256(data).hexdigest())  # fmt: skip
        export.asset_id, export.status = asset.id, "ready"
        export.expires_at = datetime.now(UTC) + timedelta(days=get_settings().export_retention_days)
        metrics.exports.labels(fmt, "ready").inc()
        await events.emit(db, project_id, "export.updated",
                          {"export_id": str(export_id), "status": "ready", "format": fmt})  # fmt: skip
        await events.notify_after_commit(db)
    return {"status": "ready"}


async def stage_export_failed(export_id: uuid.UUID, code: str, message: str) -> None:
    async with transaction() as db:
        export = (
            await db.execute(select(Export).where(Export.id == export_id).with_for_update())
        ).scalar_one()
        if export.status == "ready":
            return
        export.status, export.error = "failed", {"code": code, "message": message}
        metrics.exports.labels(export.format, "failed").inc()
        await events.emit(db, export.project_id, "export.updated", {
            "export_id": str(export_id), "status": "failed",
            "error": {"code": code, "message": message}})  # fmt: skip
        await events.notify_after_commit(db)
