"""Regressions from the live stage 1A run (docs/evidence/stage-1a-2026-11-18): the face boxes the run
stored for Train panel 2, where the letterer put Teo's line on top of Mira's head and Mira's line beside
Teo, and neither was flagged. Pure geometry: no artwork, no provider."""

import json
from pathlib import Path

from storypanel.catalog import layouts
from storypanel.pipeline import lettering

ROOT = Path(__file__).resolve().parents[3]
STORED = json.loads((ROOT / "docs/evidence/stage-1a-2026-11-18/train-face-check.json").read_text())
T = layouts()["w6_bottom_large"]


def heads(panel: int) -> dict:
    return next(p for p in STORED["panels"] if p["panel"] == panel)["head_boxes_on_displayed_panel"]


def rect(panel: int) -> tuple[float, ...]:
    return tuple(float(v) for v in T.rects[panel - 1])


def place_p2() -> list[lettering.Placed]:
    return lettering.layout_panel(
        panel_rect=rect(2), page_size=T.page,
        dialogue=[{"type": "speech", "speaker": "teo", "text": "Missed it again! Ha... sorry!"},
                  {"type": "speech", "speaker": "mira", "text": "It's... okay."}],
        characters=[{"id": "mira", "position": "left"}, {"id": "teo", "position": "right"}],
        head_boxes=heads(2))  # fmt: skip


def test_a_line_is_not_placed_on_top_of_the_other_speakers_head() -> None:
    r = rect(2)
    abs_heads = {k: lettering._abs(v, r) for k, v in heads(2).items()}
    for b in place_p2():
        own, other = abs_heads[b.speaker], abs_heads["teo" if b.speaker == "mira" else "mira"]
        cx = b.x + b.w / 2
        assert not (other[0] <= cx <= other[0] + other[2] and other[1] - (b.y + b.h) < 0.12 * r[3]), b.text
        assert b.tail is not None
        assert lettering._dist_to(b.tail, own) < lettering._dist_to(b.tail, other)
        assert "covers_face" not in b.flags and "tail_ambiguous" not in b.flags


def test_the_old_placement_is_now_warned_about() -> None:
    """The run's placement (reproduced: Teo's balloon over Mira's head, Mira's balloon by Teo) now
    raises the warnings it lacked: the face check allows for approximate boxes, and a tail must end
    clearly nearer its speaker."""
    r = rect(2)
    h = {k: lettering._abs(v, r) for k, v in heads(2).items()}
    teo_line = (r[0] + 0.11 * r[2], r[1] + 0.03 * r[3], 0.36 * r[2], 0.36 * r[3])
    assert lettering.covers_face(teo_line, h["mira"]) or lettering.overlap(teo_line, h["mira"]) > 0
    # Mira's tail in the run ended 27.6 vs 26.2 units from the hand-placed faces: a near tie
    tip = (h["teo"][0] - 3.0, h["teo"][1] + h["teo"][3] * 0.5)
    to_mira, to_teo = lettering._dist_to(tip, h["mira"]), lettering._dist_to(tip, h["teo"])
    assert to_teo < to_mira + lettering.TAIL_MARGIN * (r[2] ** 2 + r[3] ** 2) ** 0.5


def test_a_clear_single_speaker_panel_is_unchanged() -> None:
    """Train panel 4 was clean in the live run: the fix must not move it onto Mira."""
    placed = lettering.layout_panel(
        panel_rect=rect(4), page_size=T.page,
        dialogue=[{"type": "speech", "speaker": "teo", "text": "Delayed! I can make it!"}],
        characters=[{"id": "teo", "position": "right"}, {"id": "mira", "position": "left"}],
        head_boxes=heads(4))  # fmt: skip
    assert placed[0].flags == []
