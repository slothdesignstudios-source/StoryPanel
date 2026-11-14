"""Balloon text must sit inside the shape that is actually drawn, with a margin.

Measured on rendered pixels, not on the arithmetic: each balloon is rendered on coloured art with
and without its text; the text's ink (the difference) must lie inside the balloon's white
interior shrunk by a margin. Covers speech and thought balloons and captions, several panel
sizes, short and long lines, the longest allowed word, multi-line text and Latin-1 glyphs.

This proves the geometry and the export renderer agree. It is not a visual review of real comic
pages (that needs generated art and a person looking).
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from PIL import Image, ImageChops, ImageFilter

from storypanel.pipeline import lettering, render

TEXTS = {
    "short": "Hi!",
    "sentence": "I calculated you were hungry.",
    "max_words": " ".join(["word"] * 25),
    "long_word": "Supercalifragilisticexpi is long",  # 24-character word: the longest allowed
    "glyphs": "Ça va? Ñandú, Øresund, Æble, déjà vu: «très bien» – ¡olé!",
    "question": "You made this, just for me, on a night like this?",
}
PANELS = {"strip_narrow": (40.0, 40.0, 322.0, 480.0), "page": (40.0, 40.0, 448.0, 698.0),
          "wide": (40.0, 40.0, 920.0, 540.0)}  # fmt: skip
PAGE = {"strip_narrow": (1600, 560), "page": (1000, 1500), "wide": (1000, 1500)}
WIDTH_PX = 2400
MARGIN_PX = 3  # at this resolution ~2.5 page units of clear space between text and the outline


def _art(color: tuple[int, int, int]) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (400, 300), color).save(out, format="PNG")
    return out.getvalue()


def _balloon(kind: str, text: str, panel: str) -> dict[str, Any]:
    (placed,) = lettering.layout_panel(
        panel_rect=PANELS[panel], page_size=PAGE[panel],
        dialogue=[{"type": kind, "text": text, "speaker": "kiro" if kind != "caption" else None}],
        characters=[{"id": "kiro", "position": "center"}])  # fmt: skip
    return {"type": kind, "text": text, "geometry": placed.geometry("comic_neue"), "order_index": 0}


def _render(b: dict[str, Any], panel: str) -> Image.Image:
    doc = render.PageDoc(PAGE[panel], (render.PanelDoc(PANELS[panel], _art((40, 90, 150)), (b,)),))
    return Image.open(io.BytesIO(render.render_page(doc, WIDTH_PX))).convert("RGB")


def text_outside_shape(kind: str, text: str, panel: str) -> int:
    """Pixels of text ink that fall outside the balloon interior shrunk by MARGIN_PX."""
    b = _balloon(kind, text, panel)
    with_text = _render(b, panel)
    blank = _render({**b, "text": "", "geometry": {**b["geometry"], "lines": []}}, panel)
    ink = ImageChops.difference(with_text, blank).convert("L").point(lambda v: 255 if v > 60 else 0)
    fill = (255, 244, 200) if kind == "caption" else (255, 255, 255)
    # the interior: pixels painted exactly with the balloon's fill colour, inside the balloon's box
    chans = [c.point(lambda v, t=t: 255 if v == t else 0) for c, t in zip(blank.split(), fill, strict=True)]
    mask = ImageChops.multiply(ImageChops.multiply(chans[0], chans[1]), chans[2])
    k = WIDTH_PX / PAGE[panel][0]
    g = b["geometry"]
    box = Image.new("L", blank.size, 0)
    box.paste(
        255,
        (
            int(g["x"] * k) - 4,
            int(g["y"] * k) - 4,
            int((g["x"] + g["w"]) * k) + 4,
            int((g["y"] + g["h"]) * k) + 4,
        ),
    )
    mask = ImageChops.multiply(mask, box)
    safe = mask.filter(ImageFilter.MinFilter(2 * MARGIN_PX + 1))  # shrink the interior by the margin
    outside = ImageChops.subtract(ink, safe)
    return int(outside.histogram()[255]) + sum(outside.histogram()[1:255])


@pytest.mark.parametrize("panel", list(PANELS))
@pytest.mark.parametrize("kind", ["speech", "thought", "caption"])
@pytest.mark.parametrize("name", list(TEXTS))
def test_text_stays_inside_the_drawn_balloon(kind: str, name: str, panel: str) -> None:
    assert text_outside_shape(kind, TEXTS[name], panel) == 0


def test_web_text_box_matches_the_server() -> None:
    """The editor fits text with the same insets as the server and the export renderer."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[3] / "apps/web/src/lib/text.ts").read_text()
    assert f"export const ELLIPSE_INSET = {lettering.ELLIPSE_INSET};" in src
    assert f"export const THOUGHT_INSET = {lettering.THOUGHT_INSET};" in src
    assert f"export const BOX_PAD = {int(lettering.BOX_PAD)};" in src
