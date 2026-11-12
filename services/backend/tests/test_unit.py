"""Pure-logic tests: no database, network or workflow engine."""

from __future__ import annotations

import io
import json
import os
from typing import Any

import pytest
from PIL import Image

from storypanel import storage
from storypanel.catalog import layouts, render_prompt, styles
from storypanel.config import Settings
from storypanel.domain.schemas import (
    CharacterSpec,
    ComicFormat,
    ComicScript,
    LocationSpec,
    PageSpec,
    PanelSpec,
)  # fmt: skip
from storypanel.domain.states import ProjectStatus as S
from storypanel.domain.states import can_transition
from storypanel.domain.validation import has_errors, validate_script
from storypanel.errors import NON_RETRYABLE_CODES, AppError, ErrorCode
from storypanel.obs.logging import REDACTED, pseudonymize, redact_mapping
from storypanel.pipeline import art, lettering, moderation, render
from storypanel.providers.fal_image import assert_public_https_url
from storypanel.workflows import definitions

KIRO = CharacterSpec.model_validate({
    "id": "kiro", "name": "Kiro", "role": "protagonist", "personality": "shy", "voice": "literal",
    "visual": {"type": "small service robot", "body": "round body", "head": "dome head",
               "hair": "none", "outfit": "red apron", "palette": {"body": "#E9E4D8"},
               "signature_features": ["single visor eye", "red apron", "antenna with a bell"]}})  # fmt: skip
MEI = KIRO.model_copy(update={"id": "mei", "name": "Mei"})
LOC = LocationSpec(id="stall", name="Stall", description="A noodle stall at night")


def panel(**over: object) -> PanelSpec:
    base = {"shot": "medium", "angle": "eye-level", "location_id": "stall", "time_of_day": "night",
            "characters": [{"id": "kiro", "position": "left", "pose": "ladling", "expression": "nervous"},
                           {"id": "mei", "position": "right", "pose": "sitting", "expression": "curious"}],
            "action": "Kiro offers Mei a bowl", "mood": "warm", "emphasis": "normal",
            "dialogue": [{"type": "speech", "speaker": "mei", "text": "You made this just for me?"},
                         {"type": "speech", "speaker": "kiro", "text": "I calculated you were hungry."}]}  # fmt: skip
    return PanelSpec.model_validate({**base, **over})


def script(pages: int = 2, panels: int = 4, **over: object) -> ComicScript:
    return ComicScript(pages=[PageSpec(panels=[panel(**over) for _ in range(panels)]) for _ in range(pages)])


def check(s: ComicScript, fmt: ComicFormat = ComicFormat.WESTERN_PAGE, pages: int = 2) -> set[str]:
    return {i.code for i in validate_script(s, fmt=fmt, expected_pages=pages,
                                            character_ids={"kiro", "mei"}, location_ids={"stall"})
            if i.severity == "error"}  # fmt: skip


# ------------------------------------------------------------------ script rules
def test_valid_script_has_no_errors() -> None:
    assert check(script()) == set()


@pytest.mark.parametrize(("over", "code"), [
    ({"location_id": "moon"}, "unknown_location"),
    ({"dialogue": [{"type": "speech", "speaker": "mei", "text": " ".join(["w"] * 26)}]}, "balloon_too_long"),
    ({"dialogue": [{"type": "caption", "text": "a"}] * 4}, "too_many_balloons"),
    ({"dialogue": [{"type": "speech", "speaker": "bob", "text": "hi"}]}, "unknown_speaker"),
    ({"dialogue": [{"type": "speech", "text": "hi"}]}, "missing_speaker"),
    ({"characters": [{"id": "zed", "position": "left", "pose": "p", "expression": "e"}],
      "dialogue": []}, "unknown_character"),
    ({"characters": [{"id": c, "position": "left", "pose": "p", "expression": "e"}
                     for c in ("kiro", "mei", "kiro", "mei")], "dialogue": []}, "too_many_characters"),
])  # fmt: skip
def test_script_rule_violations(over: dict[str, object], code: str) -> None:
    assert code in check(script(**over))


