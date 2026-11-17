"""Prompts v4: reveal timing, lettering against located faces, and honest sheet checks.

Built from two real comics (docs/quality/baseline-2.md, docs/quality/v4.md). The lettering tests use
small crops of the real Train artwork (tests/fixtures/real/), with face positions read off the art
by eye. Everything here is deterministic except the last test, which runs the simulated pipeline.
Simulated judgments prove the plumbing, never a model's accuracy. No paid calls.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw

from storypanel.catalog import PROMPT_VERSION
from storypanel.domain.schemas import (
    BeatSheet,
    CharacterSpec,
    ComicFormat,
    ComicScript,
    ContinuityRule,
    LocationSpec,
    PageSpec,
    PanelSpec,
    ScriptConsistencyReport,
    VisionQAReport,
)  # fmt: skip
from storypanel.domain.validation import story_checks, validate_script
from storypanel.pipeline import art, consistency, lettering, qa, sheets
from storypanel.pipeline.stages_art import lettering_problems, merge_print_quality
from storypanel.services.projects import layout_notes

REAL = Path(__file__).parent / "fixtures" / "real"


def char(cid: str, name: str) -> CharacterSpec:
    return CharacterSpec.model_validate({
        "id": cid, "name": name, "role": "lead", "personality": "warm", "voice": "soft",
        "visual": {"type": "teen, 1.6 m", "body": "slim", "head": "round face", "hair": "dark bob",
                   "outfit": "cream cardigan", "palette": {"cardigan": "#f0e6d0"},
                   "signature_features": ["sketchbook with a red ribbon"]}})  # fmt: skip


MIRA, TEO = char("mira", "Mira"), char("teo", "Teo")
CAFE = LocationSpec(
    id="cafe", name="Station Café", description="A small café with a window table onto the platform"
)
GARDEN = LocationSpec(
    id="garden", name="The Garden", description="A walled garden with a stone path and beds of red tulips"
)
TICKET = ContinuityRule(id="ticket", text="The ticket stub left on the table has a small drawing of Mira on the back",
                        applies_to="location", target="cafe")  # fmt: skip
SKETCHBOOK = ContinuityRule(id="sketchbook", text="Mira carries a worn sketchbook with a red ribbon bookmark",
                            applies_to="character", target="mira")  # fmt: skip
BLOOM = ContinuityRule(
    id="bloom", text="The tulips in the garden are in bloom", applies_to="location", target="garden"
)
REFS = {"mira": {"three_quarter": "m3q", "portrait": "mp"}, "teo": {"three_quarter": "t3q", "portrait": "tp"}}
BEATS = BeatSheet.model_validate({"structure": "kishotenketsu", "beats": [
    {"name": "setup", "summary": "Mira sketches alone at the window table in the café", "pages": [1]},
    {"name": "development", "summary": "Teo rushes in late again and sits with her", "pages": [1]},
    {"name": "turn", "summary": "Teo finally catches his train and leaves", "pages": [1]},
    {"name": "payoff", "summary": "Mira finds the ticket stub he left, with a drawing of her on the back", "pages": [1]},
]})  # fmt: skip


def panel(**over: Any) -> PanelSpec:
    base = {"shot": "medium", "angle": "eye-level", "location_id": "cafe", "time_of_day": "late afternoon",
            "characters": [{"id": "mira", "position": "left", "pose": "sketching", "expression": "absorbed"}],
            "action": "Mira sketches at the window table", "mood": "warm", "purpose": "Mira alone, sketching",
            "focus": "her pencil on the page"}  # fmt: skip
    return PanelSpec.model_validate({**base, **over})


def stub(state: str) -> list[dict[str, str]]:
    return [{"element": "the ticket stub", "state": state}]


def compile_(**kw: Any) -> art.CompiledPanel:
    args: dict[str, Any] = dict(panel=panel(), aspect="9:16", style_preset="clean_manga_warm",
                                cast={"mira": MIRA, "teo": TEO}, locations={"cafe": CAFE, "garden": GARDEN},
                                character_refs=REFS, location_refs={"cafe": "loc", "garden": "gloc"},
                                continuity=("prev", "prev-asset"), max_references=4, rules=[SKETCHBOOK],
                                panel_size=(300.0, 520.0))  # fmt: skip
    return art.compile_panel(**{**args, **kw})


# ------------------------------------------------------------------ reveal timing in the picture
def test_new_comics_use_v4() -> None:
    assert PROMPT_VERSION == "v4" and compile_().prompt_version == "v4"


@pytest.mark.parametrize(("state", "words"), [
    ("absent", "Not in this scene yet: the ticket stub. Don't draw it."),
    ("hidden", "The ticket stub is here but hidden from the reader: keep it fully covered or out of frame."),
    ("unnoticed", "the ticket stub is visible but nobody has noticed it yet: show it small and secondary"),
    ("revealed", "Show clearly: the ticket stub."),
])  # fmt: skip
def test_each_element_state_has_its_own_instruction(state: str, words: str) -> None:
    c = compile_(panel=panel(element_states=stub(state)))
    assert words.lower() in c.prompt.lower()
    assert c.inputs["element_states"] == [{"element": "the ticket stub", "state": state}]


def test_foreshadowing_is_kept_not_banned() -> None:
    """A deliberate hint ("unnoticed") stays visible: no exclusion, and the references are used in full."""
    c = compile_(panel=panel(element_states=stub("unnoticed")))
    assert "Don't draw it" not in c.prompt and "hidden from the reader" not in c.prompt
    assert "architecture, props, colours and light; not the camera angle" in c.prompt
    assert [r.role for r in c.references] == ["character", "location", "continuity"]


def test_an_exception_keeps_its_meaning() -> None:
    """Excepting "tulips in bloom" doesn't mean "no tulips": the rule is stated as not holding, and the
    panel's action decides what is drawn."""
    p = panel(
        location_id="garden", rule_exceptions=["bloom"], action="Walter enters; the beds are still green"
    )
    c = compile_(panel=p, rules=[BLOOM])
    assert (
        'Exception in this panel: "The tulips in the garden are in bloom" does not hold here; follow the action above.'
        in c.prompt
    )
    assert "Must be clearly visible: The tulips" not in c.prompt
    assert "Not in this scene" not in c.prompt and "Don't draw" not in c.prompt  # not turned into "absent"
    assert c.inputs["excepted_rules"] == ["bloom"]
    assert "architecture and light only" in c.prompt  # the location picture shows the tulips in bloom
    assert "Must be clearly visible: The tulips in the garden are in bloom." in compile_(panel=panel(location_id="garden"),
                                                                                       rules=[BLOOM]).prompt  # fmt: skip


