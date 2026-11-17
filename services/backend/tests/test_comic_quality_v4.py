"""Prompts v4: fixes for what two real comics showed (docs/quality/baseline-2.md).

- the payoff leaked into early panels (Train: the ticket stub on the table from panel 1; Tulips: the
  tulips in bloom before the reveal);
- a balloon covered a face in a tall narrow panel and nothing warned at export;
- reversed speaker order read ambiguously;
- false "turnaround cut off" alarms, and facial details judged from full-body images.

Deterministic except the last test, which runs the simulated pipeline. No paid calls.
"""

from __future__ import annotations

from typing import Any

import pytest

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
from storypanel.pipeline import art, consistency, lettering, qa
from storypanel.pipeline.stages_art import lettering_problems, merge_print_quality
from storypanel.pipeline.stages_plan import layout_flags_sheet


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
TICKET = ContinuityRule(id="ticket", text="The ticket stub left on the table has a small drawing of Mira on the back",
                        applies_to="location", target="cafe")  # fmt: skip
SKETCHBOOK = ContinuityRule(id="sketchbook", text="Mira carries a worn sketchbook with a red ribbon bookmark",
                            applies_to="character", target="mira")  # fmt: skip
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


def compile_(**kw: Any) -> art.CompiledPanel:
    args: dict[str, Any] = dict(panel=panel(), aspect="9:16", style_preset="clean_manga_warm",
                                cast={"mira": MIRA, "teo": TEO}, locations={"cafe": CAFE}, character_refs=REFS,
                                location_refs={"cafe": "loc"}, continuity=("prev", "prev-asset"), max_references=4,
                                rules=[SKETCHBOOK], panel_size=(300.0, 520.0))  # fmt: skip
    return art.compile_panel(**{**args, **kw})


# ------------------------------------------------------------------ reveals stay hidden
def test_new_comics_use_v4() -> None:
    assert PROMPT_VERSION == "v4" and compile_().prompt_version == "v4"


def test_things_revealed_later_are_named_as_not_in_the_panel() -> None:
    c = compile_(panel=panel(not_shown=["the ticket stub on the table"]))
    assert "Not in this panel (the story reveals it later): the ticket stub on the table." in c.prompt
    assert c.inputs["not_shown"] == ["the ticket stub on the table"]
    # and the location picture can't bring it back: limited to architecture and light
    assert (
        "architecture and light only; plants, objects and their state as described for this panel" in c.prompt
    )
    # from the reveal on, nothing is held back and the location reference is used in full
    reveal = compile_(panel=panel(not_shown=[]))
    assert "Not in this panel" not in reveal.prompt
    assert "architecture, props, colours and light; not the camera angle" in reveal.prompt


def test_an_excepted_rule_is_stated_not_silently_dropped() -> None:
    bloom = ContinuityRule(
        id="bloom", text="The tulips in the garden are in bloom.", applies_to="location", target="cafe"
    )
    c = compile_(panel=panel(rule_exceptions=["bloom"]), rules=[SKETCHBOOK, bloom])
    assert 'Exception in this panel: "The tulips in the garden are in bloom" does not hold here' in c.prompt
    assert "Must be clearly visible: The tulips" not in c.prompt
    assert c.inputs["excepted_rules"] == ["bloom"]
    assert "architecture and light only" in c.prompt  # a location rule excepted: the reference is limited
    # without the exception, it is required as before
    assert (
        "Must be clearly visible: The tulips in the garden are in bloom."
        in compile_(rules=[SKETCHBOOK, bloom]).prompt
    )


def test_previous_panel_reference_only_when_the_setting_is_not_anchored() -> None:
    anchored = compile_()
    assert [r.role for r in anchored.references] == ["character", "location"]
    assert anchored.inputs["references_omitted"] == [
        "previous panel: not needed, the location reference anchors the setting"
    ]
    unanchored = compile_(location_refs={})
    assert [r.role for r in unanchored.references] == ["character", "continuity"]
    assert "the previous panel, only to keep lighting" in unanchored.prompt


def script(*panels: PanelSpec) -> ComicScript:
    return ComicScript(pages=[PageSpec(panels=list(panels))])


def train_strip() -> ComicScript:
    both = [{"id": "teo", "position": "left", "pose": "panting", "expression": "sheepish"},
            {"id": "mira", "position": "right", "pose": "looking up", "expression": "amused"}]  # fmt: skip
    return script(
        panel(beats=["setup"], not_shown=["the ticket stub"]),
        panel(characters=both, beats=["development"], not_shown=["the ticket stub"], purpose="Teo arrives late",
              dialogue=[{"type": "speech", "speaker": "teo", "text": "Missed it again!"},
                        {"type": "speech", "speaker": "mira", "text": "It's okay."}]),
        panel(shot="wide", beats=["turn"], not_shown=["the ticket stub"], purpose="He finally leaves",
              action="Teo's train pulls away; Mira watches from the window"),
        panel(shot="close_up", beats=["payoff"], purpose="The reveal",
              action="Mira picks up a ticket stub with a drawing of her on the back"),
    )  # fmt: skip