def test_panel_counts_and_page_limits() -> None:
    assert "panel_count" in check(script(panels=7))
    assert "panel_count" in check(script(panels=3))
    assert check(script(pages=1, panels=3), ComicFormat.STRIP, 1) == set()
    assert "strip_single_page" in check(script(pages=2), ComicFormat.STRIP, 1)
    assert "too_many_pages" in check(script(pages=9), pages=9)
    assert "page_count" in check(script(pages=3), pages=2)
    exactly_25 = script(dialogue=[{"type": "caption", "text": " ".join(["w"] * 25)}])
    assert check(exactly_25) == set() and not has_errors([])


# ------------------------------------------------------------------ layout & prompt compiler
def test_layout_selection_is_deterministic_and_respects_emphasis() -> None:
    page = PageSpec(panels=[panel(), panel(), panel(emphasis="large"), panel(), panel()])
    chosen = art.choose_template(ComicFormat.WESTERN_PAGE, page)
    assert chosen.id == "w5_mid_large" == art.choose_template(ComicFormat.WESTERN_PAGE, page).id
    plain = art.choose_template(ComicFormat.WESTERN_PAGE, PageSpec(panels=[panel()] * 4))
    assert plain.large_index is None and plain.panel_count == 4
    strip = art.choose_template(ComicFormat.STRIP, PageSpec(panels=[panel()] * 3))
    assert strip.format is ComicFormat.STRIP
    for t in layouts().values():  # panels stay inside the page and never overlap
        for i, a in enumerate(t.rects):
            assert a[0] >= 0 and a[1] >= 0 and a[0] + a[2] <= t.page[0] and a[1] + a[3] <= t.page[1]
            assert all(lettering.overlap(tuple(map(float, a)), tuple(map(float, b))) == 0
                       for b in t.rects[i + 1:])  # fmt: skip
        assert 0.015 <= t.gutter / t.page[0] <= 0.03


def compiled(**kw: object) -> art.CompiledPanel:
    args: dict[str, object] = dict(
        panel=panel(), aspect="4:3", style_preset="clean_manga_warm", cast={"kiro": KIRO, "mei": MEI},
        locations={"stall": LOC}, location_refs={"stall": "loc-asset"}, continuity=("prev", "prev-asset"),
        character_refs={"kiro": {"three_quarter": "k3q", "side": "kside", "portrait": "kp"},
                        "mei": {"three_quarter": "m3q", "portrait": "mp"}}, max_references=4)  # fmt: skip
    return art.compile_panel(**{**args, **kw})  # type: ignore[arg-type]


def test_prompt_compiler_order_references_and_no_dialogue() -> None:
    c = compiled()
    token = styles().get("clean_manga_warm").token
    assert c.prompt.startswith(token + ". Medium shot")  # style token verbatim and first
    assert "You made this" not in c.prompt and "calculated" not in c.prompt  # text stays out of art
    assert c.prompt.rstrip().endswith("No text, letters, speech bubbles, captions or watermarks.")
    assert [(r.role, r.key) for r in c.references] == [
        ("character", "kiro"),
        ("character", "mei"),
        ("location", "stall"),
        ("continuity", "prev"),
    ]
    assert "from image 1" in c.prompt and "from image 2" in c.prompt and "(image 3)" in c.prompt
    assert KIRO.canonical_description() in c.prompt  # identical wording every time
    assert c.prompt == compiled().prompt and c.prompt_sha256 == compiled().prompt_sha256
    # characters keep their reference slots when the model's limit is tight
    tight = compiled(max_references=2)
    assert [r.role for r in tight.references] == ["character", "character"]
    # view matching: a profile shot selects the side view when one exists
    side = compiled(panel=panel(angle="side profile"))
    assert side.references[0].view == "side" and side.references[1].view == "three_quarter"
    assert "Correction: fix the bell" in compiled(fix_notes=("fix the bell",)).prompt