def test_a_location_reference_that_shows_a_hidden_element_is_left_out() -> None:
    """The garden's description (and so its reference picture) names the tulips; a panel that keeps
    them hidden gets no location reference rather than one that shows them."""
    p = panel(location_id="garden", element_states=[{"element": "red tulips", "state": "hidden"}])
    c = compile_(panel=p)
    assert "location" not in [r.role for r in c.references]
    assert c.inputs["references_omitted"][0] == ("location: left out, the location reference likely shows red tulips, "
                                                 "which is hidden in this panel")  # fmt: skip
    # an element the description doesn't mention: the reference stays, limited to architecture and light
    c2 = compile_(panel=panel(element_states=stub("hidden")))
    assert "location" in [r.role for r in c2.references] and "architecture and light only" in c2.prompt


def test_a_previous_panel_that_shows_a_hidden_element_is_left_out() -> None:
    before = panel(element_states=stub("unnoticed"))
    now = panel(element_states=stub("hidden"))
    c = compile_(panel=now, previous=before)
    assert "continuity" not in [r.role for r in c.references]
    assert any(
        r.startswith("previous panel: left out, the previous panel shows the ticket stub")
        for r in c.inputs["references_omitted"]
    )
    # without a conflict the previous panel stays (unchanged selection: no evidence it caused repetition)
    assert "continuity" in [r.role for r in compile_(panel=now, previous=panel()).references]


# ------------------------------------------------------------------ reveal timing in the script
def script(*panels: PanelSpec) -> ComicScript:
    return ComicScript(pages=[PageSpec(panels=list(panels))])


def train(first: str = "hidden") -> ComicScript:
    both = [{"id": "mira", "position": "left", "pose": "looking up", "expression": "amused"},
            {"id": "teo", "position": "right", "pose": "panting", "expression": "sheepish"}]  # fmt: skip
    return script(
        panel(beats=["setup"], element_states=stub(first)),
        panel(characters=both, beats=["development"], element_states=stub("hidden"), purpose="Teo arrives late",
              dialogue=[{"type": "speech", "speaker": "teo", "text": "Missed it again!"},
                        {"type": "speech", "speaker": "mira", "text": "It's okay."}]),
        panel(shot="wide", beats=["turn"], element_states=stub("absent"), purpose="He finally leaves",
              action="Teo's train pulls away; Mira watches from the window"),
        panel(shot="close_up", beats=["payoff"], element_states=stub("revealed"), purpose="The reveal",
              action="Mira picks up a ticket stub with a drawing of her on the back"),
    )  # fmt: skip


