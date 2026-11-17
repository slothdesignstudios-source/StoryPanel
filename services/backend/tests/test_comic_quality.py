"""Comic quality (prompts v3, frozen; v4 is in test_comic_quality_v4.py): the per-panel visual plan, reference purposes and limits, the
lettering plan shared by the image prompt and the letterer, deterministic story checks, the
storytelling judgments of the Story Bible check, QA aspects and bounded repair.

Deterministic, no provider calls. These tests prove what is sent and what is decided; they say
nothing about how good real images are (that needs the live comparison in docs/quality/).
"""

from __future__ import annotations

from typing import Any

import pytest

from storypanel.catalog import PROMPT_VERSION, styles
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
from storypanel.errors import AppError
from storypanel.pipeline import art, consistency, lettering, qa


def char(cid: str, name: str, **visual: Any) -> CharacterSpec:
    return CharacterSpec.model_validate({
        "id": cid, "name": name, "role": "lead", "personality": "dry", "voice": "short",
        "visual": {"type": "human, 1.7 m", "body": "lean", "head": "long face", "hair": "grey bun",
                   "outfit": "yellow oilskin coat", "palette": {"coat": "#e8b400"},
                   "signature_features": ["brass lantern", "round spectacles"], **visual}})  # fmt: skip


INGRID = char(
    "ingrid", "Ingrid", silhouette="tall and narrow with a lantern held out", proportions="about 7 heads tall"
)
GULL = char("gull", "Gull", type="seagull", hair="none", outfit="none")
LANTERN = ContinuityRule(
    id="lantern", text="Ingrid carries a brass lantern", applies_to="character", target="ingrid"
)
LOCS = {"cliff": LocationSpec(id="cliff", name="Cliff path", description="A windy cliff path above the sea")}
REFS = {
    "ingrid": {"three_quarter": "i3q", "portrait": "ip", "side": "iside"},
    "gull": {"three_quarter": "g3q"},
}


def panel(**over: Any) -> PanelSpec:
    base = {"shot": "medium", "angle": "eye-level", "location_id": "cliff", "time_of_day": "dusk",
            "characters": [{"id": "ingrid", "position": "left", "pose": "holding the lantern up",
                            "expression": "suspicious"}],
            "action": "Ingrid spots the gull on her sandwich", "mood": "wry",
            "purpose": "Ingrid discovers the thief", "focus": "the gull's beak in the sandwich",
            "dialogue": [{"type": "speech", "speaker": "ingrid", "text": "Not again."}]}  # fmt: skip
    return PanelSpec.model_validate({**base, **over})


def compile_(**kw: Any) -> art.CompiledPanel:
    args: dict[str, Any] = dict(panel=panel(), aspect="4:3", style_preset="clean_manga_warm",
                                cast={"ingrid": INGRID, "gull": GULL}, locations=LOCS, character_refs=REFS,
                                location_refs={"cliff": "loc"}, continuity=("prev", "prev-asset"), max_references=4,
                                rules=[LANTERN], panel_size=(560.0, 420.0), prompt_version="v3")  # fmt: skip
    return art.compile_panel(**{**args, **kw})


# ------------------------------------------------------------------ versions
def test_new_comics_use_v3_and_v2_prompts_are_unchanged() -> None:
    """v2 is frozen: the same inputs give the same bytes as the release before v3 (hash taken from
    the previous commit's code), so prompts of existing comics can still be rebuilt and verified."""
    from storypanel.domain.schemas import ContinuityRule as R

    kiro = CharacterSpec.model_validate({"id": "kiro", "name": "Kiro", "role": "lead", "personality": "shy",
                                         "voice": "short", "visual": {"type": "robot", "body": "round", "head": "dome",
                                         "hair": "none", "outfit": "red apron", "palette": {"body": "#fff"},
                                         "signature_features": ["visor eye", "antenna bell"]}})  # fmt: skip
    p = PanelSpec.model_validate({"shot": "medium", "angle": "eye-level", "location_id": "stall", "time_of_day": "night",
                                  "characters": [{"id": "kiro", "position": "left", "pose": "ladling", "expression": "nervous"}],
                                  "action": "Kiro offers a bowl", "mood": "warm",
                                  "dialogue": [{"type": "speech", "speaker": "kiro", "text": "Hot!"}]})  # fmt: skip
    v2 = art.compile_panel(panel=p, aspect="4:3", style_preset="clean_manga_warm", cast={"kiro": kiro},
                           locations={"stall": LocationSpec(id="stall", name="Stall", description="A noodle stall")},
                           character_refs={"kiro": {"three_quarter": "a"}}, location_refs={"stall": "b"},
                           continuity=("prev", "c"), max_references=4, fix_notes=("draw the bell",),
                           rules=[R(id="apron", text="Kiro wears the red apron", applies_to="character", target="kiro")],
                           prompt_version="v2")  # fmt: skip
    assert v2.prompt_version == "v2"
    assert v2.prompt_sha256 == "7d9a19d6d6502f4b5e8c01ea9e163ffd0515262400224d5ea21fa8855190f931"
    assert PROMPT_VERSION == "v4"
    # v3 is frozen too (comics made with it keep reproducible prompts): hashes recorded at cfa4534
    assert compile_().prompt_version == "v3"
    assert compile_().prompt_sha256 == "85d4fdac2e2835a20658abe00af762382fc7f80ddaec8a1c1f45820db3e40ec8"
    assert compile_(fix_notes=("Draw the lantern.",)).prompt_sha256 == (
        "86b29d3bf42492f60666366d39eaabcd0eedc59957a9467c557b5e569a05b527"
    )
    with pytest.raises(AppError):
        compile_(prompt_version="v1")


