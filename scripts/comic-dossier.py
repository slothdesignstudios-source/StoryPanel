#!/usr/bin/env python3
"""Read-only quality dossier for ONE comic: everything needed to judge the comic itself.

    scripts/live-env.sh services/backend/.venv/bin/python scripts/comic-dossier.py <comic-id> OUT_DIR

Writes into OUT_DIR (which must not exist yet):
  dossier.json          original idea, Story Bible (every version), cast with sheet reviews, every
                        script version with beat sheet and checks, and for every panel: spec, every
                        attempt (provider, model, prompt version, references, QA verdict, problems,
                        fix notes, cost, latency) and the panel prompt REBUILT from the stored inputs,
                        with a flag saying whether its SHA-256 matches the recorded one
  refs/                 approved reference images (portrait, turnaround crops, expressions, locations)
  panels/               every generated attempt, named p<page>-n<panel>-a<attempt>-<qa status>.png
  pages/                each page lettered as it stands now, at 2000 px wide (rendered locally)
  contact-sheet.png     every attempt of every panel in reading order, with its QA verdict
  exports/              the newest finished PDF, PNG and CBZ export, if any

No provider is called and nothing is written to the database or storage: the queries run in a
READ ONLY transaction, and only the configured LOCAL database and storage are read. No keys,
tokens or account details are written. Prompts are not stored by the app (only their SHA-256), so
the rebuilt prompt is labelled "reproduced" only when its hash matches the recorded one.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/backend/src"))
LOCAL = {"127.0.0.1", "localhost", "::1", ""}


def _local_only() -> None:
    from storypanel.config import get_settings

    s = get_settings()
    db_host = urllib.parse.urlsplit(s.database_url.replace("+asyncpg", "")).hostname or ""
    s3_host = urllib.parse.urlsplit(s.s3_endpoint_url or "").hostname or ""
    if db_host not in LOCAL or (s.s3_endpoint_url and s3_host not in LOCAL):
        raise SystemExit("refusing: the configured database or storage is not on this machine")


def rebuild_prompt(spec: dict[str, Any], v: Any, prev_notes: list[str], ctx: dict[str, Any]) -> dict[str, Any]:
    """Recompile the panel prompt from stored inputs, restricted to the references the attempt
    actually used, and compare its hash with the recorded one."""
    from storypanel.domain.schemas import PanelSpec
    from storypanel.pipeline import art

    if v.kind != "generate" and v.kind != "regenerate":
        return {"status": "not_rebuilt", "reason": f"{v.kind} prompts depend on the user's instruction"}
    if any(r.get("role") == "repair_base" for r in v.references or []):
        return {"status": "not_rebuilt", "reason": "targeted repair of an earlier attempt"}
    refs = v.references or []
    char_refs: dict[str, dict[str, str]] = {}
    loc_refs: dict[str, str] = {}
    continuity = None
    for r in refs:
        if r["role"] == "character":
            char_refs.setdefault(r["key"], {})[r["view"]] = r["asset_id"]
        elif r["role"] == "location":
            loc_refs[r["key"]] = r["asset_id"]
        elif r["role"] == "continuity":
            continuity = (r["key"], r["asset_id"])
    try:
        compiled = art.compile_panel(
            panel=PanelSpec.model_validate(spec), aspect=ctx["aspect"], style_preset=ctx["preset"],
            cast=ctx["cast"], locations=ctx["locations"], character_refs=char_refs, location_refs=loc_refs,
            continuity=continuity, max_references=max(len(refs), 1), fix_notes=tuple(prev_notes),
            rules=ctx["rules"], prompt_version=v.prompt_version, panel_size=ctx["panel_size"],
        )  # fmt: skip
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        return {"status": "not_rebuilt", "reason": f"{type(exc).__name__}: {exc}"[:200]}
    same = compiled.prompt_sha256 == v.prompt_sha256
    return {"status": "reproduced" if same else "differs",
            "note": None if same else "inputs changed since (spec, rules or cast edited); this is today's prompt, "
                                      "not necessarily the one sent",
            "prompt": compiled.prompt}  # fmt: skip


def contact_sheet(tiles: list[tuple[str, bytes]], out: Path) -> None:
    from PIL import Image, ImageDraw

    if not tiles:
        return
    tw, th, cols, label = 360, 360, 4, 26
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tw, rows * (th + label)), "white")
    draw = ImageDraw.Draw(sheet)
    for n, (caption, data) in enumerate(tiles):
        with Image.open(io.BytesIO(data)) as img:
            im = img.convert("RGB")
            im.thumbnail((tw - 8, th - 8))
        x, y = (n % cols) * tw, (n // cols) * (th + label)
        sheet.paste(im, (x + (tw - im.width) // 2, y + (th - im.height) // 2))
        draw.text((x + 6, y + th + 4), caption, fill=(0, 0, 0))
    sheet.save(out)


async def dossier(pid: uuid.UUID, out: Path) -> dict[str, Any]:
    from sqlalchemy import select, text

    from storypanel.catalog import layouts, styles
    from storypanel.db.models import (
        Asset,
        Character,
        CharacterRef,
        Export,
        Location,
        PanelVersion,
        Project,
        Script,
        StoryBibleRow,
    )  # fmt: skip
    from storypanel.db.session import transaction
    from storypanel.domain.schemas import StoryBible
    from storypanel.pipeline import art, render
    from storypanel.pipeline.stages_art import page_doc
    from storypanel import storage
    from storypanel.services import repo

    for d in ("refs", "panels", "pages", "exports"):
        (out / d).mkdir(parents=True)
    tiles: list[tuple[str, bytes]] = []
    async with transaction() as db:
        await db.execute(text("set transaction read only"))
        project = (await db.execute(select(Project).where(Project.id == pid))).scalar_one_or_none()
        if project is None:
            raise SystemExit(f"no comic {pid}")
        bibles = list((await db.execute(select(StoryBibleRow).where(StoryBibleRow.project_id == pid)
                                        .order_by(StoryBibleRow.version))).scalars())  # fmt: skip
        bible = StoryBible.model_validate(bibles[-1].content) if bibles else None
        cast = await repo.cast_specs(db, pid)
        locations = await repo.location_specs(db, pid)
        preset = project.style_preset or styles().default
        rules = bible.continuity_rules if bible else []
        result: dict[str, Any] = {
            "comic_id": str(pid), "status": project.status, "format": project.format,
            "image_provider": project.image_provider, "style_preset": preset, "idea": project.prompt,
            "bible_versions": [{"version": b.version, "source": b.source, "content": b.content} for b in bibles],
            "characters": [], "locations": [], "scripts": [], "pages": [],
        }  # fmt: skip
        for c in (await db.execute(select(Character).where(Character.project_id == pid)
                                   .order_by(Character.order_index))).scalars():  # fmt: skip
            refs = (await db.execute(select(CharacterRef.view, CharacterRef.asset_id).where(
                CharacterRef.character_id == c.id, CharacterRef.sheet_version == c.sheet_version))).all()  # fmt: skip
            for view, asset_id in refs:
                (out / "refs" / f"{c.slug}-{view}.png").write_bytes(await repo.asset_bytes(db, asset_id))
            result["characters"].append({"slug": c.slug, "spec": c.spec, "sheet_version": c.sheet_version,
                                         "sheet_check": c.sheet_check, "views": sorted(v for v, _ in refs)})  # fmt: skip
        for loc in (await db.execute(select(Location).where(Location.project_id == pid))).scalars():
            if loc.reference_asset_id:
                (out / "refs" / f"location-{loc.slug}.png").write_bytes(await repo.asset_bytes(db, loc.reference_asset_id))
            result["locations"].append({"slug": loc.slug})
        for sc in (await db.execute(select(Script).where(Script.project_id == pid).order_by(Script.version))).scalars():
            result["scripts"].append({c.key: getattr(sc, c.key) for c in Script.__table__.columns
                                      if c.key not in ("id", "project_id")} | {"created_at": str(sc.created_at)})  # fmt: skip
        for page, panels in await repo.pages_with_panels(db, pid):
            template = layouts()[page.layout_template_id]
            pg: dict[str, Any] = {"index": page.index, "layout": page.layout_template_id, "panels": []}
            for panel in panels:
                ctx = {"aspect": art.panel_aspect(template, panel.index), "preset": preset, "cast": cast,
                       "locations": locations, "rules": rules,
                       "panel_size": (float(template.rects[panel.index][2]), float(template.rects[panel.index][3]))}  # fmt: skip
                versions = list((await db.execute(select(PanelVersion).where(PanelVersion.panel_id == panel.id)
                                                  .order_by(PanelVersion.created_at))).scalars())  # fmt: skip
                pv: list[dict[str, Any]] = []
                prev_notes: list[str] = []
                for v in versions:
                    quarantined = (await db.execute(select(Asset.quarantined).where(Asset.id == v.image_asset_id))).scalar_one()
                    name = f"p{page.index + 1}-n{panel.index + 1}-a{v.attempt}-{v.kind}-{v.qa_status}.png"
                    if not quarantined:
                        data = await repo.asset_bytes(db, v.image_asset_id)
                        (out / "panels" / name).write_bytes(data)
                        tiles.append((f"P{page.index + 1} #{panel.index + 1} a{v.attempt} {v.qa_status}", data))
                    scores = v.qa_scores or {}
                    pv.append({
                        "attempt": v.attempt, "kind": v.kind, "instruction": v.instruction, "provider": v.provider,
                        "model": v.model, "prompt_version": v.prompt_version, "prompt_sha256": v.prompt_sha256,
                        "references": v.references, "qa_status": v.qa_status, "problems": scores.get("problems"),
                        "fix_notes": scores.get("fix_notes"), "qa_aspects": scores.get("aspects"),
                        "repeated_after_repair": bool(scores.get("repeated_after_repair")),
                        "identity_rating_avg": scores.get("identity_rating_avg"),
                        "cost_usd": str(v.cost_usd), "latency_ms": v.latency_ms, "current": v.id == panel.current_version_id,
                        "image": None if quarantined else f"panels/{name}",
                        "rebuilt_prompt": rebuild_prompt(panel.spec, v, prev_notes, ctx),
                    })  # fmt: skip
                    prev_notes = list(scores.get("fix_notes") or [])
                pg["panels"].append({"index": panel.index, "status": panel.status, "spec": panel.spec,
                                     "aspect": ctx["aspect"], "versions": pv})  # fmt: skip
            doc = await page_doc(db, page, panels, watermark=False, preset=preset)
            (out / "pages" / f"page-{page.index + 1}.png").write_bytes(render.render_page(doc, 2000))
            result["pages"].append(pg)
        for fmt in ("pdf", "png", "cbz"):
            ex = (await db.execute(select(Export).where(Export.project_id == pid, Export.format == fmt,
                                                        Export.status == "ready", Export.asset_id.is_not(None))
                                   .order_by(Export.created_at.desc()).limit(1))).scalar_one_or_none()  # fmt: skip
            if ex is not None:
                key = (await db.execute(select(Asset.storage_key).where(Asset.id == ex.asset_id))).scalar_one()
                (out / "exports" / f"comic.{fmt}").write_bytes(await storage.get_bytes(key))
                result.setdefault("exports", {})[fmt] = {"print_quality": ex.print_quality}
    contact_sheet(tiles, out / "contact-sheet.png")
    return result


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    pid, out = uuid.UUID(sys.argv[1]), Path(sys.argv[2])
    if out.exists():
        raise SystemExit(f"refusing: {out} already exists")
    _local_only()
    result = asyncio.run(dossier(pid, out))
    (out / "dossier.json").write_text(json.dumps(result, indent=1, default=str))
    n = sum(len(p["versions"]) for pg in result["pages"] for p in pg["panels"])
    print(f"dossier for {pid}: {len(result['pages'])} page(s), {n} panel attempt(s) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
