"""Pure-logic tests: no database, network or workflow engine."""

from __future__ import annotations

import io
import json
import os

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