def test_a_rule_about_the_ending_is_flagged_and_an_ordinary_prop_rule_is_not() -> None:
    issues = story_checks(train_strip(), BEATS, [TICKET, SKETCHBOOK])
    reveal = [i for i in issues if i.code == "reveal_rule"]
    assert len(reveal) == 1 and reveal[0].path == "rule:ticket" and reveal[0].severity == "warning"
    assert "3 earlier panel(s)" in reveal[0].message
    # excepting it for the early panels resolves it
    s = train_strip()
    for p in s.pages[0].panels[:-1]:
        p.rule_exceptions.append("ticket")
    assert not [i for i in story_checks(s, BEATS, [TICKET]) if i.code == "reveal_rule"]


def test_not_shown_contradicting_the_action_is_reported() -> None:
    bad = script(panel(beats=["setup"], not_shown=["the ticket stub"],
                       action="Mira sketches beside the ticket stub on the table"),
                 *train_strip().pages[0].panels[1:])  # fmt: skip
    assert any(i.code == "not_shown_conflict" for i in story_checks(bad, BEATS))
    assert not any(i.code == "not_shown_conflict" for i in story_checks(train_strip(), BEATS))


def test_too_many_not_shown_items_are_refused() -> None:
    s = script(panel(not_shown=["a", "b", "c", "d", "e"]))
    issues = validate_script(s, fmt=ComicFormat.WESTERN_PAGE, expected_pages=1, character_ids={"mira", "teo"},
                             location_ids={"cafe"})  # fmt: skip
    assert any(i.code == "text_too_long" and i.path.endswith(".not_shown") for i in issues)


def test_the_story_bible_check_reports_an_early_reveal_as_advice() -> None:
    report = ScriptConsistencyReport.model_validate({"panels": [
        {"page": 1, "panel": 1, "rules": [], "beats": [], "reveals_early": True, "note": "The stub is already on the table."}],
        "conflicts": []})  # fmt: skip
    issues = [
        i for i in consistency.issues_from_report(report, train_strip(), []) if i.code == "reveal_too_early"
    ]
    assert (
        len(issues) == 1
        and issues[0].severity == "warning"
        and "The stub is already on the table." in issues[0].message
    )


def vqa(**over: Any) -> VisionQAReport:
    base = {"character_count": 1, "characters": [{"id": "mira", "present": True, "missing_features": [], "identity_rating": 5}],
            "action_matches": True, "visible_text": False, "anatomy_defects": [], "fix_notes": [], "required_elements": []}  # fmt: skip
    return VisionQAReport.model_validate({**base, **over})


async def run_qa(monkeypatch: pytest.MonkeyPatch, report: VisionQAReport, absent: list[str]) -> qa.QAOutcome:
    from storypanel.domain.schemas import SafetyVerdict
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
    from storypanel.domain.schemas import AgeRating

    out = await qa.evaluate_panel(image=b"", panel=panel(), cast={"mira": MIRA}, audience=AgeRating.ALL_AGES,
                                  reference_images={}, reference_embeddings={}, ctx=CallContext("panel_qa", "u", "p"),
                                  absent=absent)  # fmt: skip
    if absent:
        assert "Must NOT be visible yet" in sent[-1] and "- 1: the ticket stub" in sent[-1]
    return out


async def test_a_spoiled_reveal_is_a_repairable_defect_with_a_concrete_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = await run_qa(
        monkeypatch, vqa(absent_elements=[{"number": 1, "visible": "yes"}]), ["the ticket stub"]
    )
    assert out.status == "fail" and "shown_too_early:1" in out.scores["problems"]
    assert "Remove the ticket stub from this panel: it must not be visible yet." in out.fix_notes
    assert out.scores["aspects"]["story"] == "defect"
    unsure = await run_qa(
        monkeypatch, vqa(absent_elements=[{"number": 1, "visible": "uncertain"}]), ["the ticket stub"]
    )
    assert unsure.status == "pass"  # not evidence either way: no paid repair
    clean = await run_qa(
        monkeypatch, vqa(absent_elements=[{"number": 1, "visible": "no"}]), ["the ticket stub"]
    )
    assert clean.status == "pass" and clean.fix_notes == []


# ------------------------------------------------------------------ reading order and faces
def test_reversed_speakers_read_in_order() -> None:
    """Train panel 2: Teo (right) speaks first, Mira (left) answers. Her balloon must sit clearly lower."""
    rect = (40.0, 40.0, 500.0, 420.0)
    chars = [{"id": "mira", "position": "left"}, {"id": "teo", "position": "right"}]
    first, second = lettering.layout_panel(panel_rect=rect, page_size=(1000, 1500), characters=chars, dialogue=[
        {"type": "speech", "speaker": "teo", "text": "Missed it again! Ha... sorry!"},
        {"type": "speech", "speaker": "mira", "text": "It's... okay."}])  # fmt: skip
    if second.x + second.w / 2 < first.x + first.w / 2:
        assert second.y >= first.y + first.h * 0.6 or "reading_order" in second.flags
    assert "reading_order" not in first.flags