def test_a_rule_that_contradicts_the_approved_timing_is_a_review_item() -> None:
    """Train rule 4 requires the stub in every café panel; the story keeps it hidden. Reported for the
    user to decide, never rewritten."""
    issues = [
        i for i in story_checks(train(), BEATS, [TICKET, SKETCHBOOK]) if i.code == "rule_state_conflict"
    ]
    # panels 1-3 are in the café and keep the stub hidden or absent; panel 4 reveals it
    assert [i.path for i in issues] == [
        "pages[0].panels[0].rule:ticket",
        "pages[0].panels[1].rule:ticket",
        "pages[0].panels[2].rule:ticket",
    ]
    assert all(i.severity == "review" for i in issues)  # needs a recorded confirmation to approve
    assert (
        TICKET.text == "The ticket stub left on the table has a small drawing of Mira on the back"
    )  # unchanged
    # the user excepts the rule in those panels: resolved
    s = train()
    for p in s.pages[0].panels[:3]:
        p.rule_exceptions.append("ticket")
    assert not [i for i in story_checks(s, BEATS, [TICKET]) if i.code == "rule_state_conflict"]
    # foreshadowing doesn't conflict with a rule that shows the element
    assert not [i for i in story_checks(train(first="unnoticed"), BEATS, [TICKET]) if i.path.startswith("pages[0].panels[0]")
                and i.code == "rule_state_conflict"]  # fmt: skip


def test_scripts_without_timing_fall_back_to_the_beat_sheet_heuristic() -> None:
    plain = script(*[p.model_copy(update={"element_states": []}) for p in train().pages[0].panels])
    codes = [i.code for i in story_checks(plain, BEATS, [TICKET, SKETCHBOOK])]
    assert (
        codes.count("reveal_rule") == 1 and "rule_state_conflict" not in codes
    )  # an ordinary prop rule isn't flagged


def test_an_element_kept_hidden_but_described_in_the_action_is_reported() -> None:
    bad = script(panel(beats=["setup"], element_states=stub("hidden"), action="Mira sketches beside the ticket stub"),
                 *train().pages[0].panels[1:])  # fmt: skip
    assert any(i.code == "element_state_conflict" for i in story_checks(bad, BEATS))
    assert not any(i.code == "element_state_conflict" for i in story_checks(train(), BEATS))


def test_reversed_speakers_are_not_a_script_problem() -> None:
    s = train()
    s.pages[0].panels[1] = PanelSpec.model_validate({**s.pages[0].panels[1].model_dump(), "characters": [
        {"id": "teo", "position": "left", "pose": "p", "expression": "e"},
        {"id": "mira", "position": "right", "pose": "p", "expression": "e"}]})  # fmt: skip
    assert not [
        i for i in story_checks(s, BEATS) if "speaker" in i.code
    ]  # either arrangement is the user's choice


def test_too_many_elements_are_refused() -> None:
    s = script(panel(element_states=[{"element": f"thing {n}", "state": "hidden"} for n in range(5)]))
    issues = validate_script(s, fmt=ComicFormat.WESTERN_PAGE, expected_pages=1, character_ids={"mira", "teo"},
                             location_ids={"cafe"})  # fmt: skip
    assert any(i.code == "text_too_long" and i.path.endswith(".element_states") for i in issues)


def test_the_story_bible_check_reports_an_early_reveal_as_advice() -> None:
    report = ScriptConsistencyReport.model_validate({"panels": [
        {"page": 1, "panel": 1, "rules": [], "beats": [], "reveals_early": True, "note": "The stub is already on the table."}],
        "conflicts": []})  # fmt: skip
    issues = [i for i in consistency.issues_from_report(report, train(), []) if i.code == "reveal_too_early"]
    assert (
        len(issues) == 1
        and issues[0].severity == "warning"
        and "The stub is already on the table." in issues[0].message
    )