def test_prompt_templates_are_versioned_files() -> None:
    assert "Return" not in render_prompt("vision_qa")[:10]
    assert "25" in render_prompt("script", pages=2, panel_rule="x", max_words=25, max_balloons=3,
                                 max_per_panel=3, character_ids="kiro", location_ids="stall",
                                 audience="all_ages")  # fmt: skip


# ------------------------------------------------------------------ lettering
RECT = (40.0, 40.0, 448.0, 416.0)
CHARS = [{"id": "kiro", "position": "left"}, {"id": "mei", "position": "right"}]


def place(dialogue: list[dict[str, object]], rect: tuple[float, ...] = RECT) -> list[lettering.Placed]:
    return lettering.layout_panel(panel_rect=rect, page_size=(1000, 1500), dialogue=dialogue,  # type: ignore[arg-type]
                                  characters=CHARS)  # fmt: skip


def test_lettering_reading_order_faces_and_determinism() -> None:
    dialogue = [{"type": "speech", "speaker": "kiro", "text": "I calculated you were hungry."},
                {"type": "speech", "speaker": "mei", "text": "You made this just for me?"},
                {"type": "speech", "speaker": "kiro", "text": "Affirmative."}]  # fmt: skip
    a, b = place(dialogue), place(dialogue)
    assert [p.geometry() for p in a] == [p.geometry() for p in b]
    keys = [lettering.reading_key(p.rect, RECT) for p in a]
    assert keys == sorted(keys) and all(not p.flags for p in a)
    for p in a:  # no balloon overlaps another or covers a default head region
        assert all(lettering.overlap(p.rect, q.rect) == 0 for q in a if q is not p)
        assert p.tail is not None and RECT[0] <= p.x and p.x + p.w <= RECT[0] + RECT[2]
        assert p.font_size >= lettering.FONT_MIN
        _, _, fits = lettering.fit_text(p.text, "comic_neue", *lettering.inner_box("speech", p.w, p.h),
                                        p.font_size)  # fmt: skip
        assert fits


def test_lettering_overflow_escalates_shrink_then_border_then_flag() -> None:
    long = " ".join(["extraordinarily"] * 25)
    tiny = (40.0, 40.0, 150.0, 120.0)
    [p] = place([{"type": "speech", "speaker": "kiro", "text": long}], tiny)
    assert p.font_size == lettering.FONT_MIN and "breaks_border" in p.flags and "overflow" in p.flags
    [ok] = place([{"type": "speech", "speaker": "kiro", "text": "Hi."}], tiny)
    assert ok.flags == [] and ok.font_size == lettering.FONT_MAX
    assert lettering.check_geometry("speech", long, {"w": 60, "h": 40, "font_size": 22}) == ["overflow"]
    assert lettering.wrap("supercalifragilistic", "comic_neue", 20, 60)[0].endswith("-")


def test_caption_sfx_and_render_are_deterministic() -> None:
    placed = place([{"type": "caption", "text": "Later."}, {"type": "sfx", "text": "Clang!"},
                    {"type": "thought", "speaker": "mei", "text": "Hmm."}])  # fmt: skip
    assert placed[0].x == RECT[0] + min(RECT[2], RECT[3]) * 0.03 and placed[1].rotation == -8.0
    doc = render.PageDoc((1000, 1500), (render.PanelDoc(RECT, None, tuple(
        {"type": p.type, "text": p.text, "geometry": p.geometry(), "order_index": p.index}
        for p in placed)),), watermark=True)  # fmt: skip
    one, two = render.render_page(doc, 500), render.render_page(doc, 500)
    assert one == two
    with Image.open(io.BytesIO(one)) as img:
        assert img.size == (500, 750)