# ------------------------------------------------------------------ visual plan
def test_v3_prompt_is_an_ordered_visual_plan() -> None:
    c = compile_()
    text = c.prompt
    order = ["Reference images", "Panel purpose: Ingrid discovers the thief", "Framing: Medium shot, eye-level, a landscape 4:3 frame",
             "On the left: Ingrid", "Action: Ingrid spots the gull", "Must be clearly visible: Ingrid carries a brass lantern.",
             "Setting: A windy cliff path above the sea, dusk", "Focal point: the gull's beak in the sandwich",
             "Lettering space:", "No text, letters"]  # fmt: skip
    positions = [text.index(s) for s in order]
    assert positions == sorted(positions)
    assert text.startswith(styles().get("clean_manga_warm").token + ".")
    assert INGRID.canonical_description() in text and "shape: tall and narrow" in text
    assert "Not again" not in text  # lettering stays out of the art
    assert "Correction" not in text and "Corrections" not in text  # nothing to correct on a first attempt
    assert text == compile_().prompt  # deterministic


def test_each_reference_image_is_labelled_with_its_purpose() -> None:
    c = compile_(panel=panel(characters=[{"id": "ingrid", "position": "left", "pose": "p", "expression": "e"},
                                         {"id": "gull", "position": "right", "pose": "p", "expression": "e"}]))  # fmt: skip
    assert [(r.role, r.key) for r in c.references] == [("character", "ingrid"), ("character", "gull"),
                                                       ("location", "cliff"), ("continuity", "prev")]  # fmt: skip
    assert "Image 1: Ingrid's identity (three-quarter view" in c.prompt
    assert "Image 2: Gull's identity" in c.prompt
    assert (
        "Image 3: the location, Cliff path: architecture, props, colours and light; not the camera angle"
        in c.prompt
    )
    # the previous panel keeps lighting and objects consistent but must not dictate the composition
    assert (
        "Image 4: the previous panel, only to keep lighting" in c.prompt
        and "new camera position and composition" in c.prompt
    )
    assert "(as in image 1)" in c.prompt and "(as in image 2)" in c.prompt
    assert "Keep the characters' relative heights" in c.prompt


def test_reference_limits_are_explicit_never_silent() -> None:
    two = panel(characters=[{"id": "ingrid", "position": "left", "pose": "p", "expression": "e"},
                            {"id": "gull", "position": "right", "pose": "p", "expression": "e"}])  # fmt: skip
    tight = compile_(panel=two, max_references=2)
    assert [r.role for r in tight.references] == ["character", "character"]
    assert tight.inputs["references_omitted"] == ["location: no reference slot left after the characters",
                                                  "previous panel: no reference slot left"]  # fmt: skip
    with pytest.raises(AppError, match="accepts 1 reference"):
        compile_(panel=two, max_references=1)  # a character is never dropped to make room
    with pytest.raises(AppError, match="Gull has no approved reference"):
        compile_(panel=two, character_refs={"ingrid": REFS["ingrid"]})
    assert compile_().inputs["references_omitted"] == []


def test_reference_view_follows_the_shot() -> None:
    assert compile_(panel=panel(shot="close_up")).references[0].view == "portrait"
    assert compile_(panel=panel(angle="side profile")).references[0].view == "side"
    assert compile_(panel=panel(shot="wide")).references[0].view == "three_quarter"