# ------------------------------------------------------------------ reveal timing in the panel check
def vqa(**over: Any) -> VisionQAReport:
    base = {"character_count": 1, "characters": [{"id": "mira", "present": True, "missing_features": [], "identity_rating": 5,
                                                  "head_box": {"x": 0.3, "y": 0.1, "w": 0.3, "h": 0.3}}],
            "action_matches": True, "visible_text": False, "anatomy_defects": [], "fix_notes": [], "required_elements": []}  # fmt: skip
    return VisionQAReport.model_validate({**base, **over})


async def run_qa(monkeypatch: pytest.MonkeyPatch, report: VisionQAReport, state: str) -> qa.QAOutcome:
    from storypanel.domain.schemas import AgeRating, SafetyVerdict
    from storypanel.providers import metering
    from storypanel.providers.base import CallContext

    sent: list[str] = []

    async def structured(*, schema: Any, user: str = "", **_: Any) -> Any:
        sent.append(user)
        return (
            SafetyVerdict(allowed=True, categories=[], severity="none") if schema is SafetyVerdict else report
        )

    monkeypatch.setattr(metering, "structured", structured)
    monkeypatch.setattr(qa.get_settings(), "identity_qa", "none", raising=False)
    out = await qa.evaluate_panel(image=b"", panel=panel(), cast={"mira": MIRA}, audience=AgeRating.ALL_AGES,
                                  reference_images={}, reference_embeddings={}, ctx=CallContext("panel_qa", "u", "p"),
                                  elements=[("the ticket stub", state)])  # fmt: skip
    assert f"- 1: the ticket stub (expected: {qa.ELEMENT_EXPECT[state]})" in sent[-1]
    return out


@pytest.mark.parametrize(("state", "seen", "status", "problem", "note"), [
    ("hidden", "yes", "fail", "element_too_early:1", "Keep the ticket stub out of sight in this panel"),
    ("absent", "yes", "fail", "element_too_early:1", "Remove the ticket stub from this panel"),
    ("hidden", "no", "pass", None, None),
    ("unnoticed", "yes", "pass", None, None),  # foreshadowing drawn as intended
    ("unnoticed", "no", "fail", "element_missing:1", "Show the ticket stub small"),
    ("revealed", "no", "fail", "element_missing:1", "Show the ticket stub clearly"),
    ("hidden", "uncertain", "review", "element_uncertain:1", None),  # unsure: a person looks, no paid repair
])  # fmt: skip
async def test_the_panel_check_compares_what_is_drawn_with_the_timing(
    monkeypatch: pytest.MonkeyPatch, state: str, seen: str, status: str, problem: str | None, note: str | None
) -> None:
    out = await run_qa(monkeypatch, vqa(element_checks=[{"number": 1, "visible": seen}]), state)
    assert out.status == status
    if problem:
        assert problem in out.scores["problems"]
    if note:
        assert any(n.startswith(note) for n in out.fix_notes)
    if status == "review":
        assert out.fix_notes == [] and out.scores["aspects"]["story"] == "uncertain"


async def test_faces_the_check_did_not_locate_are_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    out = await run_qa(monkeypatch, vqa(characters=[{"id": "mira", "present": True, "missing_features": [], "identity_rating": 5}],
                                        element_checks=[{"number": 1, "visible": "no"}]), "hidden")  # fmt: skip
    assert out.scores["faces_unlocated"] == ["mira"]


# ------------------------------------------------------------------ lettering on real artwork
TRAIN = {  # Train panels (w6_bottom_large); faces read off the real art by eye, normalised to the panel
    "p2": dict(
        rect=(512.0, 40.0, 448.0, 404.0),
        chars=[("mira", "left"), ("teo", "right")],
        dialogue=[("speech", "teo", "Missed it again! Ha... sorry!"), ("speech", "mira", "It's... okay.")],
        heads={"mira": (0.10, 0.18, 0.28, 0.37), "teo": (0.50, 0.02, 0.22, 0.30)},
    ),
    "p4": dict(
        rect=(355.0, 468.0, 291.0, 404.0),
        chars=[("mira", "left"), ("teo", "right")],
        dialogue=[("speech", "teo", "Delayed! I can make it!")],
        heads={"mira": (0.05, 0.40, 0.32, 0.18), "teo": (0.58, 0.13, 0.30, 0.20)},
    ),
    "p6": dict(
        rect=(40.0, 896.0, 920.0, 565.0),
        chars=[("mira", "left"), ("teo", "right")],
        dialogue=[("speech", "mira", "Tomorrow... same table?")],
        heads={"mira": (0.24, 0.27, 0.06, 0.13), "teo": (0.88, 0.05, 0.06, 0.13)},
    ),
}