def test_svg_layer_escapes_user_text() -> None:
    evil = '<script>alert(1)</script>"&'
    doc = render.PageDoc((1000, 1500), (render.PanelDoc(RECT, None, (
        {"type": "speech", "text": evil, "geometry": {"x": 1, "y": 1, "w": 100, "h": 60}},)),))  # fmt: skip
    svg = render.balloon_layer_svg(doc)
    assert "<script>" not in svg and "&lt;script&gt;" in svg
    assert b"<Title>&lt;b&gt;" in __import__("zipfile").ZipFile(
        io.BytesIO(render.build_cbz([b"x"], "<b>"))).read("ComicInfo.xml")  # fmt: skip


# ------------------------------------------------------------------ safety & security helpers
def test_moderation_rules() -> None:
    assert moderation.rule_check("A robot makes noodles") is None
    assert moderation.rule_check("Batman fights crime").categories == ["ip"]  # type: ignore[union-attr]
    assert moderation.rule_check("an erotic story").categories == ["sexual"]  # type: ignore[union-attr]
    v = moderation.rule_check("a sexy schoolgirl")
    assert v is not None and "minors" in v.categories and not v.allowed
    assert moderation.rule_check("a photo of my boss as a villain").categories == ["real_person"]  # type: ignore[union-attr]
    assert moderation.rule_check("the batmanlike hero") is None  # whole-word matching only
    text, changed = moderation.neutralise_style_references("A duel in the style of Hayao Miyazaki at dawn")
    assert changed and "Miyazaki" not in text and "duel" in text


def test_image_validation_rejects_bad_input() -> None:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), "red").save(buf, format="PNG", pnginfo=None)
    ok = storage.validate_image(buf.getvalue())
    assert (ok.width, ok.height, ok.content_type) == (64, 48, "image/png")
    for bad in (b"not an image", buf.getvalue()[:40], b"<svg xmlns='http://www.w3.org/2000/svg'/>"):
        with pytest.raises(AppError) as e:
            storage.validate_image(bad)
        assert e.value.code is ErrorCode.PROVIDER_BAD_OUTPUT
    gif = io.BytesIO()
    Image.new("RGB", (64, 64)).save(gif, format="GIF")
    with pytest.raises(AppError):
        storage.validate_image(gif.getvalue())
    tiny = io.BytesIO()
    Image.new("RGB", (4, 4)).save(tiny, format="PNG")
    with pytest.raises(AppError):
        storage.validate_image(tiny.getvalue())


@pytest.mark.parametrize("url", [
    "http://example.com/a.png", "https://127.0.0.1/a.png", "https://localhost/a.png",
    "https://169.254.169.254/latest/meta-data", "https://10.0.0.5/x", "file:///etc/passwd",
    "https://[::1]/x",
])  # fmt: skip
def test_ssrf_guard_blocks_non_public_targets(url: str) -> None:
    with pytest.raises(AppError):
        assert_public_https_url(url)


def test_log_redaction() -> None:
    out = redact_mapping({
        "authorization": "Bearer abc.def.ghi", "api_key": "x", "note": "key sk_live_abcdefgh12345678 ok",
        "url": "https://s3/x?X-Amz-Signature=deadbeef&X-Amz-Credential=AKIA", "email": "a@b.c",
        "nested": {"password": "p", "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijk"},
        "prompt_text": "secret story", "ok": "plain", "stripe-signature": "t=1,v1=abc"})  # fmt: skip
    blob = json.dumps(out)
    for leak in ("abc.def.ghi", "sk_live_abcdefgh", "deadbeef", "AKIA", "a@b.c", "secret story",
                 "eyJhbGci", "v1=abc"):  # fmt: skip
        assert leak not in blob
    assert out["ok"] == "plain" and out["api_key"] == REDACTED
    assert pseudonymize("user-1") == pseudonymize("user-1") != pseudonymize("user-2")
    assert "user-1" not in pseudonymize("user-1")


