#!/usr/bin/env python3
"""Stage 1A: locate faces automatically on an EXISTING comic's lettered panels and re-letter them from
the result. No image is generated, repaired or redrawn; the comic, its QA records and balloons are not
changed. Everything goes to a separate output folder.

    # free, makes no provider call: storage, panels, expected faces, credentials, exact reservations
    scripts/live-env.sh services/backend/.venv/bin/python scripts/face-check.py preflight <comic-id>

    # PAID (one vision request per lettered panel), stops before any call that would cross the cap
    scripts/live-env.sh services/backend/.venv/bin/python scripts/face-check.py run <comic-id> OUT_DIR \\
        --approve-usd 0.20 [--annotations FILE --comic train|tulips]

    # testing only, against simulated providers
    ... face-check.py run <comic-id> OUT_DIR --simulated [--annotations FILE --comic NAME]

The run uses the production code path: the v4 panel check's vision call (qa.vision_report, metered
like every paid request) -> boxes in the stored qa_scores format -> stages_art.letter_panel (crop
conversion, bad-box refusal) -> lettering.layout_panel -> render.render_page (PNG) and
render.build_pdf (PDF). The boxes are written to OUT_DIR/face-check.json, not to the database.

The cap: before each call, the counted spend of this run (from the paid-request ledger) plus the
app's own reservation for that call (input bound + full output cap, pricing.text_estimate with the
real images) must stay within --approve-usd, or the run stops there. The app's per-comic and per-user
limits apply as usual. This is an application limit, not a guaranteed provider billing ceiling.

--annotations (docs/evidence/quality-2026-11-17/reletter/annotations.json) is read only AFTER the
automatic placement, to evaluate it: assignment, box alignment, balloon overlap, reading order, tail
attribution. It is never an input to detection or placement.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/backend/src"))


async def load(pid: uuid.UUID) -> dict[str, Any]:
    """Everything the check needs, read-only. Each stored image is read back and its SHA-256 compared
    with the hash recorded when it was saved."""
    from sqlalchemy import select

    from storypanel.catalog import layouts, styles
    from storypanel.db.models import Asset, Character, CharacterRef, PanelVersion, Project
    from storypanel.db.session import transaction
    from storypanel.domain.schemas import PanelSpec
    from storypanel.services import repo
    from storypanel.storage import get_bytes

    async with transaction() as db:
        project = (await db.execute(select(Project).where(Project.id == pid))).scalar_one_or_none()
        if project is None:
            raise SystemExit(f"no comic {pid} in this database")
        cast = await repo.cast_specs(db, pid)
        bible = await repo.latest_bible(db, pid)
        portraits: dict[str, Any] = {}
        for slug, asset in (await db.execute(select(Character.slug, Asset).join(
                CharacterRef, CharacterRef.character_id == Character.id).join(Asset, Asset.id == CharacterRef.asset_id).where(
                Character.project_id == pid, CharacterRef.view == "portrait",
                CharacterRef.sheet_version == Character.sheet_version))).all():  # fmt: skip
            portraits[slug] = asset
        pages = []
        for page, panels in await repo.pages_with_panels(db, pid):
            t = layouts()[page.layout_template_id]
            rows = []
            for pn in panels:
                spec = PanelSpec.model_validate(pn.spec)
                version = asset = None
                if pn.current_version_id:
                    version, asset = (await db.execute(select(PanelVersion, Asset).join(
                        Asset, Asset.id == PanelVersion.image_asset_id).where(PanelVersion.id == pn.current_version_id))).one()  # fmt: skip
                rows.append({"panel": pn, "spec": spec, "version": version, "asset": asset})
            pages.append({"page": page, "template": t, "panels": rows, "all": panels})
    blobs: dict[uuid.UUID, bytes | None] = {}
    problems = []
    for a in [r["asset"] for p in pages for r in p["panels"] if r["asset"] is not None] + list(portraits.values()):
        try:
            data = await get_bytes(a.storage_key)
        except Exception as exc:  # noqa: BLE001 - reported, the run won't start
            blobs[a.id], _ = None, problems.append(f"{a.kind} {a.id}: not readable ({type(exc).__name__})")
            continue
        ok = hashlib.sha256(data).hexdigest() == a.sha256
        blobs[a.id] = data if ok else None
        if not ok:
            problems.append(f"{a.kind} {a.id}: content differs from the recorded SHA-256")
    return {"project": project, "cast": cast, "bible": bible, "portraits": portraits, "pages": pages,
            "blobs": blobs, "storage_problems": problems, "preset": project.style_preset or styles().default}  # fmt: skip


def lettered(data: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return [(p, r) for p in data["pages"] for r in p["panels"] if r["spec"].dialogue and r["version"] is not None]


def call_inputs(data: dict[str, Any], r: dict[str, Any]) -> tuple[bytes, dict[str, bytes]]:
    image = data["blobs"][r["asset"].id]
    speakers = {c.id for c in r["spec"].characters}
    refs = {s: data["blobs"][a.id] for s, a in data["portraits"].items() if s in speakers and data["blobs"].get(a.id)}
    return image, refs


def reservation(data: dict[str, Any], r: dict[str, Any]) -> Decimal:
    """The app's own reservation for exactly the request this panel's check will send."""
    from storypanel.domain.schemas import VisionQAReport
    from storypanel.pipeline import art, qa
    from storypanel.providers import metering

    image, refs = call_inputs(data, r)
    spec = r["spec"]
    rules = art.effective_rules(data["bible"].continuity_rules if data["bible"] else [], spec)
    system, user, images = qa.vision_request(image=image, panel=spec, cast=data["cast"], reference_images=refs,
                                             required=rules.must, elements=[(e.element, e.state) for e in spec.element_states])  # fmt: skip
    return metering.structured_reservation(role="vision", system=system, user=user, schema=VisionQAReport, images=images)


async def preflight(pid: uuid.UUID) -> int:
    from storypanel.config import get_settings

    s = get_settings()
    data = await load(pid)
    todo = lettered(data)
    print(f"comic {pid}: \"{data['project'].title}\"  status {data['project'].status}")
    print(f"storage: endpoint {s.s3_endpoint_url or 'default'}; SP_LOCAL_S3={os.environ.get('SP_LOCAL_S3', 'unset')}")
    n_assets = len([1 for b in data["blobs"].values()])
    print(f"  {n_assets} image(s) read back; {len(data['storage_problems'])} problem(s)")
    for p in data["storage_problems"]:
        print("  PROBLEM", p)
    total = Decimal(0)
    for p in data["pages"]:
        for r in p["panels"]:
            spec = r["spec"]
            mark = "CHECK" if (p, r) in [(a, b) for a, b in todo] else "skip "
            faces = ", ".join(f"{data['cast'][c.id].name} ({c.position})" for c in spec.characters if c.id in data["cast"])
            lines = "; ".join(f"{d.type}{' ' + d.speaker if d.speaker else ''}: {d.text[:30]}" for d in spec.dialogue) or "no lettering"
            cost = reservation(data, r) if mark == "CHECK" else Decimal(0)
            total += cost
            print(f"  {mark} page {p['page'].index + 1} panel {r['panel'].index + 1}: expected faces [{faces or 'none'}]"
                  f" | {lines}" + (f" | reserved USD {cost:.4f}" if cost else ""))  # fmt: skip
    print(f"panels to check: {len(todo)} (only panels with lettering; a panel without lettering has nothing to place)")
    print(f"reserved total: USD {total:.4f} (the app counts each call at this until it settles)")
    print(f"provider mode: {s.provider_mode}; vision model: {s.vision_model}; Anthropic key configured: "
          f"{'yes' if s.anthropic_api_key else 'no'}")  # fmt: skip
    durable = os.environ.get("SP_LOCAL_S3", "versity") != "moto" or s.s3_endpoint_url is None
    print("image storage: " + ("on disk (durable)" if durable else "IN MEMORY: images are lost when it stops"))
    ready = (not data["storage_problems"] and durable and s.provider_mode == "live" and s.anthropic_api_key is not None
             and bool(todo))  # fmt: skip
    print("preflight: " + ("PASS" if ready else "FAIL (see above); no call would be made"))
    return 0 if ready else 1


async def counted(pid: uuid.UUID, since: datetime) -> list[dict[str, Any]]:
    from sqlalchemy import text

    from storypanel.db.session import transaction

    async with transaction() as db:
        rows = (await db.execute(text("""
            select status, cost_basis, cost_estimate_usd, cost_counted_usd, cost_calculated_usd, cost_confirmed_usd,
                   input_tokens, output_tokens, images, latency_ms, error_code, model
            from provider_attempts where project_id = :p and stage = 'face_check' and created_at >= :t
            order by created_at"""), {"p": pid, "t": since})).mappings().all()  # fmt: skip
    return [dict(r) for r in rows]


def annotation_index(path: Path, comic: str, cast: dict[str, Any]) -> dict[tuple[int, int], dict[str, Any]]:
    """Hand-placed faces, keyed by (page, panel), with names matched to this comic's character ids."""
    notes = json.loads(path.read_text())[comic]["panels"]
    by_name = {c.name.lower(): cid for cid, c in cast.items()}
    out = {}
    for n, ann in notes.items():
        heads = {}
        for k, v in ann["heads"].items():
            cid = k if k in cast else by_name.get(k.lower()) or next((c for c in cast if c.startswith(k)), None)
            if cid:
                heads[cid] = dict(zip("xywh", v, strict=True))
        out[(1, int(n))] = {"heads": heads}
    return out