def test_reversed_speakers_are_flagged_in_the_script() -> None:
    s = train_strip()
    s.pages[0].panels[1] = PanelSpec.model_validate({**s.pages[0].panels[1].model_dump(), "characters": [
        {"id": "teo", "position": "right", "pose": "p", "expression": "e"},
        {"id": "mira", "position": "left", "pose": "p", "expression": "e"}]})  # fmt: skip
    assert any(i.code == "speaker_order" for i in story_checks(s, BEATS))
    assert not any(i.code == "speaker_order" for i in story_checks(train_strip(), BEATS))


def test_a_balloon_crosses_the_border_rather_than_sit_on_a_face() -> None:
    """Train panel 4: a tall, narrow panel whose speaker fills the top."""
    rect = (300.0, 400.0, 220.0, 420.0)
    head = {"teo": {"x": 0.15, "y": 0.04, "w": 0.7, "h": 0.36}}
    b = lettering.layout_panel(panel_rect=rect, page_size=(1000, 1500), characters=[{"id": "teo", "position": "center"}],
                               head_boxes=head, body_boxes={"teo": {"x": 0.1, "y": 0.04, "w": 0.8, "h": 0.95}},
                               dialogue=[{"type": "speech", "speaker": "teo", "text": "Delayed! I can make it!"}])[0]  # fmt: skip
    h = lettering._abs(head["teo"], rect)
    assert h is not None
    covered = lettering.overlap((b.x, b.y, b.w, b.h), h) / (h[2] * h[3])
    assert covered <= 0.15 or "covers_face" in b.flags  # never silently on a face
    assert b.font_size >= lettering.READABLE_MIN


def test_unresolved_lettering_problems_are_reported_with_the_export() -> None:
    page = {"size": [1000, 1500], "panels": [
        {"balloons": [{"type": "speech", "flags": ["covers_face", "breaks_border"]}]},
        {"balloons": [{"type": "speech", "flags": []}]},
        {"balloons": [{"type": "speech", "flags": ["covers_prop", "overflow"]}]}]}  # fmt: skip
    rows = lettering_problems(0, page)
    assert rows == [
        {"page": 1, "panel": 1, "flags": ["covers_face"]},
        {"page": 1, "panel": 3, "flags": ["covers_prop", "overflow"]},
    ]
    q = merge_print_quality(None, 0, [], "local_resample", flagged=rows)
    assert (
        "Page 1, panel 1: a balloon covers a face. In the editor, move or shorten the balloon, or redraw the panel."
        in q["warnings"]
    )
    assert any(
        "panel 3: a balloon covers an important object; text doesn't fit at a readable size" in w
        for w in q["warnings"]
    )
    assert (
        merge_print_quality(None, 0, [], "local_resample")["warnings"] == []
    )  # nothing to report: no warning


# ------------------------------------------------------------------ sheet review
def test_only_the_turnaround_layout_can_flag_a_sheet() -> None:
    assert not layout_flags_sheet({"turnaround": {"method": "grid"}, "expressions": {"method": "unverified"}})
    assert layout_flags_sheet({"turnaround": {"method": "unverified"}, "expressions": {"method": "grid"}})
    assert not layout_flags_sheet({})


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
    assert len(seen[-1]) == 2  # older sheets without an expression sheet still work


# ------------------------------------------------------------------ end to end (simulated)
@pytest.mark.workflow
async def test_a_spoiled_reveal_is_repaired_once_with_its_note(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    from sqlalchemy import select

    from storypanel.db.models import PanelVersion
    from storypanel.db.session import transaction
    from storypanel.providers import fake
    from tests.conftest import run_to_completion

    fake.control.script_not_shown = ["the gift box"]
    fake.control.shown_early_attempts = 1  # the first picture of each early panel shows it anyway
    p = await run_to_completion(
        client, headers, "A robot and a girl share noodles in the rain, as a 4 panel strip"
    )
    panels = [pn for pg in p["pages"] for pn in pg["panels"]]
    assert all(x["status"] == "ok" for x in panels)
    assert [x["spec"]["not_shown"] for x in panels] == [["the gift box"]] * 3 + [[]]
    async with transaction() as db:
        rows = (await db.execute(select(PanelVersion.attempt, PanelVersion.qa_scores)
                                 .order_by(PanelVersion.created_at))).all()  # fmt: skip
    first = [s for a, s in rows if a == 0 and "shown_too_early:1" in (s or {}).get("problems", [])]
    assert len(first) == 3  # the three early panels failed the first time, the reveal panel did not
    assert all(
        "Remove the gift box from this panel: it must not be visible yet." in s["fix_notes"] for s in first
    )
    assert sum(1 for a, _ in rows if a == 1) == 3  # one repair each, then they passed