def letter(name: str, located: bool) -> tuple[list[lettering.Placed], tuple[float, ...], dict[str, Any]]:
    c = TRAIN[name]
    rect = c["rect"]
    image = (REAL / f"train-{name}.jpg").read_bytes()
    placed = lettering.layout_panel(
        panel_rect=rect, page_size=(1000, 1500), characters=[{"id": i, "position": p} for i, p in c["chars"]],
        dialogue=[{"type": t, "speaker": s, "text": x} for t, s, x in c["dialogue"]],
        head_boxes={k: dict(zip("xywh", v, strict=True)) for k, v in c["heads"].items()} if located else None,
        detail=lettering.art_detail(image, rect[2], rect[3]))  # fmt: skip
    return placed, rect, c


def face_share(b: lettering.Placed, rect: tuple[float, ...], head: tuple[float, ...]) -> float:
    h = (rect[0] + head[0] * rect[2], rect[1] + head[1] * rect[3], head[2] * rect[2], head[3] * rect[3])
    return lettering.overlap((b.x, b.y, b.w, b.h), h) / (h[2] * h[3])


@pytest.mark.parametrize("name", ["p2", "p4", "p6"])
def test_with_located_faces_no_balloon_covers_a_face_on_the_real_pages(name: str) -> None:
    placed, rect, c = letter(name, located=True)
    for b in placed:
        assert all(face_share(b, rect, h) < 0.15 for h in c["heads"].values()), (b.text, b.flags)
        assert not {
            "covers_face",
            "face_position_estimated",
            "tail_target_estimated",
            "tail_ambiguous",
        } & set(b.flags)
        assert b.font_size >= lettering.READABLE_MIN
        # inside the panel: no automatic gutter balloons
        assert (
            rect[0] <= b.x
            and b.x + b.w <= rect[0] + rect[2]
            and rect[1] <= b.y
            and b.y + b.h <= rect[1] + rect[3]
        )


def test_guessed_faces_are_reported_as_uncertain_not_passed() -> None:
    """Panel 4 as exported: faces weren't located, the balloon sat on Teo's real face, and nothing
    warned. Placement can't be fixed without face positions; it is now reported as possible."""
    placed, rect, c = letter("p4", located=False)
    b = placed[0]
    assert face_share(b, rect, c["heads"]["teo"]) > 0.5  # still on his face: the guess is wrong
    assert "face_position_estimated" in b.flags and "tail_target_estimated" in b.flags
    assert "covers_face" not in b.flags  # not claimed as confirmed


def test_reversed_speakers_read_in_order_in_either_arrangement() -> None:
    for chars in ([("mira", "left"), ("teo", "right")], [("teo", "left"), ("mira", "right")]):
        rect = (40.0, 40.0, 500.0, 420.0)
        first, second = lettering.layout_panel(
            panel_rect=rect, page_size=(1000, 1500), characters=[{"id": i, "position": p} for i, p in chars],
            head_boxes={"mira": {"x": 0.1 if chars[0][0] == "mira" else 0.7, "y": 0.4, "w": 0.2, "h": 0.2},
                        "teo": {"x": 0.7 if chars[0][0] == "mira" else 0.1, "y": 0.4, "w": 0.2, "h": 0.2}},
            dialogue=[{"type": "speech", "speaker": "teo", "text": "Missed it again! Ha... sorry!"},
                      {"type": "speech", "speaker": "mira", "text": "It's... okay."}])  # fmt: skip
        if second.x + second.w / 2 < first.x + first.w / 2:  # the answer sits left of the first line
            assert second.y >= first.y + first.h * 0.6 or "reading_order" in second.flags
        assert lettering.reading_key((second.x, second.y, second.w, second.h), rect) > lettering.reading_key(
            (first.x, first.y, first.w, first.h), rect
        )