def evaluate(placed: list[Any], detected: dict[str, dict[str, float]], ann: dict[str, Any] | None,
             rect: tuple[float, ...]) -> dict[str, Any]:  # fmt: skip
    """Score the automatic result against hand-placed faces (comparison evidence only)."""
    from storypanel.pipeline import lettering

    if not ann:
        return {"annotations": "none for this panel"}
    truth = {k: lettering._abs(v, rect) for k, v in ann["heads"].items()}
    found = {k: lettering._abs(v, rect) for k, v in detected.items()}
    per_char = {}
    for cid, t in truth.items():
        d = found.get(cid)
        if d is None or t is None:
            per_char[cid] = {"detected": False}
            continue
        inter = lettering.overlap(d, t)
        iou = inter / (d[2] * d[3] + t[2] * t[3] - inter)
        dc = (d[0] + d[2] / 2, d[1] + d[3] / 2)
        own = lettering._dist_to(dc, t)
        others = [lettering._dist_to(dc, o) for k, o in truth.items() if k != cid and o is not None]
        per_char[cid] = {"detected": True, "iou": round(iou, 2),
                         "centre_error_of_panel": round(((dc[0] - t[0] - t[2] / 2) ** 2 + (dc[1] - t[1] - t[3] / 2) ** 2) ** 0.5
                                                        / (rect[2] ** 2 + rect[3] ** 2) ** 0.5, 3),
                         "assigned_correctly": own == 0.0 or not others or own < min(others)}  # fmt: skip
    balloons = []
    keys = []
    for b in placed:
        cover = {k: round(lettering.overlap((b.x, b.y, b.w, b.h), lettering.face_core(t)) / (t[2] * t[3] * 0.7), 2)
                 for k, t in truth.items() if t is not None}  # fmt: skip
        row = {"text": b.text[:28], "type": b.type, "speaker": b.speaker, "flags": b.flags, "covers_true_face": cover}
        if b.tail is not None and b.speaker in truth and truth[b.speaker] is not None:
            to_sp = lettering._dist_to(b.tail, truth[b.speaker])  # type: ignore[arg-type]
            others = [lettering._dist_to(b.tail, t) for k, t in truth.items() if k != b.speaker and t is not None]
            row["tail_to_speaker"] = round(to_sp, 1)
            row["tail_to_nearest_other"] = round(min(others), 1) if others else None
            row["tail_attributed"] = not others or to_sp < min(others)
        balloons.append(row)
        if b.type != "sfx":
            keys.append(lettering.reading_key((b.x, b.y, b.w, b.h), rect))
    return {"characters": per_char, "balloons": balloons, "reading_order_ok": keys == sorted(keys)}