def test_error_contract_and_retryability() -> None:
    e = AppError(ErrorCode.VALIDATION, "Bad", fields=[{"field": "prompt", "message": "short"}],
                 meta={"internal": "detail"})  # fmt: skip
    body = e.to_public("req-1")["error"]
    assert body == {"code": "validation_error", "message": "Bad", "request_id": "req-1",
                    "retryable": False, "fields": [{"field": "prompt", "message": "short"}]}  # fmt: skip
    assert "internal" not in json.dumps(body)
    assert AppError(ErrorCode.PROVIDER_THROTTLED, "x").retryable
    assert not AppError(ErrorCode.MODERATION_BLOCKED, "x").retryable
    # permanent failures are never auto-retried by Temporal
    for code in ("moderation_blocked", "insufficient_credits", "spend_limit_reached", "validation_error"):
        assert code in NON_RETRYABLE_CODES and code in definitions.NON_RETRYABLE
    assert "provider_throttled" not in definitions.NON_RETRYABLE


def test_state_machine() -> None:
    assert can_transition(S.PLANNING, S.AWAITING_CAST_APPROVAL)
    assert not can_transition(S.PLANNING, S.ILLUSTRATING)  # gates cannot be skipped
    assert not can_transition(S.AWAITING_CAST_APPROVAL, S.ILLUSTRATING)
    assert not can_transition(S.BLOCKED, S.PLANNING) and not can_transition(S.CANCELLED, S.QUEUED)
    assert can_transition(S.FAILED, S.ILLUSTRATING) and can_transition(S.ILLUSTRATING, S.CANCELLED)
    assert not can_transition(S.COMPLETED, S.FAILED)


def test_production_config_refuses_fakes_and_dev_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in [k for k in os.environ if k.startswith("SP_")]:
        monkeypatch.delenv(key)
    with pytest.raises(ValueError) as e:
        Settings(env="production", _env_file=None)  # type: ignore[call-arg]
    msg = str(e.value)
    assert "fake providers are not permitted" in msg and "dev auth is not permitted" in msg
    with pytest.raises(ValueError) as e:
        Settings(env="local", provider_mode="live", _env_file=None)  # type: ignore[call-arg]
    assert "SP_IMAGE_MODEL_GENERATE must be set" in str(e.value)  # no guessed model ids
    assert Settings(_env_file=None).max_pages == 8  # type: ignore[call-arg]


def test_sampling_ratio_applies_to_browser_traces_and_is_consistent_across_hops() -> None:
    """The browser always sends sampled=1; the configured ratio must still govern, and every
    service must reach the same decision for one trace id."""
    import random

    from opentelemetry.sdk.trace.sampling import Decision
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, set_span_in_context

    from storypanel.obs.tracing import sampler

    s = sampler(0.2)
    rng = random.Random(7)
    sampled = 0
    for _ in range(4000):
        tid = rng.getrandbits(128)
        parent = set_span_in_context(NonRecordingSpan(SpanContext(tid, rng.getrandbits(64), is_remote=True,
                                                                  trace_flags=TraceFlags(TraceFlags.SAMPLED))))  # fmt: skip
        api = s.should_sample(parent, tid, "POST /v1/projects").decision
        worker = s.should_sample(parent, tid, "RunActivity").decision  # the outbox hop: same trace id
        assert api == worker
        sampled += api is Decision.RECORD_AND_SAMPLE
    assert 0.17 < sampled / 4000 < 0.23  # ~20 %, not 100 %
    unsampled = set_span_in_context(
        NonRecordingSpan(SpanContext(1, 1, is_remote=True, trace_flags=TraceFlags(0)))
    )
    assert s.should_sample(unsampled, 1, "x").decision is Decision.DROP


