#!/usr/bin/env python3
"""Re-run ONLY the panel check's vision call (prompts v4) on an existing comic's lettered panels, to see
whether faces are located automatically, then re-letter the pages from those boxes. Nothing is redrawn.

    scripts/live-env.sh services/backend/.venv/bin/python scripts/face-check.py <comic-id> OUT_DIR --approve-usd 0.10
    (--simulated instead of --approve-usd runs it against simulated providers only, for testing)

PAID: one vision request per panel that has lettering (claude-haiku-5-5; the Train page has 6, Tulips 1).
Estimated USD 0.001 per request, reserved up to about USD 0.009 each (16,000 output tokens at the
verified price). Refuses unless SP_PROVIDER_MODE=live and --approve-usd covers the reserved total.
Requests are metered and counted like any other (provider_attempts), against the comic owner's limits.

Writes into OUT_DIR (must not exist): face-check.json (per panel: the boxes returned, which faces were
located, box problems, and the lettering flags of the automatic re-lettering) and
page-N-auto.png (the page re-lettered automatically from the returned boxes). The comic, its QA
records and its balloons are NOT changed.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/backend/src"))

PER_CALL_RESERVED = Decimal("0.009")


async def run(pid: uuid.UUID, out: Path, approve: Decimal | None) -> int:
    from sqlalchemy import select

    from storypanel.catalog import layouts, styles
    from storypanel.config import get_settings
    from storypanel.db.models import (
        Asset,
        Character,
        CharacterRef,
        PanelVersion,
        Project,
    )
    from storypanel.db.session import transaction
    from storypanel.domain.schemas import PanelSpec
    from storypanel.pipeline import art, qa, render
    from storypanel.pipeline.stages_art import letter_panel, page_doc, panel_rect
    from storypanel.providers.base import CallContext
    from storypanel.services import repo

    live = get_settings().provider_mode == "live"
    if live != (approve is not None):
        print("refusing: use --approve-usd with live providers, --simulated only with simulated ones", file=sys.stderr)
        return 2
    async with transaction() as db:
        project = (await db.execute(select(Project).where(Project.id == pid))).scalar_one_or_none()
        if project is None:
            raise SystemExit(f"no comic {pid}")
        pages = await repo.pages_with_panels(db, pid)
        todo = [(pg, pn) for pg, pns in pages for pn in pns
                if PanelSpec.model_validate(pn.spec).dialogue and pn.current_version_id]  # fmt: skip
        need = PER_CALL_RESERVED * len(todo)
        if approve is not None and approve < need:
            print(f"refusing: --approve-usd {approve} is below USD {need}, the reserved total for {len(todo)} request(s)",
                  file=sys.stderr)  # fmt: skip
            return 2
        cast = await repo.cast_specs(db, pid)
        bible = await repo.latest_bible(db, pid)
        owner, preset = project.owner_id, project.style_preset or styles().default
    report: list[dict[str, Any]] = []
    auto: dict[uuid.UUID, Any] = {}  # panel id -> balloons placed automatically from the returned boxes
    for page, panel in todo:
        spec = PanelSpec.model_validate(panel.spec)
        async with transaction() as db:
            v, w, h = (await db.execute(select(PanelVersion, Asset.width, Asset.height).join(
                Asset, Asset.id == PanelVersion.image_asset_id).where(PanelVersion.id == panel.current_version_id))).one()  # fmt: skip
            image = await repo.asset_bytes(db, v.image_asset_id)
            refs = {}
            for slug, asset_id in (await db.execute(select(Character.slug, CharacterRef.asset_id).join(
                    CharacterRef, CharacterRef.character_id == Character.id).where(
                    Character.project_id == pid, CharacterRef.view == "portrait",
                    CharacterRef.sheet_version == Character.sheet_version))).all():  # fmt: skip
                refs[slug] = await repo.asset_bytes(db, asset_id)
        rules = art.effective_rules(bible.continuity_rules if bible else [], spec)
        vr = await qa.vision_report(image=image, panel=spec, cast=cast, reference_images=refs, required=rules.must,
                                    elements=[(e.element, e.state) for e in spec.element_states],
                                    ctx=CallContext("face_check", str(owner), str(pid), target_id=str(panel.id)),
                                    operation="face_check")  # fmt: skip
        heads = {o.id: o.head_box.model_dump() for o in vr.characters if o.present and o.head_box}
        bodies = {o.id: o.body_box.model_dump() for o in vr.characters if o.present and o.body_box}
        scores = {"head_boxes": heads, "body_boxes": bodies,
                  "action_box": vr.action_box.model_dump() if vr.action_box else None}  # fmt: skip
        placed, basis = letter_panel(spec=spec, rect=panel_rect(page, panel), page_size=layouts()[page.layout_template_id].page,
                                     scores=scores, image_size=(w, h) if w and h else None, prompt_version="v4",
                                     kind=v.kind, font=styles().get(preset).font, version_id=str(v.id), image=image)  # fmt: skip
        report.append({"page": page.index + 1, "panel": panel.index + 1, "head_boxes": heads,
                       "faces_unlocated": [o.id for o in vr.characters if o.present and o.head_box is None],
                       "faces": basis["faces"], "box_problems": basis["box_problems"],
                       "flags": [b.flags for b in placed]})  # fmt: skip
        auto[panel.id] = placed
    async with transaction() as db:
        for page, panels in await repo.pages_with_panels(db, pid):
            doc = await page_doc(db, page, panels, watermark=False, preset=preset)
            docs = []
            for pn, pdoc in zip(panels, doc.panels, strict=True):
                placed = auto.get(pn.id)
                if placed is None:
                    docs.append(pdoc)
                    continue
                docs.append(render.PanelDoc(pdoc.rect, pdoc.image, tuple(
                    {"type": b.type, "text": b.text, "geometry": b.geometry(styles().get(preset).font), "order_index": b.index}
                    for b in placed)))  # fmt: skip
            redone = render.PageDoc(doc.size, tuple(docs), doc.font, doc.sfx_font, doc.watermark)
            (out / f"page-{page.index + 1}-auto.png").write_bytes(render.render_page(redone, 2000))
    (out / "face-check.json").write_text(json.dumps(report, indent=1))
    located = sum(1 for r in report if r["faces"] == "located")
    print(f"{len(report)} panel(s) checked; faces located in all characters for {located}; -> {out}")
    return 0


def main() -> int:
    simulated = len(sys.argv) == 4 and sys.argv[3] == "--simulated"
    if not simulated and (len(sys.argv) != 5 or sys.argv[3] != "--approve-usd"):
        raise SystemExit(__doc__)
    out = Path(sys.argv[2])
    if out.exists():
        raise SystemExit(f"refusing: {out} already exists")
    out.mkdir(parents=True)
    return asyncio.run(run(uuid.UUID(sys.argv[1]), out, None if simulated else Decimal(sys.argv[4])))


if __name__ == "__main__":
    sys.exit(main())