def overlay(image: bytes, rect: tuple[float, ...], detected: dict[str, Any], ann: dict[str, Any] | None,
            placed: list[Any], names: dict[str, str]) -> bytes:  # fmt: skip
    """The displayed (cropped) panel with detected faces (green, solid), hand-placed faces (magenta,
    thin) and the automatically placed balloons (blue outlines), all in panel coordinates."""
    from PIL import Image, ImageDraw

    from storypanel.pipeline import render

    with Image.open(io.BytesIO(image)) as raw:
        k = 600 / rect[2]
        im = render._cover(raw.convert("RGB"), 600, round(rect[3] * k))
    d = ImageDraw.Draw(im)

    def box(b: dict[str, float], colour: tuple[int, int, int], width: int, label: str) -> None:
        x0, y0 = b["x"] * im.width, b["y"] * im.height
        d.rectangle((x0, y0, x0 + b["w"] * im.width, y0 + b["h"] * im.height), outline=colour, width=width)
        d.text((x0 + 3, y0 + 2), label, fill=colour)

    for cid, b in (ann or {}).get("heads", {}).items():
        box(b, (230, 0, 200), 1, f"hand: {names.get(cid, cid)}")
    for cid, b in detected.items():
        box(b, (0, 200, 60), 3, f"detected: {names.get(cid, cid)}")
    for b in placed:
        d.rectangle(((b.x - rect[0]) * k, (b.y - rect[1]) * k, (b.x - rect[0] + b.w) * k, (b.y - rect[1] + b.h) * k),
                    outline=(30, 90, 255), width=2)  # fmt: skip
        if b.tail is not None:
            tx, ty = (b.tail[0] - rect[0]) * k, (b.tail[1] - rect[1]) * k
            d.ellipse((tx - 5, ty - 5, tx + 5, ty + 5), outline=(30, 90, 255), width=2)
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