def test_a_tail_that_ends_nearer_another_character_is_flagged() -> None:
    rect = (40.0, 40.0, 900.0, 400.0)
    heads = {
        "mira": {"x": 0.05, "y": 0.6, "w": 0.1, "h": 0.15},
        "teo": {"x": 0.3, "y": 0.2, "w": 0.1, "h": 0.15},
    }
    b = lettering.layout_panel(panel_rect=rect, page_size=(1000, 1500), head_boxes=heads,
                               characters=[{"id": "mira", "position": "left"}, {"id": "teo", "position": "center"}],
                               dialogue=[{"type": "speech", "speaker": "mira",
                                          "text": "Tomorrow, same table, same time, same everything?"}])[0]  # fmt: skip
    assert b.tail is not None
    d_mira = lettering._dist_to(b.tail, lettering._abs(heads["mira"], rect))  # type: ignore[arg-type]
    d_teo = lettering._dist_to(b.tail, lettering._abs(heads["teo"], rect))  # type: ignore[arg-type]
    assert ("tail_ambiguous" in b.flags) == (d_teo < d_mira)


def test_a_sound_effect_keeps_off_characters_or_is_flagged() -> None:
    rect = (40.0, 40.0, 448.0, 404.0)
    bodies = {"mira": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}}  # the character fills the panel
    sfx = lettering.layout_panel(panel_rect=rect, page_size=(1000, 1500), characters=[{"id": "mira", "position": "center"}],
                                 head_boxes={"mira": {"x": 0.3, "y": 0.05, "w": 0.4, "h": 0.35}}, body_boxes=bodies,
                                 dialogue=[{"type": "sfx", "text": "CHUFF... CHUFF..."}])[0]  # fmt: skip
    assert "sfx_covers_character" in sfx.flags
    roomy = lettering.layout_panel(panel_rect=rect, page_size=(1000, 1500), characters=[{"id": "mira", "position": "left"}],
                                   head_boxes={"mira": {"x": 0.05, "y": 0.3, "w": 0.15, "h": 0.15}},
                                   body_boxes={"mira": {"x": 0.02, "y": 0.3, "w": 0.25, "h": 0.65}},
                                   dialogue=[{"type": "sfx", "text": "CHUFF... CHUFF..."}])[0]  # fmt: skip
    assert "sfx_covers_character" not in roomy.flags


def test_exports_separate_confirmed_and_uncertain_lettering_problems() -> None:
    page = {"size": [1000, 1500], "panels": [
        {"balloons": [{"type": "speech", "flags": ["covers_face"]}]},
        {"balloons": [{"type": "speech", "flags": ["face_position_estimated", "tail_target_estimated"]}]},
        {"balloons": [{"type": "speech", "flags": ["overflow", "reading_order", "tail_ambiguous"]}]},
        {"balloons": [{"type": "speech", "flags": []}]}]}  # fmt: skip
    warnings = merge_print_quality(None, 0, [], "local_resample", flagged=lettering_problems(0, page))[
        "warnings"
    ]
    assert warnings[0] == ("Page 1, panel 1: a balloon covers a face. In the editor: move the balloon, shorten the text, "
                           "or redraw the panel to leave room.")  # fmt: skip
    assert (
        warnings[1].startswith("Page 1, panel 2: possibly: a balloon may cover a face")
        and "In the editor" not in warnings[1]
    )
    assert (
        "text doesn't fit at a readable size; balloons may read out of order; a tail points nearer another character"
        in warnings[2]
    )
    assert len(warnings) == 3


# ------------------------------------------------------------------ reference sheets
def png(im: Image.Image) -> bytes:
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def turnaround(cut_last: bool = False, straddle: bool = False) -> bytes:
    im = Image.new("RGB", (1024, 400), "white")
    d = ImageDraw.Draw(im)
    for i, cx in enumerate((128, 384, 552 if straddle else 640, 896)):
        top, h = (120, 400) if cut_last and i == 3 else (60, 300)
        d.ellipse((cx - 22, top, cx + 22, top + 44), fill=(230, 190, 160))
        d.rectangle((cx - 30, top + 44, cx + 30, top + h), fill=(60, 60, 140))
    return png(im)


def expressions(gutters: bool = True) -> bytes:
    im = Image.new("RGB", (800, 800), "white" if gutters else (150, 110, 80))
    d = ImageDraw.Draw(im)
    if gutters:
        for r in range(2):
            for c in range(2):
                d.rectangle((c * 400 + 3, r * 400 + 3, c * 400 + 397, r * 400 + 397), fill=(150, 110, 80))
                d.ellipse((c * 400 + 80, r * 400 + 60, c * 400 + 320, r * 400 + 380), fill=(230, 190, 160))
    else:  # one face across the whole sheet: not four close-ups
        d.ellipse((150, 150, 650, 650), fill=(230, 190, 160))
    return png(im)


