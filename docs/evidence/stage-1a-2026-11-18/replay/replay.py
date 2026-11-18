#!/usr/bin/env python3
"""Free replay: re-letter the existing Train and Tulips artwork from the face boxes the stage 1A run
STORED, with the letterer before and after the fix. No artwork is generated or changed, and no provider
is called. Originals are never written to.

    services/backend/.venv/bin/python docs/evidence/stage-1a-2026-11-18/replay/replay.py ART_DIR OUT_DIR [LETTERING_BEFORE.py]

ART_DIR holds train.png and tulips.png: the art layer of each exported PDF
(`pdfimages -png comic.pdf art`; the PDF keeps lettering as vectors, so its image has none).

Inputs, and how each is labelled on every image and in replay.json:
  - STORED DETECTION: head boxes from ../train-face-check.json and ../tulips-face-check.json, the live
    stage 1A run (head_boxes_on_displayed_panel). The run's body and action boxes were not saved, so
    the replay uses heads only. On panels where those boxes mattered (Train 1 and 5), the "before"
    replay does not match the live run, and the comparison is reported as limited.
  - SCRIPT: characters, their positions and the dialogue, as printed by the owner's preflight.
  - MANUAL ANNOTATION (../../quality-2026-11-17/reletter/annotations.json): read only AFTER placement,
    to measure. Never an input.
"""

import importlib.util
import io
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT / "services/backend/src"))

from PIL import Image, ImageDraw  # noqa: E402

from storypanel.catalog import layouts  # noqa: E402
from storypanel.pipeline import lettering as after_fix  # noqa: E402
from storypanel.pipeline import render  # noqa: E402

PT_PER_UNIT, BLEED_PT, ART_PX_PER_PT = 0.432, 9.0, 2084 / 450
STORED = {"train": HERE.parent / "train-face-check.json", "tulips": HERE.parent / "tulips-face-check.json"}
ANNOTATIONS = ROOT / "docs/evidence/quality-2026-11-17/reletter/annotations.json"
TEMPLATE = {"train": "w6_bottom_large", "tulips": None}
# from the owner's preflight output (the approved script), 2026-11-18
SCRIPT = {
    "train": {
        1: ([("mira", "left"), ("teo", "right")], [("sfx", None, "Chuff... chuff...")]),
        2: ([("mira", "left"), ("teo", "right")], [("speech", "teo", "Missed it again! Ha... sorry!"),
                                                    ("speech", "mira", "It's... okay.")]),
        3: ([("mira", "center")], [("thought", "mira", "Every day, the same boy...")]),
        4: ([("teo", "right"), ("mira", "left")], [("speech", "teo", "Delayed! I can make it!")]),
        5: ([("mira", "left")], [("speech", "mira", "Oh...")]),
        6: ([("teo", "right"), ("mira", "left")], [("speech", "mira", "Tomorrow... same table?")]),
    },
    "tulips": {3: ([("old_man", "left")], [("speech", "old_man", "Oh... you remembered.")])},
}
ALIAS = {"walter": "old_man"}  # the annotation names Walter; his id in the comic is old_man


