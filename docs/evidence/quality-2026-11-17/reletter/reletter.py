#!/usr/bin/env python3
"""Re-letter two real exported pages WITHOUT regenerating their artwork (no provider calls).

    services/backend/.venv/bin/python docs/evidence/quality-2026-11-17/reletter/reletter.py ART_DIR OUT_DIR

ART_DIR holds train.png and tulips.png: the art layer extracted from each exported PDF
(`pdfimages -png comic.pdf art`; the PDF keeps lettering as vectors, so its image has none).
For each page it letters every panel in annotations.json twice with the current letterer:
  - "guessed": face positions guessed from the script, as happened in the export;
  - "located": face and prop positions from annotations.json (read off the art by eye);
and writes PNG pages (the app's PNG renderer), one PDF (the app's PDF exporter) and the flags and
export warnings the app would show. Panels not in annotations.json have no lettering.
"""

import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "services/backend/src"))

from PIL import Image  # noqa: E402

from storypanel.catalog import layouts  # noqa: E402
from storypanel.pipeline import lettering, render  # noqa: E402
from storypanel.pipeline.stages_art import lettering_problems, merge_print_quality  # noqa: E402

PT_PER_UNIT, BLEED_PT, ART_PX_PER_PT = 0.432, 9.0, 2084 / 450


def crop(art: Image.Image, r: tuple[float, ...]) -> bytes:
    def px(u: float) -> int:
        return int((BLEED_PT + u * PT_PER_UNIT) * ART_PX_PER_PT)

    out = io.BytesIO()
    art.crop((px(r[0]), px(r[1]), px(r[0] + r[2]), px(r[1] + r[3]))).save(out, "PNG")
    return out.getvalue()


def boxes(d: dict[str, list[float]] | None) -> dict[str, dict[str, float]] | None:
    return {k: dict(zip("xywh", v, strict=True)) for k, v in d.items()} if d else None


LAST: dict[int, list[lettering.Placed]] = {}


def stamp(png: bytes, label: str) -> bytes:
    """Write how the page was produced across its top, so no image can be mistaken for another kind."""
    from PIL import ImageDraw

    im = Image.open(io.BytesIO(png)).convert("RGB")
    band = max(24, im.width // 45)
    out = Image.new("RGB", (im.width, im.height + band), (255, 236, 160))
    out.paste(im, (0, band))
    ImageDraw.Draw(out).text((10, band // 4), label, fill=(0, 0, 0))
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return buf.getvalue()


def attribution(spec: dict, t: object) -> list[dict]:
    """Per balloon, measured against the HAND-ANNOTATED faces: where its tail ends relative to the
    speaker's face and the nearest other face (page units), and its reading position."""
    rows = []
    for n, placed in LAST.items():
        ann = spec["panels"][str(n + 1)]
        rect = tuple(float(v) for v in t.rects[n])  # type: ignore[attr-defined]
        faces = {k: lettering._abs(dict(zip("xywh", v, strict=True)), rect) for k, v in ann["heads"].items()}
        for order, b in enumerate(placed, start=1):
            if b.tail is None or b.speaker not in faces:
                continue
            to_speaker = lettering._dist_to(b.tail, faces[b.speaker])  # type: ignore[arg-type]
            others = [lettering._dist_to(b.tail, f) for k, f in faces.items() if k != b.speaker]  # type: ignore[arg-type]
            rows.append({"panel": n + 1, "balloon": order, "speaker": b.speaker, "text": b.text[:24],
                         "tail_to_speaker_face": round(to_speaker, 1),
                         "tail_to_nearest_other_face": round(min(others), 1) if others else None})  # fmt: skip
    return rows


def main() -> int:
    art_dir, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    notes = json.loads((Path(__file__).parent / "annotations.json").read_text())
    report = {}
    for comic in ("train", "tulips"):
        spec = notes[comic]
        t = layouts()[spec["template"]]
        art = Image.open(art_dir / f"{comic}.png").convert("RGB")
        for mode in ("guessed", "located"):
            LAST.clear()
            panels, snap = [], {"size": list(t.page), "panels": []}
            for i, r in enumerate(t.rects):
                rect = tuple(float(v) for v in r)
                image = crop(art, rect)
                ann = spec["panels"].get(str(i + 1))
                balloons = []
                if ann:
                    placed = lettering.layout_panel(
                        panel_rect=rect, page_size=t.page,
                        dialogue=[{"type": k, "speaker": s, "text": x} for k, s, x in ann["dialogue"]],
                        characters=[{"id": c, "position": p} for c, p in ann["characters"]],
                        head_boxes=boxes(ann["heads"]) if mode == "located" else None,
                        body_boxes=boxes(ann.get("bodies")) if mode == "located" else None,
                        prop_boxes=boxes(ann.get("props")) if mode == "located" else None,
                        detail=lettering.art_detail(image, rect[2], rect[3]))  # fmt: skip
                    balloons = [{"type": b.type, "text": b.text, "geometry": b.geometry(), "order_index": b.index,
                                 "flags": b.flags} for b in placed]  # fmt: skip
                    LAST[i] = placed
                panels.append(render.PanelDoc(rect, image, tuple(balloons)))
                snap["panels"].append({"balloons": balloons})
            doc = render.PageDoc(t.page, tuple(panels))
            label = {"guessed": "AUTOMATIC - faces guessed from the script (as exported)",
                     "located": "MANUAL face positions typed in by hand (stand-in for the panel check)"}[mode]
            (out / f"{comic}-{mode}.png").write_bytes(stamp(render.render_page(doc, 2000), label))
            (out / f"{comic}-{mode}-reading-size.png").write_bytes(stamp(render.render_page(doc, 1000), label))
            pdf = render.build_pdf([doc], [render.render_page(doc, 2000, lettering=False)], f"{comic} {mode}")
            (out / f"{comic}-{mode}.pdf").write_bytes(pdf)
            rows = lettering_problems(0, snap)
            report[f"{comic}-{mode}"] = {"provenance": label, "flags": rows, "export_warnings": merge_print_quality(
                None, 0, [], "local_resample", flagged=rows)["warnings"],
                "attribution": attribution(spec, t)}  # fmt: skip
    (out / "report.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
