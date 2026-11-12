"""A strip with a stated panel count is held to it, in live and simulated modes alike.

Before this change the user's "3 panel strip" reached the script writer only as free text and the
script rule said "3 to 4 panels", so neither the live model nor the simulated fixture had to honour
it (the fixture always wrote 4)."""

from __future__ import annotations

from typing import Any

from storypanel.domain.schemas import ComicBrief, ComicFormat, FormatSpec, StoryBible
from storypanel.domain.validation import validate_script
from storypanel.pipeline import story
from tests.conftest import wait_for
from tests.test_unit import KIRO, LOC, MEI, script


def _brief(panels: int | None, fmt: ComicFormat = ComicFormat.STRIP) -> ComicBrief:
    return ComicBrief(summary="s", genre=["comedy"], tone="warm", audience="all_ages", pages=3, format=fmt,  # type: ignore[arg-type]
                      style_preset="clean_manga_warm", named_characters=[], setting="pier", must_haves=[],
                      panels=panels)  # fmt: skip


def _bible(panels: int | None) -> StoryBible:
    bible = StoryBible.model_construct(format=FormatSpec(type=ComicFormat.STRIP, pages=1, panels_per_page="3",
                                                         panels=panels))  # fmt: skip
    return bible


def test_brief_keeps_a_strip_panel_count_within_product_limits() -> None:
    presets = ["clean_manga_warm"]
    assert story.normalise_brief(_brief(3), presets).panels == 3
    assert story.normalise_brief(_brief(9), presets).panels == 4  # clamped to the strip maximum
    assert story.normalise_brief(_brief(None), presets).panels is None
    assert story.normalise_brief(_brief(3, ComicFormat.WESTERN_PAGE), presets).panels is None


def test_requested_count_is_exact_for_the_script() -> None:
    def errors(bible: StoryBible, n: int) -> set[str]:
        return {i.code for i in validate_script(script(pages=1, panels=n), fmt=ComicFormat.STRIP, expected_pages=1,
                                                character_ids={KIRO.id, MEI.id}, location_ids={LOC.id},
                                                limits=story.script_limits(bible)) if i.severity == "error"}  # fmt: skip

    assert "panel_count" in errors(_bible(3), 4)
    assert "panel_count" not in errors(_bible(3), 3)
    assert "panel_count" not in errors(_bible(None), 4)  # no stated count: 3-4 as before


async def test_three_panel_strip_is_drawn_with_three_panels_and_edits_are_held_to_it(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    prompt = "A lighthouse keeper and a cheeky seagull argue over a sandwich, as a 3 panel strip"
    r = await client.post("/v1/projects", json={"prompt": prompt}, headers=headers)
    pid = r.json()["id"]
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_cast_approval")
    await client.post(f"/v1/projects/{pid}/characters/approve", json={"cast_version": p["cast_version"]},
                      headers=headers)  # fmt: skip
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_script_approval")
    pages = p["script"]["content"]["pages"]
    assert [len(pg["panels"]) for pg in pages] == [3]  # before approval: nothing paid for panels yet
    # A user edit that adds a fourth panel is flagged and cannot be approved.
    four = {"pages": [{"panels": pages[0]["panels"] + [pages[0]["panels"][-1]]}]}
    r = await client.patch(f"/v1/projects/{pid}/script", json={"base_version": p["script_version"], "script": four},
                           headers=headers)  # fmt: skip
    assert r.status_code == 200, r.text
    assert "panel_count" in {i["code"] for i in r.json()["issues"] if i["severity"] == "error"}