def load_before(path: Path):  # noqa: ANN201
    spec = importlib.util.spec_from_file_location("storypanel.pipeline.lettering_before", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses look their module up while being defined
    spec.loader.exec_module(mod)
    return mod


def crop(art: Image.Image, r: tuple[float, ...]) -> bytes:
    def px(u: float) -> int:
        return int((BLEED_PT + u * PT_PER_UNIT) * ART_PX_PER_PT)

    out = io.BytesIO()
    art.crop((px(r[0]), px(r[1]), px(r[0] + r[2]), px(r[1] + r[3]))).save(out, "PNG")
    return out.getvalue()


def stamp(png: bytes, label: str) -> bytes:
    im = Image.open(io.BytesIO(png)).convert("RGB")
    band = max(26, im.width // 40)
    out = Image.new("RGB", (im.width, im.height + band), (24, 32, 43))
    out.paste(im, (0, band))
    ImageDraw.Draw(out).text((10, band // 4), label, fill=(255, 255, 255))
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return buf.getvalue()


def measure(lt, placed, ann_heads, rect) -> dict:  # noqa: ANN001
    """Against the MANUAL annotation (comparison only)."""
    truth = {ALIAS.get(k, k): lt._abs(dict(zip("xywh", v, strict=True)), rect) for k, v in ann_heads.items()}
    rows = []
    for b in placed:
        cover = {k: round(lt.overlap((b.x, b.y, b.w, b.h), lt.face_core(t)) / (t[2] * t[3] * 0.7), 2)
                 for k, t in truth.items()}  # fmt: skip
        row = {"text": b.text[:28], "flags": list(b.flags), "covers_annotated_face": cover}
        if b.tail is not None and b.speaker in truth:
            own = lt._dist_to(b.tail, truth[b.speaker])
            others = [lt._dist_to(b.tail, t) for k, t in truth.items() if k != b.speaker]
            row["tail_to_speaker"] = round(own, 1)
            row["tail_to_nearest_other"] = round(min(others), 1) if others else None
        rows.append(row)
    return {"source": "manual annotation (comparison only)", "balloons": rows}


def main() -> int:
    art_dir, out = Path(sys.argv[1]), Path(sys.argv[2])
    before_fix = load_before(Path(sys.argv[3])) if len(sys.argv) > 3 else None
    out.mkdir(parents=True, exist_ok=True)
    notes = json.loads(ANNOTATIONS.read_text())
    report: dict = {"inputs": {"faces": "STORED DETECTION (stage 1A run, head boxes only)",
                               "characters_and_dialogue": "SCRIPT (owner's preflight)",
                               "annotations": "MANUAL, comparison only"}, "pages": {}}  # fmt: skip
    versions = [("after-fix", after_fix)] + ([("before-fix", before_fix)] if before_fix else [])
    for comic in ("train", "tulips"):
        stored = {p["panel"]: p["head_boxes_on_displayed_panel"] for p in json.loads(STORED[comic].read_text())["panels"]}
        t = layouts()[notes[comic]["template"]]
        art = Image.open(art_dir / f"{comic}.png").convert("RGB")
        for name, lt in versions:
            panels, rows = [], {}
            for i, r in enumerate(t.rects):
                n = i + 1
                rect = tuple(float(v) for v in r)
                image = crop(art, rect)
                balloons: tuple = ()
                if n in SCRIPT[comic]:
                    chars, lines = SCRIPT[comic][n]
                    placed = lt.layout_panel(
                        panel_rect=rect, page_size=t.page,
                        dialogue=[{"type": k, "speaker": s, "text": x} for k, s, x in lines],
                        characters=[{"id": c, "position": p} for c, p in chars],
                        head_boxes=stored.get(n), detail=lt.art_detail(image, rect[2], rect[3]))  # fmt: skip
                    balloons = tuple({"type": b.type, "text": b.text, "geometry": b.geometry(), "order_index": b.index}
                                     for b in placed)  # fmt: skip
                    ann = notes[comic]["panels"].get(str(n))
                    rows[f"panel {n}"] = {
                        "flags": sorted({f for b in placed for f in b.flags}),
                        "measured": measure(lt, placed, ann["heads"], rect) if ann else
                        "unavailable: no manual annotation for this panel; judge from the page",
                    }
                panels.append(render.PanelDoc(rect, image, balloons))
            doc = render.PageDoc(t.page, tuple(panels))
            label = (f"REPLAY ({'letterer AFTER the fix' if name == 'after-fix' else 'letterer BEFORE the fix'}): "
                     "existing artwork + STORED stage 1A face boxes; no provider calls")  # fmt: skip
            (out / f"{comic}-{name}-reading-size.png").write_bytes(stamp(render.render_page(doc, 1000), label))
            report["pages"][f"{comic}-{name}"] = rows
    (out / "replay.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