@pytest.mark.parametrize(
    ("aspect", "words"),
    [
        ("21:9", "very wide 21:9 frame: spread the subjects"),
        ("1:1", "square 1:1 frame"),
        ("2:3", "tall 2:3 frame: use the height"),
    ],
)
def test_framing_matches_the_real_panel_shape(aspect: str, words: str) -> None:
    assert words in compile_(aspect=aspect).prompt


def test_shot_labels_and_emphasis_change_the_composition() -> None:
    close = compile_(panel=panel(shot="close_up", focus="")).prompt
    assert "Ingrid's head and shoulders fill most of the frame" in close
    assert "Focal point: Ingrid's face" in close  # a close-up without a stated focus centres the face
    wide = compile_(panel=panel(shot="wide")).prompt
    assert "Full figures, small enough that the surroundings tell the reader where we are" in wide
    assert "most important moment on the page" in compile_(panel=panel(emphasis="large")).prompt
    assert "most important moment" not in compile_().prompt


def test_corrections_keep_what_already_works() -> None:
    text = compile_(fix_notes=("Draw the lantern in Ingrid's right hand.",)).prompt
    assert (
        "Corrections from the review of the previous attempt (keep everything else that already matches"
        in text
    )
    assert "- Draw the lantern in Ingrid's right hand." in text


def test_legacy_panels_without_purpose_still_compile() -> None:
    text = compile_(panel=panel(purpose="", focus="")).prompt
    assert "Panel purpose" not in text and "Focal point" not in text
    assert "Framing: Medium shot" in text


# ------------------------------------------------------------------ lettering plan
def test_lettering_space_follows_the_speaker_and_the_amount_of_text() -> None:
    left = art.lettering_plan(panel(), (560, 420))
    assert left.zone is not None and left.zone[0] == 0.0 and left.zone[2] < 0.6
    assert "left side of the frame, above the speaker" in left.phrase and "1 balloon" in left.phrase
    right = art.lettering_plan(
        panel(characters=[{"id": "ingrid", "position": "right", "pose": "p", "expression": "e"}]), (560, 420)
    )
    assert right.zone is not None and right.zone[0] > 0.4
    wordy = art.lettering_plan(panel(dialogue=[{"type": "speech", "speaker": "ingrid",
                                                "text": "That bird has taken my lunch three days running and I am done."}]),
                               (560, 420))  # fmt: skip
    assert wordy.zone is not None and wordy.zone[3] > left.zone[3]  # more text, more room
    both = art.lettering_plan(panel(characters=[{"id": "ingrid", "position": "left", "pose": "p", "expression": "e"},
                                                {"id": "gull", "position": "right", "pose": "p", "expression": "e"}],
                                    dialogue=[{"type": "speech", "speaker": "ingrid", "text": "Shoo."},
                                              {"type": "speech", "speaker": "gull", "text": "Mine."}]), (560, 420))  # fmt: skip
    assert both.zone is not None and both.zone[2] == 1.0 and "above the speakers" in both.phrase


def test_no_lettering_no_reserved_space() -> None:
    silent = panel(dialogue=[])
    assert art.lettering_plan(silent, (560, 420)).zone is None
    assert "Lettering space" not in compile_(panel=silent).prompt
    sfx = panel(dialogue=[{"type": "sfx", "text": "SQUAWK"}])
    assert art.lettering_plan(sfx, (560, 420)).zone is None  # sound effects sit near the action
    caption = art.lettering_plan(panel(dialogue=[{"type": "caption", "text": "Dawn."}]), (560, 420))
    assert caption.zone is not None and caption.zone[2] == 1.0 and "1 caption" in caption.phrase


RECT = (40.0, 40.0, 560.0, 420.0)


def test_letterer_uses_the_planned_space_and_keeps_props_clear() -> None:
    chars = [{"id": "ingrid", "position": "right"}]
    speech = [{"type": "speech", "speaker": "ingrid", "text": "Not again."}]
    zone = {"x": 0.45, "y": 0.0, "w": 0.55, "h": 0.3}
    placed = lettering.layout_panel(
        panel_rect=RECT, page_size=(1000, 1500), dialogue=speech, characters=chars, zone=zone
    )[0]
    zx, zy, zw, zh = RECT[0] + zone["x"] * RECT[2], RECT[1], zone["w"] * RECT[2], zone["h"] * RECT[3]
    inside = lettering.overlap((placed.x, placed.y, placed.w, placed.h), (zx, zy, zw, zh)) / (
        placed.w * placed.h
    )
    assert inside > 0.9 and "covers_face" not in placed.flags
    # a required prop exactly where the balloon would go: the balloon moves off it
    prop = {"lantern": {"x": (placed.x - RECT[0]) / RECT[2], "y": (placed.y - RECT[1]) / RECT[3],
                        "w": placed.w / RECT[2], "h": placed.h / RECT[3]}}  # fmt: skip
    moved = lettering.layout_panel(panel_rect=RECT, page_size=(1000, 1500), dialogue=speech, characters=chars,
                                   zone=zone, prop_boxes=prop)[0]  # fmt: skip
    pr = lettering._abs(prop["lantern"], RECT)
    assert (
        pr is not None and lettering.overlap((moved.x, moved.y, moved.w, moved.h), pr) < 0.25 * pr[2] * pr[3]
    )
    assert "covers_prop" not in moved.flags