def _sheet(figs: list[tuple[float, float, float, float]], size: tuple[int, int] = (1600, 900)) -> bytes:
    import io

    from PIL import Image, ImageDraw

    img = Image.new("RGB", size, (250, 248, 240))
    d = ImageDraw.Draw(img)
    w, h = size
    for x0, y0, x1, y1 in figs:
        d.rectangle([int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)], fill=(40, 60, 160))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def test_sheet_layout_validation() -> None:
    from storypanel.pipeline import sheets

    grid = [(0.05 + i * 0.25, 0.1, 0.2 + i * 0.25, 0.9) for i in range(4)]
    assert sheets.analyse(_sheet(grid), 4, 1).method == "grid"
    # model drew four figures, but crowded to the left: the fixed grid would cut them
    crowded = [(0.02 + i * 0.17, 0.1, 0.15 + i * 0.17, 0.9) for i in range(4)]
    seg = sheets.analyse(_sheet(crowded), 4, 1)
    assert seg.method == "segmented" and len(seg.crops) == 4 and seg.problems
    # three figures, one of them straddling a crop line: nothing reliable to crop
    bad = sheets.analyse(_sheet([(0.05, 0.1, 0.2, 0.9), (0.4, 0.1, 0.6, 0.9), (0.8, 0.1, 0.95, 0.9)]), 4, 1)
    assert bad.method == "unverified" and not bad.crops
    assert any(p.startswith("figure_on_vertical_line") for p in bad.problems)
    two_by_two = [(0.1, 0.1, 0.4, 0.4), (0.6, 0.1, 0.9, 0.4), (0.1, 0.6, 0.4, 0.9), (0.6, 0.6, 0.9, 0.9)]
    assert sheets.analyse(_sheet(two_by_two, (1000, 1000)), 2, 2).method == "grid"
    empty = sheets.analyse(_sheet(two_by_two[:3], (1000, 1000)), 2, 2)
    assert "empty_cell:1,1" in empty.problems and empty.method == "unverified"


def test_fake_sheets_have_the_layout_the_pipeline_crops() -> None:
    from storypanel.pipeline import sheets
    from storypanel.providers.base import ImageRequest
    from storypanel.providers.fake import render_fake_image

    ch = [{"id": "a", "position": "center", "palette": {"a": "#AA3333", "b": "#3333AA"}}]
    t = render_fake_image(ImageRequest("t", "16:9", inputs={"kind": "turnaround", "characters": ch}))
    e = render_fake_image(ImageRequest("e", "1:1", inputs={"kind": "expressions", "characters": ch}))
    assert sheets.analyse(t, 4, 1).method == "grid"
    assert sheets.analyse(e, 2, 2).method == "grid"
    # a full-width floor band does not count as a figure on a crop line
    floor = [(0.05 + i * 0.25, 0.1, 0.2 + i * 0.25, 0.7) for i in range(4)] + [(0.0, 0.75, 1.0, 1.0)]
    assert sheets.analyse(_sheet(floor), 4, 1).method == "grid"


def _lettered_doc(edge_panel: bool = False) -> Any:
    import io

    from PIL import Image

    from storypanel.pipeline import lettering, render

    def art(color: tuple[int, int, int]) -> bytes:
        out = io.BytesIO()
        Image.new("RGB", (400, 300), color).save(out, format="PNG")
        return out.getvalue()

    balloons = []
    for n, (kind, text, geo) in enumerate([
        ("speech", "Meet me at the lighthouse after dark.", {"x": 80, "y": 60, "w": 260, "h": 120, "tail": {"x": 200, "y": 260}}),
        ("caption", "Later that night.", {"x": 420, "y": 40, "w": 300, "h": 70}),
        ("thought", "Is anyone there?", {"x": 460, "y": 400, "w": 240, "h": 140}),
        ("sfx", "Crash", {"x": 120, "y": 700, "w": 240, "h": 110, "rotation": -8}),
    ]):  # fmt: skip
        g = {**geo, "font_size": 48 if kind == "sfx" else 22}
        if kind != "sfx":
            g = lettering.typeset(kind, text, g, "comic_neue")[0]
        balloons.append({"type": kind, "text": text, "geometry": g, "order_index": n})
    rect = (0.0, 0.0, 400.0, 600.0) if edge_panel else (40.0, 40.0, 360.0, 520.0)
    panels = (render.PanelDoc(rect, art((30, 90, 160)), tuple(balloons[:2])),
              render.PanelDoc((420.0, 360.0, 360.0, 560.0), art((200, 120, 60)), tuple(balloons[2:])))  # fmt: skip
    return render.PageDoc((800, 1200), panels)


