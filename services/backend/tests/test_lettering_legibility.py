"""Minimum readable lettering: defined in output terms, never silently undercut, same in every output.

The floor is lettering.READABLE_MIN page units: 6.48 pt on the 6 x 9 in PDF page and 30 px in the
2000 px PNG/CBZ page; 18.75 px in a strip PNG and 4.05 pt if a strip is printed 6 in wide (below the
floor, so the export reports a print warning). When text cannot fit at the floor, typesetting keeps
the floor and flags "overflow" (shorten, move or enlarge) instead of shrinking further.

Fixtures: the representative pages of test_print_safe_area and the balloon texts and panels of
test_balloon_text_bounds, with flat colour art. This checks sizes and parity of the three renderers;
it is not a readability review of real generated comics (no real art exists yet).
"""

from __future__ import annotations

import io
import re
import uuid
from pathlib import Path

import pytest
from PIL import Image, ImageChops

from storypanel.pipeline import lettering, render
from storypanel.pipeline.stages_art import EXPORT_WIDTH, lettering_sizes, merge_print_quality
from tests.test_balloon_text_bounds import PAGE, PANELS, TEXTS
from tests.test_print_safe_area import representative_pages

LONG = " ".join(["extraordinarily"] * 25)


def test_the_floor_in_output_terms() -> None:
    assert lettering.READABLE_MIN == lettering.FONT_MIN == 15.0
    assert lettering.MIN_PRINT_PT == 6.48
    assert lettering.printed_pt(15, 1000) == 6.48  # 6 x 9 in page
    assert lettering.printed_pt(15, 1600) == 4.05  # a strip printed 6 in wide: below the floor
    assert lettering.printed_pt(lettering.FONT_MAX, 1600) < lettering.MIN_PRINT_PT  # even the largest
    assert 15 * EXPORT_WIDTH / 1000 == 30 and 15 * EXPORT_WIDTH / 1600 == 18.75


@pytest.mark.parametrize("kind", ["speech", "thought", "caption"])
def test_text_is_never_set_below_the_floor_it_overflows_instead(kind: str) -> None:
    for w in range(40, 420, 20):
        for h in range(30, 260, 20):
            for text in (*TEXTS.values(), LONG):
                g, flags = lettering.typeset(kind, text, {"w": w, "h": h, "font_size": 22})
                assert g["text_size"] >= lettering.READABLE_MIN, (kind, w, h, text)
                _, bw, bh = lettering.text_block(" ".join(g["lines"]), "comic_neue", g["text_size"], 1e9)
                iw, ih = lettering.inner_box(kind, w, h)
                fits = lettering.fit_text(text, "comic_neue", iw, ih, g["text_size"], g["text_size"])[2]
                assert ("overflow" in flags) == (not fits), (kind, w, h, text)
    # a stored font size below the floor (old data, a hand-made request) is raised, not honoured
    g, _ = lettering.typeset("speech", "Hi.", {"w": 200, "h": 120, "font_size": 9})
    assert g["text_size"] == lettering.READABLE_MIN


def test_automatic_lettering_on_the_fixtures_respects_the_floor() -> None:
    for panel, rect in PANELS.items():
        for kind in ("speech", "thought", "caption"):
            for text in TEXTS.values():
                (p,) = lettering.layout_panel(panel_rect=rect, page_size=PAGE[panel],
                                              dialogue=[{"type": kind, "text": text, "speaker": "kiro"}],
                                              characters=[{"id": "kiro", "position": "center"}])  # fmt: skip
                g = p.geometry("comic_neue")
                assert g["text_size"] >= lettering.READABLE_MIN
                assert "overflow" not in p.flags, (panel, kind, text)  # the fixtures all fit


def test_old_balloons_set_below_the_floor_are_shown_as_not_fitting() -> None:
    from storypanel.db.models import Balloon
    from storypanel.services.projects import balloon_out

    def out(size: float, kind: str = "speech") -> list[str]:
        b = Balloon(id=uuid.uuid4(), panel_id=uuid.uuid4(), order_index=0, type=kind, speaker_id=None, text="x",
                    geometry={"x": 0, "y": 0, "w": 90, "h": 60, "font_size": 22, "text_size": size, "lines": ["x"]},
                    flags=[], user_edited=False, version=1)  # fmt: skip
        return balloon_out(b).flags

    assert out(11.0) == ["overflow"]
    assert out(15.0) == [] and out(22.0) == []
    assert out(11.0, "sfx") == []


def test_web_editor_uses_the_same_floor() -> None:
    src = (Path(__file__).resolve().parents[3] / "apps/web/src/lib/text.ts").read_text()
    assert f"export const READABLE_MIN = {int(lettering.READABLE_MIN)};" in src
    assert "minSize = READABLE_MIN" in src