def test_letterer_flags_a_prop_it_cannot_avoid() -> None:
    # a small panel the balloon nearly fills, with the lantern in its middle: there is nowhere to go
    small = (40.0, 40.0, 220.0, 140.0)
    lantern = {"lantern": {"x": 0.3, "y": 0.2, "w": 0.4, "h": 0.4}}
    b = lettering.layout_panel(panel_rect=small, page_size=(1000, 1500), characters=[{"id": "ingrid", "position": "left"}],
                               dialogue=[{"type": "speech", "speaker": "ingrid",
                                          "text": "That bird has taken my lunch three days running and I am done with it."}],
                               prop_boxes=lantern)[0]  # fmt: skip
    assert "covers_prop" in b.flags or "covers_face" in b.flags
    # the size is never reduced below the readable minimum to dodge it
    assert b.font_size >= lettering.READABLE_MIN


# ------------------------------------------------------------------ story checks
def script(*panels: PanelSpec) -> ComicScript:
    return ComicScript(pages=[PageSpec(panels=list(panels))])


BEATS = BeatSheet.model_validate({"structure": "kishotenketsu", "beats": [
    {"name": n, "summary": f"{n} happens", "pages": [1]} for n in ("setup", "development", "turn", "payoff")]})  # fmt: skip


def good_strip() -> ComicScript:
    one = [{"id": "ingrid", "position": "left", "pose": "eating", "expression": "content"}]
    two = one + [{"id": "gull", "position": "right", "pose": "landing", "expression": "greedy"}]
    return script(
        panel(shot="wide", characters=one, action="Ingrid eats lunch on the cliff", purpose="Ingrid and her lunch",
              beats=["setup"], dialogue=[]),
        panel(shot="medium", characters=two, action="A gull lands beside her", purpose="A rival arrives",
              beats=["development"], dialogue=[{"type": "speech", "speaker": "ingrid", "text": "Don't even think about it."}]),
        panel(shot="close_up", characters=two, action="Ingrid lifts the lantern; the gull flinches", purpose="She defends it",
              beats=["turn", "payoff"], dialogue=[{"type": "sfx", "text": "SQUAWK"}]),
    )  # fmt: skip


def test_a_well_formed_strip_gets_no_story_warnings() -> None:
    assert story_checks(good_strip(), BEATS) == []


def test_story_checks_find_repeats_lookalikes_flat_framing_and_a_missing_payoff() -> None:
    same = panel(
        beats=["setup"],
        dialogue=[{"type": "speech", "speaker": "ingrid", "text": "I calculated you were hungry."}],
    )
    issues = story_checks(script(same, same.model_copy(update={"beats": ["development"]}),
                                 same.model_copy(update={"beats": ["turn"], "action": "Ingrid sighs"})), BEATS)  # fmt: skip
    codes = [i.code for i in issues]
    assert codes.count("repeated_dialogue") == 2
    assert "interchangeable_panels" in codes and "unvaried_framing" in codes and "payoff_not_last" in codes
    assert all(i.severity == "warning" for i in issues)  # advice; never blocks approval


def test_purpose_missing_only_when_the_script_uses_purposes() -> None:
    s = good_strip()
    s.pages[0].panels[1] = s.pages[0].panels[1].model_copy(update={"purpose": ""})
    assert [i.code for i in story_checks(s, BEATS)] == ["purpose_missing"]
    legacy = script(*[p.model_copy(update={"purpose": ""}) for p in good_strip().pages[0].panels])
    assert story_checks(legacy, BEATS) == []  # scripts written before v3 are not penalised