async def run(pid: uuid.UUID, out: Path, approve: Decimal | None, annotations: Path | None, comic: str | None) -> int:
    from storypanel.catalog import styles
    from storypanel.config import get_settings
    from storypanel.errors import AppError
    from storypanel.pipeline import art, qa, render
    from storypanel.pipeline.stages_art import letter_panel, page_doc, panel_rect
    from storypanel.providers.base import CallContext

    live = get_settings().provider_mode == "live"
    if live != (approve is not None):
        print("refusing: --approve-usd with live providers, --simulated only with simulated ones", file=sys.stderr)
        return 2
    data = await load(pid)
    if data["storage_problems"]:
        print("refusing: stored images are missing or changed (run preflight)", file=sys.stderr)
        return 2
    started = datetime.now(UTC)
    font = styles().get(data["preset"]).font
    names = {cid: c.name for cid, c in data["cast"].items()}
    truth = annotation_index(annotations, comic, data["cast"]) if annotations and comic else {}
    results, auto, stop = [], {}, None
    for p, r in lettered(data):
        page, panel, spec = p["page"], r["panel"], r["spec"]
        cost = reservation(data, r)
        spent = sum((Decimal(str(x["cost_counted_usd"])) for x in await counted(pid, started)), Decimal(0))
        if approve is not None and spent + cost > approve:
            stop = (f"stopped before page {page.index + 1} panel {panel.index + 1}: counted USD {spent:.4f} + reserved "
                    f"USD {cost:.4f} would exceed the approved USD {approve}")  # fmt: skip
            break
        image, refs = call_inputs(data, r)
        rules = art.effective_rules(data["bible"].continuity_rules if data["bible"] else [], spec)
        try:
            vr = await qa.vision_report(image=image, panel=spec, cast=data["cast"], reference_images=refs, required=rules.must,
                                        elements=[(e.element, e.state) for e in spec.element_states],
                                        ctx=CallContext("face_check", str(data["project"].owner_id), str(pid),
                                                        target_id=str(panel.id)), operation="face_check")  # fmt: skip
        except AppError as exc:  # recorded by metering; not retried
            results.append({"page": page.index + 1, "panel": panel.index + 1, "error": exc.code.value, "message": exc.message})
            continue
        # exactly the stored qa_scores format, so letter_panel sees what production would
        scores = {"head_boxes": {o.id: o.head_box.model_dump() for o in vr.characters if o.present and o.head_box},
                  "body_boxes": {o.id: o.body_box.model_dump() for o in vr.characters if o.present and o.body_box},
                  "action_box": vr.action_box.model_dump() if vr.action_box else None}  # fmt: skip
        w, h = r["asset"].width, r["asset"].height
        rect = panel_rect(page, panel)
        placed, basis = letter_panel(spec=spec, rect=rect, page_size=p["template"].page, scores=scores,
                                     image_size=(w, h) if w and h else None, prompt_version="v4", kind=r["version"].kind,
                                     font=font, version_id=str(r["version"].id), image=image)  # fmt: skip
        auto[panel.id] = placed
        from storypanel.pipeline import lettering

        shown = {k: v for k, v in ((k, lettering.image_box_to_panel(b, (w, h) if w and h else None, (rect[2], rect[3]))[0])
                                   for k, b in scores["head_boxes"].items()) if v}  # fmt: skip
        ann = truth.get((page.index + 1, panel.index + 1))
        (out / f"overlay-p{page.index + 1}-{panel.index + 1}.png").write_bytes(overlay(image, rect, shown, ann, placed, names))
        results.append({
            "page": page.index + 1, "panel": panel.index + 1,
            "expected_faces": [c.id for c in spec.characters],
            "returned": {"present": [o.id for o in vr.characters if o.present],
                         "head_boxes_image_fractions": scores["head_boxes"]},
            "faces_unlocated": [o.id for o in vr.characters if o.present and o.head_box is None],
            "head_boxes_on_displayed_panel": shown, "basis": basis,
            "balloons": [{"text": b.text[:28], "flags": b.flags} for b in placed],
            "evaluation_vs_hand_annotation": evaluate(placed, shown, ann, rect),
        })  # fmt: skip
    # pages: as stored (before) and re-lettered automatically (after), PNG at reading size and print, and PDF
    from storypanel.db.session import transaction

    after_docs = []
    async with transaction() as db:
        for p in data["pages"]:
            doc = await page_doc(db, p["page"], p["all"], watermark=False, preset=data["preset"])
            n = p["page"].index + 1
            (out / f"page-{n}-before-reading-size.png").write_bytes(render.render_page(doc, 1000))
            panels = []
            for pn, pdoc in zip(p["all"], doc.panels, strict=True):
                placed = auto.get(pn.id)
                panels.append(pdoc if placed is None else render.PanelDoc(pdoc.rect, pdoc.image, tuple(
                    {"type": b.type, "text": b.text, "geometry": b.geometry(font), "order_index": b.index} for b in placed)))  # fmt: skip
            redone = render.PageDoc(doc.size, tuple(panels), doc.font, doc.sfx_font, doc.watermark)
            after_docs.append(redone)
            (out / f"page-{n}-after-auto-reading-size.png").write_bytes(render.render_page(redone, 1000))
            (out / f"page-{n}-after-auto.png").write_bytes(render.render_page(redone, 2000))
    (out / "after-auto.pdf").write_bytes(render.build_pdf(after_docs, [render.render_page(d, 2000, lettering=False)
                                                                       for d in after_docs], "face check"))  # fmt: skip
    ledger = await counted(pid, started)
    summary = {"comic_id": str(pid), "mode": "LIVE" if live else "SIMULATED", "approved_usd": str(approve) if approve else None,
               "started_at": started.isoformat(), "stopped": stop, "panels": results,
               "calls": [{k: str(v) if isinstance(v, Decimal) else v for k, v in x.items()} for x in ledger],
               "totals": {"calls": len(ledger),
                          "reserved_usd": str(sum((Decimal(str(x["cost_estimate_usd"])) for x in ledger), Decimal(0))),
                          "counted_usd": str(sum((Decimal(str(x["cost_counted_usd"])) for x in ledger), Decimal(0))),
                          "calculated_usd": str(sum((Decimal(str(x["cost_calculated_usd"] or 0)) for x in ledger), Decimal(0))),
                          "unresolved": sum(1 for x in ledger if x["cost_basis"] == "unresolved" or x["status"] == "ambiguous"),
                          "input_tokens": sum(x["input_tokens"] for x in ledger),
                          "output_tokens": sum(x["output_tokens"] for x in ledger)}}  # fmt: skip
    (out / "face-check.json").write_text(json.dumps(summary, indent=1, default=str))
    print(json.dumps(summary["totals"], indent=1))
    if stop:
        print(stop)
    print(f"-> {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    pf = sub.add_parser("preflight")
    pf.add_argument("comic_id", type=uuid.UUID)
    rn = sub.add_parser("run")
    rn.add_argument("comic_id", type=uuid.UUID)
    rn.add_argument("out", type=Path)
    g = rn.add_mutually_exclusive_group(required=True)
    g.add_argument("--approve-usd", type=Decimal)
    g.add_argument("--simulated", action="store_true")
    rn.add_argument("--annotations", type=Path)
    rn.add_argument("--comic", choices=["train", "tulips"])
    a = ap.parse_args()
    if a.cmd == "preflight":
        return asyncio.run(preflight(a.comic_id))
    if a.out.exists():
        raise SystemExit(f"refusing: {a.out} already exists (outputs are never overwritten)")
    if a.approve_usd is not None and a.approve_usd > Decimal("0.20"):
        raise SystemExit("refusing: stage 1A is approved for at most USD 0.20")
    a.out.mkdir(parents=True)
    return asyncio.run(run(a.comic_id, a.out, a.approve_usd, a.annotations, a.comic))


if __name__ == "__main__":
    sys.exit(main())
