"""Balloon and sound-effect placement against characters (deterministic fixtures).

Boxes are fixed fractions of the panel, the way the QA check reports them. "Before" is the same
call without body boxes, which is what the letterer received until now. These prove the placement
rules; whether they look right on real generated art still needs real images (pending).
"""

from __future__ import annotations

from storypanel.pipeline import lettering

PANEL = (40.0, 40.0, 448.0, 698.0)  # a page panel
NARROW = (40.0, 40.0, 322.0, 480.0)  # a 4-panel strip slot
PAGE = (1000, 1500)


def _abs(b: dict[str, float], panel: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    px, py, pw, ph = panel
    return (px + b["x"] * pw, py + b["y"] * ph, b["w"] * pw, b["h"] * ph)


def _share(p: lettering.Placed, box: tuple[float, float, float, float]) -> float:
    return lettering.overlap((p.x, p.y, p.w, p.h), box) / (box[2] * box[3])


# A character standing on the left third, head near the top: the right side of the panel is free.
HEAD = {"x": 0.1, "y": 0.12, "w": 0.24, "h": 0.16}
BODY = {"x": 0.05, "y": 0.1, "w": 0.35, "h": 0.85}
CHAR = [{"id": "gull", "position": "left"}]


def test_a_thought_balloon_moves_off_the_body_when_there_is_room() -> None:
    text = "I will guard this fry until the tide comes back in, whatever it takes."
    dialogue = [{"type": "thought", "text": text, "speaker": "gull"}]
    before = lettering.layout_panel(panel_rect=PANEL, page_size=PAGE, dialogue=dialogue, characters=CHAR,
                                    head_boxes={"gull": HEAD})[0]  # fmt: skip
    after = lettering.layout_panel(panel_rect=PANEL, page_size=PAGE, dialogue=dialogue, characters=CHAR,
                                   head_boxes={"gull": HEAD}, body_boxes={"gull": BODY})[0]  # fmt: skip
    body = _abs(BODY, PANEL)
    assert _share(after, body) < _share(before, body)
    assert _share(after, body) < 0.05 and "covers_subject" not in after.flags


def test_a_sound_effect_stays_off_faces_and_bodies() -> None:
    action = {"x": 0.05, "y": 0.3, "w": 0.35, "h": 0.4}  # the action is the character itself
    dialogue = [{"type": "sfx", "text": "SQUAWK!"}]
    before = lettering.layout_panel(panel_rect=PANEL, page_size=PAGE, dialogue=dialogue, characters=[],
                                    action_box=action)[0]  # fmt: skip
    after = lettering.layout_panel(panel_rect=PANEL, page_size=PAGE, dialogue=dialogue, characters=CHAR,
                                   head_boxes={"gull": HEAD}, body_boxes={"gull": BODY}, action_box=action)[0]  # fmt: skip
    head, body = _abs(HEAD, PANEL), _abs(BODY, PANEL)
    assert _share(before, head) > 0  # without character boxes it landed on the face
    assert _share(after, head) == 0 and _share(after, body) < _share(before, body)


def test_when_there_is_no_room_the_overlap_is_flagged_not_hidden() -> None:
    wide_body = {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}  # the character fills the narrow panel
    dialogue = [
        {"type": "speech", "text": "This fry is mine and mine alone, everybody hear that?", "speaker": "gull"}
    ]
    (p,) = lettering.layout_panel(panel_rect=NARROW, page_size=(1600, 560), dialogue=dialogue, characters=CHAR,
                                  head_boxes={"gull": {"x": 0.3, "y": 0.05, "w": 0.4, "h": 0.25}},
                                  body_boxes={"gull": wide_body})  # fmt: skip
    assert "covers_subject" in p.flags or "covers_face" in p.flags


def test_without_body_boxes_placement_is_unchanged() -> None:
    """Comics lettered before body boxes were stored (or whose QA found none) keep their layout."""
    dialogue = [{"type": "speech", "text": "Same time tomorrow?", "speaker": "gull"}]
    a = lettering.layout_panel(panel_rect=PANEL, page_size=PAGE, dialogue=dialogue, characters=CHAR,
                               head_boxes={"gull": HEAD})[0]  # fmt: skip
    b = lettering.layout_panel(panel_rect=PANEL, page_size=PAGE, dialogue=dialogue, characters=CHAR,
                               head_boxes={"gull": HEAD}, body_boxes={})[0]  # fmt: skip
    assert (a.x, a.y, a.w, a.h) == (b.x, b.y, b.w, b.h)