def test_pdf_lettering_is_vector_text_matching_the_raster_renderer(tmp_path: Any) -> None:
    import io
    import shutil
    import subprocess

    from PIL import Image, ImageChops
    from pypdf import PdfReader

    from storypanel.pipeline import render

    doc = _lettered_doc()
    art = render.render_page(doc, 800, lettering=False)
    pdf = render.build_pdf([doc], [art], "T")
    page = PdfReader(io.BytesIO(pdf)).pages[0]
    text = page.extract_text()
    for b in [b for p in doc.panels for b in p.balloons if b["type"] != "sfx"]:
        for line in b["geometry"]["lines"]:
            assert line in text  # real, selectable text, not pixels
    assert "CRASH" in text
    fonts = {str(f.get_object()["/BaseFont"]) for f in page["/Resources"]["/Font"].values()}
    # the bundled faces, embedded as subsets ("XXXXXX+" prefix), as in the editor and PNG export
    assert {"ComicNeue-Regular", "Bangers-Regular"} <= {f.split("+", 1)[-1] for f in fonts if "+" in f}, fonts
    assert [float(v) for v in page.trimbox] == [9.0, 9.0, 441.0, 657.0]
    assert [float(v) for v in page.mediabox] == [0.0, 0.0, 450.0, 666.0]
    if not shutil.which("pdftoppm"):
        pytest.skip("pdftoppm not installed")
    # rasterise the trim area at the PNG renderer's scale and compare the lettering ink
    (tmp_path / "p.pdf").write_bytes(pdf)
    dpi = 800 / (432 / 72)
    subprocess.run(
        ["pdftoppm", "-r", f"{dpi:.4f}", "-png", "-singlefile", str(tmp_path / "p.pdf"), str(tmp_path / "r")],
        check=True,
    )
    ras = Image.open(tmp_path / "r.png").convert("L")
    off = round(9 / 72 * dpi)
    ras = ras.crop((off, off, off + 800, off + 1200))
    png = Image.open(io.BytesIO(render.render_page(doc, 800))).convert("L")
    art_l = Image.open(io.BytesIO(art)).convert("L")

    def lettering_ink(img: Image.Image) -> Image.Image:  # lettering = pixels that differ from the art layer
        return ImageChops.difference(img, art_l).point(lambda v: 255 if v > 60 else 0)

    a, b = lettering_ink(ras), lettering_ink(png)
    inter = ImageChops.multiply(a, b).histogram()[255]
    union = ImageChops.lighter(a, b).histogram()[255]
    print(f"lettering IoU pdf-vs-png: {inter / union:.3f}")
    assert inter / union > 0.75, inter / union