def _balloons(doc: render.PageDoc) -> list[dict]:
    return [b for p in doc.panels for b in p.balloons if b["type"] != "sfx"]


@pytest.mark.parametrize(
    "name", ["page-long-dialogue", "page-nonascii-edges", "strip-sfx-tails", "strip-wide-last"]
)
def test_pdf_and_png_draw_the_stored_size(name: str) -> None:
    """The PDF sets exactly text_size x 432/page-width points; the PNG draws glyphs of exactly
    text_size x 2000/page-width pixels. The editor draws the same stored size (e2e/lettering.spec.ts
    compares its pixels with the server PNG)."""
    from pypdf import PdfReader
    from pypdf.generic import ContentStream

    doc = representative_pages()[name]
    balloons = _balloons(doc)
    assert balloons and all(float(b["geometry"]["text_size"]) >= lettering.READABLE_MIN for b in balloons)
    k_pt = render.PDF_TRIM_WIDTH_PT / doc.size[0]
    want_pt = sorted({round(float(b["geometry"]["text_size"]) * k_pt, 3) for b in balloons})
    pdf = PdfReader(io.BytesIO(render.build_pdf([doc], [render.render_page(doc, 400, lettering=False)], "t")))
    page = pdf.pages[0]
    ops = ContentStream(page.get_contents(), pdf).operations
    sizes = {round(float(o[1]), 3) for o, op in ops if op == b"Tf"}
    sfx = {round(float(b["geometry"]["font_size"]) * k_pt, 3) for p in doc.panels for b in p.balloons
           if b["type"] == "sfx"}  # fmt: skip
    assert sorted(sizes - sfx) == want_pt, (sizes, want_pt)
    assert min(want_pt) >= round(lettering.printed_pt(lettering.READABLE_MIN, doc.size[0]), 3)


def _cap_height_px(size_px: float) -> int:
    from PIL import ImageFont

    from storypanel.catalog import font_path

    box = ImageFont.truetype(str(font_path("comic_neue")), round(size_px * 4) / 4).getbbox("H", anchor="ls")
    return box[3] - box[1]


@pytest.mark.parametrize("panel", ["page", "strip_narrow"])
def test_png_letter_height_at_the_floor_matches_the_output_size(panel: str) -> None:
    rect = PANELS[panel]
    g = {"x": rect[0] + 20, "y": rect[1] + 20, "w": 260, "h": 90, "font_size": 15.0}
    g, flags = lettering.typeset("caption", "HHHH", g)
    assert g["text_size"] == lettering.READABLE_MIN and flags == []
    b = {"type": "caption", "text": "HHHH", "geometry": g, "order_index": 0}
    art = io.BytesIO()
    Image.new("RGB", (64, 64), (40, 90, 150)).save(art, format="PNG")

    def png(balloon: dict) -> Image.Image:
        d = render.PageDoc(PAGE[panel], (render.PanelDoc(rect, art.getvalue(), (balloon,)),))
        return Image.open(io.BytesIO(render.render_page(d, EXPORT_WIDTH))).convert("L")

    ink = ImageChops.difference(png(b), png({**b, "geometry": {**g, "lines": []}})).point(
        lambda v: 255 if v > 80 else 0
    )
    x0, y0, x1, y1 = ink.getbbox()
    size_px = lettering.READABLE_MIN * EXPORT_WIDTH / PAGE[panel][0]  # 30 px page, 18.75 px strip
    assert abs((y1 - y0) - _cap_height_px(size_px)) <= 1, (y1 - y0, _cap_height_px(size_px))


def test_export_reports_lettering_size_and_warns_below_the_print_floor() -> None:
    def page(size: tuple[int, int], text_size: float) -> dict:
        b = {"type": "speech", "geometry": {"text_size": text_size}}
        return {"size": list(size), "panels": [{"balloons": [b, {"type": "sfx", "geometry": {}}]}]}

    q = merge_print_quality(None, 0, [], "local_resample", letters=lettering_sizes(0, page((1000, 1500), 15)))
    assert q["min_letter_pt"] == 6.48 and q["min_letter_px"] == 30.0 and q["warnings"] == []
    q = merge_print_quality(None, 0, [], "local_resample", letters=lettering_sizes(0, page((1600, 560), 22)))
    assert q["min_letter_pt"] == 5.94 and q["min_letter_px"] == 27.5
    (w,) = q["warnings"]
    assert "5.94 pt" in w and "6.48 pt" in w and re.search(r"at least 6\.5 in wide", w), w
    # pages merge: the report keeps every page's rows
    q2 = merge_print_quality(q, 1, [], "local_resample", letters=lettering_sizes(1, page((1600, 560), 15)))
    assert [r["page"] for r in q2["lettering"]] == [1, 2] and q2["min_letter_pt"] == 4.05