def test_a_valid_turnaround_passes_and_a_cropped_one_is_caught() -> None:
    good = sheets.analyse(turnaround(), 4, 1)
    assert good.method == "grid" and good.problems == []
    cut = sheets.analyse(turnaround(cut_last=True), 4, 1)
    assert cut.method == "grid" and cut.problems == ["figure_touches_edge:3"]  # used to pass silently
    straddling = sheets.analyse(turnaround(straddle=True), 4, 1)
    assert "figure_on_vertical_line:2" in straddling.problems


def test_edge_to_edge_close_ups_with_gutters_are_a_valid_grid() -> None:
    ok = sheets.analyse(expressions(), 2, 2)
    assert ok.method == "grid" and ok.problems == ["separated_by_gutters"] and len(ok.crops) == 4
    bad = sheets.analyse(expressions(gutters=False), 2, 2)
    assert bad.method == "unverified"  # not a grid: still reported, not passed


def test_sheet_layout_notes_say_which_check_and_how_sure() -> None:
    assert layout_notes({"whole_figures": "no"}) == ["the review saw a turnaround view cut off or missing"]
    assert layout_notes({"whole_figures": "uncertain"}) == [
        "the review couldn't tell whether all four turnaround views are complete"
    ]
    assert layout_notes({"whole_figures": "yes", "layout_cut_off": True}) == [
        "a turnaround figure touches the image edge and is probably cut off (measured)"
    ]
    assert layout_notes({"whole_figures": "yes", "layout_unverified": True,
                         "layout": {"turnaround": {"method": "grid"}, "expressions": {"method": "unverified"}}}) == [
        "the expression close-ups couldn't be separated automatically, so they weren't checked; look at them yourself"]  # fmt: skip
    assert layout_notes({"whole_figures": "yes"}) == []


async def test_the_sheet_check_sees_the_face_close_ups(monkeypatch: pytest.MonkeyPatch) -> None:
    from storypanel.domain.schemas import SheetCheckReport
    from storypanel.providers import metering
    from storypanel.providers.base import CallContext

    seen: list[Any] = []

    async def structured(*, images: Any = (), **_: Any) -> SheetCheckReport:
        seen.append(images)
        return SheetCheckReport.model_validate({"items": [], "whole_figures": "yes", "notes": []})

    monkeypatch.setattr(metering, "structured", structured)
    await consistency.check_sheet(
        MIRA, [], b"p", b"t", CallContext("sheet_check", "u", "p"), expressions=b"e"
    )
    assert [i.label for i in seen[-1]] == [
        "Hero portrait:",
        "Turnaround sheet:",
        "Expression sheet (close-ups of the face):",
    ]
    await consistency.check_sheet(MIRA, [], b"p", b"t", CallContext("sheet_check", "u", "p"))
    assert len(seen[-1]) == 2


# ------------------------------------------------------------------ end to end (simulated)
@pytest.mark.workflow
async def test_a_hidden_element_drawn_too_early_is_repaired_once(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    from sqlalchemy import select

    from storypanel.db.models import PanelVersion
    from storypanel.db.session import transaction
    from storypanel.providers import fake
    from tests.conftest import run_to_completion

    fake.control.script_elements = ["the gift box"]
    fake.control.script_foreshadow = True  # a deliberate hint in the first panel
    fake.control.shown_early_attempts = 1  # the first picture of each panel that hides it shows it anyway
    p = await run_to_completion(
        client, headers, "A robot and a girl share noodles in the rain, as a 4 panel strip"
    )
    panels = [pn for pg in p["pages"] for pn in pg["panels"]]
    assert all(x["status"] == "ok" for x in panels)
    assert [x["spec"]["element_states"][0]["state"] for x in panels] == [
        "unnoticed",
        "hidden",
        "hidden",
        "revealed",
    ]
    async with transaction() as db:
        rows = (await db.execute(select(PanelVersion.attempt, PanelVersion.qa_scores)
                                 .order_by(PanelVersion.created_at))).all()  # fmt: skip
    early = [s for a, s in rows if a == 0 and "element_too_early:1" in (s or {}).get("problems", [])]
    assert len(early) == 2  # only the two panels that hide it; the hint and the reveal pass
    assert all(any(n.startswith("Keep the gift box out of sight") for n in s["fix_notes"]) for s in early)
    assert sum(1 for a, _ in rows if a == 1) == 2  # one repair each, then they passed