def test_story_warnings_reach_validation_without_blocking() -> None:
    s = good_strip()
    s.pages[0].panels[2] = PanelSpec.model_validate({**s.pages[0].panels[2].model_dump(), "dialogue": [
        {"type": "speech", "speaker": "ingrid", "text": "Don't even think about it."}]})  # fmt: skip
    issues = validate_script(s, fmt=ComicFormat.STRIP, expected_pages=1, character_ids={"ingrid", "gull"},
                             location_ids={"cliff"}, beats=BEATS)  # fmt: skip
    assert any(i.code == "repeated_dialogue" for i in issues)
    assert not any(i.severity == "error" for i in issues)


def test_story_bible_check_storytelling_judgments_are_advice() -> None:
    s = good_strip()
    report = ScriptConsistencyReport.model_validate({
        "panels": [{"page": 1, "panel": 2, "rules": [], "beats": [], "adds": "no", "restates_art": True,
                    "note": "It repeats panel 1."}],
        "conflicts": [], "payoff": "weak", "payoff_reason": "The gull simply leaves."})  # fmt: skip
    issues = consistency.issues_from_report(report, s, [])
    codes = {i.code: i for i in issues}
    assert set(codes) >= {"panel_adds_nothing", "dialogue_restates_art", "weak_payoff"}
    assert all(
        codes[c].severity == "warning" for c in ("panel_adds_nothing", "dialogue_restates_art", "weak_payoff")
    )
    assert "It repeats panel 1." in codes["panel_adds_nothing"].message
    assert "The gull simply leaves." in codes["weak_payoff"].message
    # a report without the new fields (older prompt) raises nothing extra
    plain = ScriptConsistencyReport.model_validate({"panels": [{"page": 1, "panel": 1, "rules": [], "beats": []}],
                                                    "conflicts": []})  # fmt: skip
    assert not [
        i
        for i in consistency.issues_from_report(plain, s, [])
        if i.code in consistency.CONSISTENCY_CODES
        and i.code in ("panel_adds_nothing", "dialogue_restates_art", "weak_payoff")
    ]


# ------------------------------------------------------------------ QA aspects and repair
def report(**over: Any) -> VisionQAReport:
    base = {"character_count": 1, "characters": [{"id": "ingrid", "present": True, "missing_features": [], "identity_rating": 5}],
            "action_matches": True, "visible_text": False, "anatomy_defects": [], "fix_notes": [], "required_elements": []}  # fmt: skip
    return VisionQAReport.model_validate({**base, **over})


def test_aspects_keep_story_elements_identity_and_composition_apart() -> None:
    a = qa.aspects(["action", "rule_uncertain:lantern", "identity_unverified:ingrid", "anatomy"])
    assert a == {
        "story": "defect",
        "required_elements": "uncertain",
        "identity": "not_checked",
        "composition": "defect",
    }
    assert qa.aspects([]) == {"story": "ok", "required_elements": "ok", "identity": "ok", "composition": "ok"}
    assert qa.aspects(["identity_uncertain:ingrid", "identity_visual:ingrid"])["identity"] == "defect"


def test_every_blocking_problem_gets_a_concrete_repair_note() -> None:
    p = panel(characters=[{"id": "ingrid", "position": "left", "pose": "p", "expression": "e"},
                          {"id": "gull", "position": "right", "pose": "p", "expression": "e"}])  # fmt: skip
    cast = {"ingrid": INGRID, "gull": GULL}
    notes = qa.actionable_notes(["missing:gull", "character_count", "stray_text", "anatomy"], [], panel=p, cast=cast,
                                report=report(anatomy_defects=["six fingers on the left hand"]))  # fmt: skip
    assert "Draw Gull on the right, as in the reference image." in notes
    assert "Show exactly 2 characters (Ingrid, Gull); no one else." in notes
    assert any("Remove every letter" in n for n in notes)
    assert "Fix these drawing defects: six fingers on the left hand." in notes
    # the reviewer's own specific note is kept and not duplicated
    kept = qa.actionable_notes(["features:ingrid"], ["Ingrid's spectacles are missing; draw them."], panel=p, cast=cast,
                               report=report())  # fmt: skip
    assert kept == ["Ingrid's spectacles are missing; draw them."]
    action = qa.actionable_notes(["action"], [], panel=p, cast=cast, report=report())
    assert action == ["Show this action clearly: Ingrid spots the gull on her sandwich."]


def test_a_second_repair_of_the_same_problem_is_not_paid_for() -> None:
    assert qa.repeated_after_repair(["features:ingrid"], ["features:ingrid", "action"])
    assert not qa.repeated_after_repair(["features:ingrid", "anatomy"], ["features:ingrid"])  # something new
    assert not qa.repeated_after_repair(
        ["identity_uncertain:ingrid"], ["identity_uncertain:ingrid"]
    )  # review only
    assert not qa.repeated_after_repair([], ["action"])