def test_pdf_bleed_extends_artwork_not_a_blank_margin() -> None:
    import io

    from PIL import Image
    from pypdf import PdfReader

    from storypanel.pipeline import render

    doc = _lettered_doc(edge_panel=True)  # a panel touching the left/top trim edge
    art = render.render_page(doc, 800, lettering=False)
    ext = render._bleed_extend(art, 17)
    a = Image.open(io.BytesIO(art)).convert("RGB")
    assert ext.size == (a.width + 34, a.height + 34)
    assert ext.crop((17, 17, 17 + a.width, 17 + a.height)).tobytes() == a.tobytes()  # trim area untouched
    # the bleed continues the edge pixels: the panel art (blue) extends past the trim, not white
    assert ext.getpixel((3, 300)) == a.getpixel((0, 300)) != (255, 255, 255)
    assert ext.getpixel((3, 3)) == a.getpixel((0, 0))
    pdf = render.build_pdf([doc], [art], "T")
    page = PdfReader(io.BytesIO(pdf)).pages[0]
    assert [float(v) for v in page.bleedbox] == [0.0, 0.0, 450.0, 666.0]


def test_pdf_balloon_geometry_and_sfx_fill_match_the_png_renderer(tmp_path: Any) -> None:
    """Per balloon: the lettering ink's bounding box in the rasterised PDF matches the PNG
    renderer's within 3 px at 1600 px width (outline placement, tail tips that must not
    overshoot), and SFX letters keep their yellow fill (stroke drawn outside, not over it)."""
    import io
    import shutil
    import subprocess

    from PIL import Image, ImageChops

    from storypanel.pipeline import render

    if not shutil.which("pdftoppm"):
        pytest.skip("pdftoppm not installed")
    doc = _lettered_doc()
    W = 1600
    art = render.render_page(doc, W, lettering=False)
    dpi = W / (432 / 72)
    off = round(9 / 72 * dpi)

    def raster(pdf_doc: Any, name: str) -> Image.Image:
        (tmp_path / f"{name}.pdf").write_bytes(render.build_pdf([pdf_doc], [art], "T"))
        subprocess.run(["pdftoppm", "-r", f"{dpi:.4f}", "-png", "-singlefile", str(tmp_path / f"{name}.pdf"),
                        str(tmp_path / name)], check=True)  # fmt: skip
        return Image.open(tmp_path / f"{name}.png").convert("RGB").crop((off, off, off + W, off + W * 3 // 2))

    pdf = raster(doc, "lettered")
    # each renderer's lettering is isolated against its *own* art layer
    pdf_art = raster(
        render.PageDoc(doc.size, tuple(render.PanelDoc(p.rect, p.image, ()) for p in doc.panels)), "art"
    )
    png = Image.open(io.BytesIO(render.render_page(doc, W))).convert("RGB")
    png_art = Image.open(io.BytesIO(art)).convert("RGB")
    k = W / doc.size[0]

    def ink(img: Image.Image, box: tuple[int, int, int, int]) -> tuple[int, int, int, int] | None:
        base = pdf_art if img is pdf else png_art
        return (
            ImageChops.difference(img.crop(box), base.crop(box))
            .convert("L")
            .point(lambda v: 255 if v > 60 else 0)
            .getbbox()
        )

    for b in (b for p in doc.panels for b in p.balloons):
        g = b["geometry"]
        xs = [g["x"], g["x"] + g["w"]] + ([g["tail"]["x"]] if g.get("tail") else [])
        ys = [g["y"], g["y"] + g["h"]] + ([g["tail"]["y"]] if g.get("tail") else [])
        pad = 40 if b["type"] != "sfx" else 120
        box = (max(0, int(min(xs) * k) - pad), max(0, int(min(ys) * k) - pad),
               min(W, int(max(xs) * k) + pad), min(W * 3 // 2, int(max(ys) * k) + pad))  # fmt: skip
        a, c = ink(png, box), ink(pdf, box)
        assert a and c, b["type"]
        assert max(abs(u - v) for u, v in zip(a, c, strict=True)) <= 3, (b["type"], a, c)
        if b["type"] == "sfx":

            def yellow(img: Image.Image) -> int:
                return sum(
                    1 for px in img.crop(box).getdata() if px[0] > 220 and 190 < px[1] < 245 and px[2] < 110
                )

            ratio = yellow(pdf) / max(1, yellow(png))
            assert 0.85 < ratio < 1.15, ratio
