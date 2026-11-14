"""Lettering stays inside the print safe area (0.125 in inside the trim) on every page, in every
writer of balloon geometry (automatic lettering, editor moves, layout changes) and in the
rendered PDF itself. Lettering cut off by trimming is a defect, not a rasterisation difference."""

from __future__ import annotations

import io
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageChops

from storypanel.catalog import layouts
from storypanel.domain.schemas import ComicFormat
from storypanel.domain.validation import validate_script
from storypanel.pipeline import lettering, render
from storypanel.services import editor

LONG = ("Listen, I have kept this light burning for forty years and no gull is taking my last sandwich "
        "tonight, understood?")  # 25 words, the per-balloon maximum  # fmt: skip
NON_ASCII = "¿Qué? ¡Sí! Café, naïve façade — «Ærø» … “déjà vu” ½ £5"
EDGE_HEADS = [  # speakers pushed into each corner and edge of the panel
    {"x": 0.0, "y": 0.0, "w": 0.18, "h": 0.2}, {"x": 0.82, "y": 0.0, "w": 0.18, "h": 0.2},
    {"x": 0.0, "y": 0.8, "w": 0.18, "h": 0.2}, {"x": 0.82, "y": 0.8, "w": 0.18, "h": 0.2},
    {"x": 0.41, "y": 0.0, "w": 0.18, "h": 0.2},
]  # fmt: skip


def _dialogue(variant: int) -> list[dict[str, Any]]:
    lines = [
        [{"type": "caption", "text": LONG}, {"type": "speech", "speaker": "a", "text": LONG},
         {"type": "sfx", "text": "KA-BOOOOOOOM"}],
        [{"type": "speech", "speaker": "a", "text": NON_ASCII}, {"type": "thought", "speaker": "b", "text": LONG},
         {"type": "caption", "text": "Later — at the very edge of the page."}],
        [{"type": "sfx", "text": "SPLAAAAAAASH"}, {"type": "speech", "speaker": "b", "text": LONG}],
    ]  # fmt: skip
    return lines[variant % len(lines)]


def _panel_lettering(template: Any, variant: int) -> list[lettering.Placed]:
    placed: list[lettering.Placed] = []
    for i, rect in enumerate(template.rects):
        heads = {
            "a": EDGE_HEADS[(i + variant) % len(EDGE_HEADS)],
            "b": EDGE_HEADS[(i + variant + 2) % len(EDGE_HEADS)],
        }
        placed += lettering.layout_panel(
            panel_rect=tuple(rect), page_size=tuple(template.page), dialogue=_dialogue(variant + i),
            characters=[{"id": "a", "position": "left"}, {"id": "b", "position": "right"}],
            head_boxes=heads, action_box={"x": 0.9, "y": 0.9, "w": 0.1, "h": 0.1})  # fmt: skip
    return placed


@pytest.mark.parametrize("template_id", sorted(layouts()))
def test_automatic_lettering_never_leaves_the_safe_area(template_id: str) -> None:
    t = layouts()[template_id]
    for variant in range(3):
        for p in _panel_lettering(t, variant):
            g = p.geometry()
            assert not lettering.outside_safe_area(p.type, p.text, g, tuple(t.page)), (
                template_id,
                p.type,
                p.text[:30],
                lettering.ink_bounds(p.type, p.text, g),
                lettering.safe_rect(tuple(t.page)),
            )


@pytest.mark.parametrize("kind", ["speech", "thought", "caption", "sfx"])
def test_editor_moves_are_clamped_to_the_safe_area(kind: str) -> None:
    page = (1600, 560)
    for x, y, tail in [
        (-500, -500, (-50, -50)),
        (5000, 5000, (9000, 9000)),
        (0, 0, (0, 560)),
        (1590, 0, (1600, 0)),
    ]:
        g = {"x": x, "y": y, "w": 300, "h": 140, "font_size": 54 if kind == "sfx" else 22, "rotation": -8 if kind == "sfx" else 0,
             "tail": {"x": tail[0], "y": tail[1]} if kind in ("speech", "thought") else None}  # fmt: skip
        out = editor._clamp_geometry(g, page, kind, "KA-BOOOOOOOM" if kind == "sfx" else "Hello")
        assert not lettering.outside_safe_area(
            kind, "KA-BOOOOOOOM" if kind == "sfx" else "Hello", out, page
        ), out
    # a box larger than the safe area is shrunk to fit, not left hanging over the trim
    huge = editor._clamp_geometry(
        {"x": 0, "y": 0, "w": 4000, "h": 4000, "font_size": 22}, page, "caption", "x"
    )
    assert not lettering.outside_safe_area("caption", "x", huge, page)


def test_characters_the_fonts_cannot_draw_are_refused_before_anything_is_paid_for() -> None:
    from tests.test_unit import KIRO, LOC, MEI, script

    assert lettering.unsupported_chars(NON_ASCII) == ""  # Latin-1 and typographic punctuation are covered
    assert set(lettering.unsupported_chars("Łódź 灯台 🙂")) == set("Łź灯台🙂")  # would print as boxes
    bad = script(pages=1, panels=3, dialogue=[{"type": "speech", "speaker": "kiro", "text": "Привет, Łódź"}])
    codes = {i.code for i in validate_script(bad, fmt=ComicFormat.STRIP, expected_pages=1,
                                             character_ids={KIRO.id, MEI.id}, location_ids={LOC.id})}  # fmt: skip
    assert "unsupported_characters" in codes  # an error: the script cannot be approved, no panel is paid for
    assert (
        "unsupported_characters"
        in lettering.typeset("speech", "Łódź", {"x": 0, "y": 0, "w": 200, "h": 100})[1]
    )


# ------------------------------------------------------------------ the rendered PDF itself
def _art(w: int, h: int, color: tuple[int, int, int]) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (w, h), color).save(out, format="PNG")
    return out.getvalue()


def representative_pages() -> dict[str, render.PageDoc]:
    """Long dialogue, SFX, tails toward edges and corners, non-ASCII text; a page and a strip."""
    out = {}
    for name, tid, variant in (("page-long-dialogue", "w4_grid", 0), ("page-nonascii-edges", "w5_top_large", 1),
                               ("strip-sfx-tails", "s3_row", 2), ("strip-wide-last", "s4_wide_last", 0)):  # fmt: skip
        t = layouts()[tid]
        placed = _panel_lettering(t, variant)
        panels = []
        colors = [(40, 90, 150), (190, 120, 60), (70, 140, 90), (150, 70, 120), (90, 90, 90)]
        for i, rect in enumerate(t.rects):
            mine = [p for p in placed if _inside(p, rect)]
            balloons = tuple({"type": p.type, "text": p.text, "geometry": p.geometry(), "order_index": n}
                             for n, p in enumerate(mine))  # fmt: skip
            panels.append(render.PanelDoc(tuple(rect), _art(400, 300, colors[i % len(colors)]), balloons))
        out[name] = render.PageDoc(tuple(t.page), tuple(panels))
    return out


def _inside(p: lettering.Placed, rect: list[float]) -> bool:
    # each panel's lettering was produced from that panel's rect; attribute by the box centre
    cx, cy = p.x + p.w / 2, p.y + p.h / 2
    x, y, w, h = rect
    m = max(w, h) * 0.2  # border-breaking balloons may sit a little outside
    return x - m <= cx <= x + w + m and y - m <= cy <= y + h + m


def _raster(pdf: bytes, tmp: Path, name: str, dpi: float) -> Image.Image:
    (tmp / f"{name}.pdf").write_bytes(pdf)
    subprocess.run(["pdftoppm", "-r", f"{dpi:.4f}", "-png", "-singlefile", str(tmp / f"{name}.pdf"), str(tmp / name)],
                   check=True)  # fmt: skip
    return Image.open(tmp / f"{name}.png").convert("L")


def lettering_ink_outside_safe_area(doc: render.PageDoc, tmp: Path) -> tuple[int, Image.Image, Image.Image]:
    """Rasterise the PDF (whole media box, bleed included) with and without lettering; every
    differing pixel is lettering ink. Returns (ink pixels outside the safe area, with, ink mask)."""
    width = 800
    art = render.render_page(doc, width, lettering=False)
    bare = replace(doc, panels=tuple(replace(p, balloons=()) for p in doc.panels))
    dpi = width / (render.PDF_TRIM_WIDTH_PT / 72)
    full = _raster(render.build_pdf([doc], [art], "t"), tmp, "with", dpi)
    none = _raster(render.build_pdf([bare], [art], "t"), tmp, "without", dpi)
    ink = ImageChops.difference(full, none).point(lambda v: 255 if v > 40 else 0)
    px_per_unit = width / doc.size[0]
    bleed = render.PDF_BLEED_PT / 72 * dpi
    sx, sy, sw, sh = lettering.safe_rect((float(doc.size[0]), float(doc.size[1])))
    box = (round(bleed + sx * px_per_unit) - 1, round(bleed + sy * px_per_unit) - 1,
           round(bleed + (sx + sw) * px_per_unit) + 1, round(bleed + (sy + sh) * px_per_unit) + 1)  # fmt: skip
    outside = ink.copy()
    outside.paste(0, box)
    return outside.histogram()[255], full, ink


@pytest.mark.skipif(not shutil.which("pdftoppm"), reason="pdftoppm not installed")
@pytest.mark.parametrize(
    "name", ["page-long-dialogue", "page-nonascii-edges", "strip-sfx-tails", "strip-wide-last"]
)
def test_no_lettering_ink_outside_the_safe_area_in_the_pdf(name: str, tmp_path: Path) -> None:
    from pypdf import PdfReader

    doc = representative_pages()[name]
    n, _, ink = lettering_ink_outside_safe_area(doc, tmp_path)
    assert ink.histogram()[255] > 1000  # the page really has lettering
    assert n == 0, f"{n} lettering pixels outside the safe area (trim/bleed)"
    if "nonascii" in name:  # real text with the right characters, not boxes
        text = PdfReader(io.BytesIO(render.build_pdf([doc], [render.render_page(doc, 800, lettering=False)], "t"))
                         ).pages[0].extract_text()  # fmt: skip
        for word in ("¿Qué?", "¡Sí!", "Café", "naïve", "façade", "«Ærø»"):
            assert word in text, (word, text[:200])


@pytest.mark.parametrize("template_id", sorted(layouts()))
def test_lettering_never_silently_covers_other_lettering(template_id: str) -> None:
    """Captions take a free corner and SFX a free spot; when a panel is simply too full (here:
    three 25-word balloons), the overlap is flagged for the user instead of hidden text."""
    t = layouts()[template_id]
    for variant in range(3):
        for i, rect in enumerate(t.rects):
            heads = {"a": EDGE_HEADS[(i + variant) % 5], "b": EDGE_HEADS[(i + variant + 2) % 5]}
            ps = lettering.layout_panel(
                panel_rect=tuple(rect), page_size=tuple(t.page), dialogue=_dialogue(variant + i),
                characters=[{"id": "a", "position": "left"}, {"id": "b", "position": "right"}],
                head_boxes=heads, action_box={"x": 0.9, "y": 0.9, "w": 0.1, "h": 0.1})  # fmt: skip
            for a in range(len(ps)):
                for b in range(a + 1, len(ps)):
                    if lettering.overlap(lettering._ink_rect(ps[a]), lettering._ink_rect(ps[b])) > 0:
                        assert "overlaps_lettering" in ps[b].flags, (
                            template_id,
                            variant,
                            i,
                            ps[a].type,
                            ps[b].type,
                        )


def test_an_ordinary_strip_letters_without_any_overlap() -> None:
    """Typical dialogue (a caption, two short lines, one SFX per panel) never needs the flag."""
    t = layouts()["s3_row"]
    for i, rect in enumerate(t.rects):
        ps = lettering.layout_panel(
            panel_rect=tuple(rect), page_size=tuple(t.page),
            dialogue=[{"type": "caption", "text": "The pier, at noon."},
                      {"type": "speech", "speaker": "a", "text": "That sandwich is mine, you feathered thief!"},
                      {"type": "speech", "speaker": "b", "text": "Finders keepers."},
                      {"type": "sfx", "text": "SQUAWK"}],
            characters=[{"id": "a", "position": "left"}, {"id": "b", "position": "right"}])  # fmt: skip
        assert not [f for p in ps for f in p.flags], [(p.type, p.flags) for p in ps]
        for a in range(len(ps)):
            for b in range(a + 1, len(ps)):
                assert lettering.overlap(lettering._ink_rect(ps[a]), lettering._ink_rect(ps[b])) == 0


def test_web_character_list_matches_the_fonts() -> None:
    """The browser checks the same set the server enforces (regenerate with scripts/gen-lettering-charset.py)."""
    import importlib.util

    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location("gen", root / "scripts/gen-lettering-charset.py")
    gen = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(gen)  # type: ignore[union-attr]
    assert (root / "apps/web/src/lib/lettering-charset.ts").read_text() == gen.render()


def test_unsupported_characters_message_is_actionable_and_honest() -> None:
    m = lettering.unsupported_message("Łő")
    assert m.startswith("These characters can't be lettered yet: Ł, ő.")
    assert "Latin-1" in m and "Replace or remove them" in m and "never changed for you" in m
    assert "unicode" not in m.lower()  # accented Latin is not described as full Unicode support


async def test_planning_input_may_use_characters_the_fonts_cannot_letter(
    client: Any, headers: dict[str, str]
) -> None:
    """Owner decision 2026-11-14: the idea is planning-only input, never lettered itself, so it may
    contain Ł; it is stored exactly as typed. Rendered text (dialogue, balloons) stays enforced."""
    from storypanel.providers import fake

    fake.control.reset()
    r = await client.post(
        "/v1/projects", json={"prompt": "Łukasz and his cat explore Kraków at night"}, headers=headers
    )
    assert r.status_code == 201, r.text
    p = (await client.get(f"/v1/projects/{r.json()['id']}", headers=headers)).json()
    assert p["prompt"] == "Łukasz and his cat explore Kraków at night"  # never transliterated
    panel = await client.post("/v1/projects", json={"prompt": "x"}, headers=headers)
    assert panel.status_code == 422  # ordinary validation still applies
