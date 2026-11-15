"""Story Bible fidelity: continuity rules in prompts, beat coverage, consistency checks with
recorded overrides, reference-sheet review, and required elements in panel QA.

Simulated providers only: judgments come from FakeControl, so these tests prove the plumbing and
the decisions made from each verdict, not the accuracy of any real model.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from storypanel.db.models import Approval
from storypanel.db.session import transaction
from storypanel.domain.schemas import (
    BeatSheet,
    CharacterSpec,
    ComicFormat,
    ComicScript,
    ContinuityRule,
    PageSpec,
    PanelSpec,
    ScriptConsistencyReport,
)  # fmt: skip
from storypanel.domain.validation import beat_coverage, validate_script
from storypanel.pipeline import art, consistency
from storypanel.providers import fake
from tests.conftest import wait_checked, wait_for

STRIP = "A robot and a girl share noodles in the rain, as a 4 panel strip"
APRON = ContinuityRule(
    id="apron_always", text="Kiro wears the red apron", applies_to="character", target="kiro"
)
RAIN = ContinuityRule(
    id="rain", text="light rain in the air", applies_to="location", target="stall", strength="prefer"
)
LORE = ContinuityRule(id="lore", text="robots are rarely befriended", applies_to="every_panel", visual=False)


def panel(**over: Any) -> PanelSpec:
    base = {"shot": "medium", "angle": "eye-level", "location_id": "stall", "time_of_day": "night",
            "characters": [{"id": "kiro", "position": "left", "pose": "ladling", "expression": "nervous"}],
            "action": "Kiro offers a bowl", "mood": "warm"}  # fmt: skip
    return PanelSpec.model_validate({**base, **over})


def spec() -> CharacterSpec:
    return CharacterSpec.model_validate({
        "id": "kiro", "name": "Kiro", "role": "lead", "personality": "shy", "voice": "short",
        "visual": {"type": "robot", "body": "round", "head": "dome", "hair": "none", "outfit": "red apron",
                   "palette": {"body": "#fff"}, "signature_features": ["visor eye"]}})  # fmt: skip


# ------------------------------------------------------------------ prompts (deterministic)
def compile_(p: PanelSpec, rules: list[ContinuityRule]) -> str:
    from storypanel.domain.schemas import LocationSpec

    return art.compile_panel(panel=p, aspect="1:1", style_preset="clean_manga_warm", cast={"kiro": spec()},
                             locations={"stall": LocationSpec(id="stall", name="Stall", description="A stall"),
                                        "roof": LocationSpec(id="roof", name="Roof", description="A roof")},
                             character_refs={}, location_refs={}, continuity=None, max_references=4,
                             rules=rules).prompt  # fmt: skip


def test_must_rules_reach_every_panel_they_apply_to_and_only_those() -> None:
    with_kiro = compile_(panel(), [APRON, RAIN, LORE])
    assert "Must be clearly visible: Kiro wears the red apron." in with_kiro
    assert "Where it fits naturally: light rain in the air." in with_kiro  # stylistic preference
    assert "rarely befriended" not in with_kiro  # not visual: never sent to images
    nobody = compile_(panel(characters=[], location_id="roof"), [APRON, RAIN])
    assert "red apron" not in nobody and "light rain" not in nobody
    excepted = compile_(panel(rule_exceptions=["apron_always"]), [APRON])
    assert "red apron" not in excepted.split("Kiro, ")[0] and "Must be clearly visible" not in excepted


def test_shot_and_climax_reach_the_prompt() -> None:
    close = compile_(panel(shot="close_up", emphasis="large"), [])
    assert "head and shoulders fill most of the frame" in close and "climactic moment" in close
    assert "full figures small" in compile_(panel(shot="wide"), [])


def test_sheet_and_edit_prompts_carry_the_character_rules() -> None:
    for kind in ("portrait", "turnaround", "expressions"):
        prompt, _ = art.compile_sheet_prompt(kind, spec(), "clean_manga_warm", rules=[APRON, RAIN])
        assert "Kiro wears the red apron" in prompt and "light rain" not in prompt
    turnaround, _ = art.compile_sheet_prompt("turnaround", spec(), "clean_manga_warm")
    assert "no figure is cut off by the image edge" in turnaround
    edit = art.compile_edit_prompt("add rain", "clean_manga_warm", [("Kiro", 2)], [APRON])
    assert "Must still be clearly visible: Kiro wears the red apron." in edit


# ------------------------------------------------------------------ beat coverage (deterministic)
BEATS = BeatSheet.model_validate({"structure": "kishotenketsu", "beats": [
    {"name": n, "summary": n, "pages": [1]} for n in ("setup", "development", "twist", "payoff")]})  # fmt: skip


def codes(script: ComicScript) -> dict[str, str]:
    return {i.code: i.severity for i in beat_coverage(script, BEATS)}


def test_beat_coverage_is_declared_coverage_checked_in_code() -> None:
    tagged = ComicScript(pages=[PageSpec(panels=[panel(beats=["setup", "development"]), panel(beats=["twist"]),
                                                 panel(beats=["payoff"])])])  # fmt: skip
    assert codes(tagged) == {}  # three panels may carry four beats
    missing = ComicScript(pages=[PageSpec(panels=[panel(beats=["setup"]), panel(beats=["development"]),
                                                  panel(beats=[])])])  # fmt: skip
    assert codes(missing) == {"beat_uncovered": "error"}
    odd = ComicScript(pages=[PageSpec(panels=[panel(beats=["setup", "development", "twist"]), panel(beats=["payoff"]),
                                              panel(beats=["setup", "climax"])])])  # fmt: skip
    assert codes(odd) == {"unknown_beat": "warning", "beat_order": "warning"}
    untagged = ComicScript(pages=[PageSpec(panels=[panel(), panel(), panel()])])
    assert codes(untagged) == {"beat_coverage_unchecked": "warning"}
    issues = validate_script(missing, fmt=ComicFormat.STRIP, expected_pages=1, character_ids={"kiro"},
                             location_ids={"stall"}, beats=BEATS)  # fmt: skip
    assert any(i.code == "beat_uncovered" and i.severity == "error" for i in issues)


def test_strip_large_panel_that_is_not_last_is_explained_not_reordered() -> None:
    s = ComicScript(pages=[PageSpec(panels=[panel(), panel(emphasis="large"), panel()])])
    issues = validate_script(s, fmt=ComicFormat.STRIP, expected_pages=1, character_ids={"kiro"},
                             location_ids={"stall"})  # fmt: skip
    (i,) = [i for i in issues if i.code == "strip_large_not_last"]
    assert i.severity == "warning" and "wide slot is the last panel" in i.message


# ------------------------------------------------------------------ consistency verdicts -> issues
def test_consistency_verdicts_become_blocking_review_or_nothing() -> None:
    script = ComicScript(pages=[PageSpec(panels=[panel(beats=["setup"]), panel(beats=["twist"]),
                                                 panel(rule_exceptions=["apron_always"], beats=["payoff"]),
                                                 panel(characters=[], beats=[])])])  # fmt: skip
    report = ScriptConsistencyReport.model_validate({"panels": [
        {"page": 1, "panel": 1, "rules": [{"rule_id": "apron_always", "verdict": "violated", "reason": "No apron."}],
         "beats": [{"beat": "setup", "expressed": "yes", "reason": "."}]},
        {"page": 1, "panel": 2, "rules": [{"rule_id": "apron_always", "verdict": "uncertain", "reason": "Unclear."}],
         "beats": [{"beat": "twist", "expressed": "no", "reason": "Nothing surprising happens."}]},
        {"page": 1, "panel": 3, "rules": [{"rule_id": "apron_always", "verdict": "violated", "reason": "x"}],
         "beats": [{"beat": "payoff", "expressed": "uncertain", "reason": "?"}]},
        {"page": 9, "panel": 9, "rules": [], "beats": []},
    ], "conflicts": [{"kind": "rule_vs_beat", "rule_ids": ["apron_always"], "description": "The twist removes it."}]})  # fmt: skip
    found = consistency.issues_from_report(report, script, [APRON])
    got = {(i.code, i.path): i.severity for i in found}
    assert [i.message.split(":")[0] for i in found[:3]] == [
        "Panel 1",
        "Panel 2",
        "Panel 2",
    ]  # tells them apart
    assert got == {
        ("rule_violation", "pages[0].panels[0].rule:apron_always"): "error",
        ("rule_uncertain", "pages[0].panels[1].rule:apron_always"): "review",
        ("beat_not_expressed", "pages[0].panels[1].beat:twist"): "review",
        # panel 3: the user recorded an exception, so its "violated" verdict is ignored
        ("beat_uncertain", "pages[0].panels[2].beat:payoff"): "review",
        ("rule_conflict", "continuity_rules[0]"): "review",
    }  # panel 4: nothing applies and no beats claimed -> nothing to report; page 9 does not exist


def test_nothing_to_check_makes_no_paid_call() -> None:
    from storypanel.pipeline.stages_plan import initial_consistency

    bible = type("B", (), {"continuity_rules": [RAIN]})()  # only a preference: nothing must hold
    untagged = ComicScript(pages=[PageSpec(panels=[panel()])])
    assert initial_consistency(untagged, bible)["status"] == "done"
    assert (
        initial_consistency(ComicScript(pages=[PageSpec(panels=[panel(beats=["setup"])])]), bible)["status"]
        == "pending"
    )


# ------------------------------------------------------------------ end to end (simulated)
async def start(client: Any, headers: dict[str, str]) -> str:
    r = await client.post("/v1/projects", json={"prompt": STRIP}, headers=headers)
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def to_script_gate(client: Any, headers: dict[str, str]) -> tuple[str, dict[str, Any]]:
    pid = await start(client, headers)
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_cast_approval")
    r = await client.post(
        f"/v1/projects/{pid}/characters/approve", json={"cast_version": p["cast_version"]}, headers=headers
    )
    assert r.status_code == 200, r.text
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_script_approval")
    return pid, p


async def overrides(pid: str, kind: str) -> Any:
    async with transaction() as db:
        return (await db.execute(select(Approval.overrides).where(
            Approval.project_id == uuid.UUID(pid), Approval.kind == kind, Approval.status == "approved"))).scalar_one()  # fmt: skip


@pytest.mark.workflow
async def test_the_review_screen_opens_only_after_the_bible_check(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    pid, p = await to_script_gate(client, headers)
    assert p["script"]["consistency"]["status"] == "done" and p["script"]["review_required"] == []
    assert ("script_consistency", "ScriptConsistencyReport") in fake.control.calls
    r = await client.post(
        f"/v1/projects/{pid}/script/approve", json={"script_version": p["script_version"]}, headers=headers
    )
    assert r.status_code == 200 and await overrides(pid, "script") is None


@pytest.mark.workflow
async def test_violated_rule_blocks_until_edited_or_excepted(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    fake.control.consistency_violated = {"apron_always"}
    pid, p = await to_script_gate(client, headers)
    viol = [i for i in p["script"]["issues"] if i["code"] == "rule_violation"]
    assert viol and all(i["severity"] == "error" for i in viol) and p["script"]["has_errors"]
    r = await client.post(f"/v1/projects/{pid}/script/approve",
                          json={"script_version": p["script_version"], "acknowledged": [f"rule_violation@{viol[0]['path']}"]},
                          headers=headers)  # fmt: skip
    assert r.status_code == 422  # a violation is not something an acknowledgement can wave through
    script = p["script"]["content"]
    for pg in script["pages"]:
        for pn in pg["panels"]:
            if any(c["id"] == "kiro" for c in pn["characters"]):
                pn["rule_exceptions"] = ["apron_always"]  # the user's recorded decision, per panel
    r = await client.patch(f"/v1/projects/{pid}/script", headers=headers,
                           json={"base_version": p["script_version"], "script": script})  # fmt: skip
    assert r.status_code == 200
    p = await wait_checked(client, pid, headers)
    assert p["script"]["consistency"]["status"] == "done" and not p["script"]["has_errors"]
    r = await client.post(
        f"/v1/projects/{pid}/script/approve", json={"script_version": p["script_version"]}, headers=headers
    )
    assert r.status_code == 200, r.text
    await wait_for(client, pid, headers, lambda p: p["status"] == "completed")
    # the excepted rule is not demanded of those panels' images (panel prompts end with this line)
    panel_prompts = [r.prompt for r in fake.control.sync_requests if "speech bubbles, captions" in r.prompt]
    assert panel_prompts and not any("Must be clearly visible: Kiro wears" in pr for pr in panel_prompts)


@pytest.mark.workflow
async def test_uncertain_and_unavailable_checks_need_a_recorded_acknowledgement(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    fake.control.consistency_uncertain = {"apron_always"}
    pid, p = await to_script_gate(client, headers)
    review = p["script"]["review_required"]
    assert review and all(k["key"].startswith("rule_uncertain@") for k in review)
    v = p["script_version"]
    r = await client.post(f"/v1/projects/{pid}/script/approve", json={"script_version": v}, headers=headers)
    assert r.status_code == 422 and r.json()["error"]["fields"][0]["field"] == "acknowledged"
    # the check service fails for the next edit: "not checked" is a review item, never a pass
    fake.control.consistency_uncertain = set()
    fake.control.consistency_errors = 99
    script = p["script"]["content"]
    script["pages"][0]["panels"][0]["action"] = "Kiro offers a steaming bowl with both hands"
    await client.patch(
        f"/v1/projects/{pid}/script", headers=headers, json={"base_version": v, "script": script}
    )
    p = await wait_checked(client, pid, headers)
    assert p["script"]["consistency"]["status"] == "unavailable"
    keys = [k["key"] for k in p["script"]["review_required"]]
    assert [k.split("#")[0] for k in keys] == ["consistency_unavailable@script"]
    r = await client.post(f"/v1/projects/{pid}/script/approve",
                          json={"script_version": p["script_version"], "acknowledged": keys}, headers=headers)  # fmt: skip
    assert r.status_code == 200, r.text
    assert await overrides(pid, "script") == [
        {"key": keys[0], "message": p["script"]["review_required"][0]["message"]}
    ]


@pytest.mark.workflow
async def test_moderation_refusal_cannot_be_acknowledged(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    pid, p = await to_script_gate(client, headers)
    script = p["script"]["content"]
    script["pages"][0]["panels"][0]["action"] = "Kiro waves. [[unsafe]]"
    r = await client.patch(f"/v1/projects/{pid}/script", headers=headers,
                           json={"base_version": p["script_version"], "script": script})  # fmt: skip
    p = await wait_checked(client, pid, headers)
    v = p["script_version"]
    keys = [k["key"] for k in p["script"]["review_required"]]
    r = await client.post(f"/v1/projects/{pid}/script/approve",
                          json={"script_version": v, "acknowledged": keys}, headers=headers)  # fmt: skip
    assert r.status_code == 200
    await wait_for(client, pid, headers, lambda p: p["approved_script_version"] is None)
    again = await client.post(f"/v1/projects/{pid}/script/approve",
                              json={"script_version": v, "acknowledged": keys + ["moderation"]}, headers=headers)  # fmt: skip
    assert again.status_code == 422  # still refused: the content check is not overridable


@pytest.mark.workflow
async def test_flagged_reference_sheet_needs_a_recorded_acknowledgement(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    fake.control.sheet_flags = {"kiro"}
    pid = await start(client, headers)
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_cast_approval"
                       and all((c["sheet_check"] or {}).get("status") not in (None, "pending") for c in p["characters"]))  # fmt: skip
    kiro = next(c for c in p["characters"] if c["slug"] == "kiro")
    assert kiro["sheet_check"]["status"] == "flagged" and "pencil" in kiro["review"]["message"]
    assert next(c for c in p["characters"] if c["slug"] != "kiro")["review"] is None
    r = await client.post(
        f"/v1/projects/{pid}/characters/approve", json={"cast_version": p["cast_version"]}, headers=headers
    )
    assert r.status_code == 422
    r = await client.post(f"/v1/projects/{pid}/characters/approve",
                          json={"cast_version": p["cast_version"], "acknowledged": [kiro["review"]["key"]]}, headers=headers)  # fmt: skip
    assert r.status_code == 200
    assert await overrides(pid, "cast") == [
        {"key": kiro["review"]["key"], "message": kiro["review"]["message"]}
    ]
    # flag only: 3 sheet images per character + 2 locations, no automatic redraw of the flagged sheet
    assert len(fake.control.sync_calls) == 3 * 2 + 2


@pytest.mark.workflow
async def test_unavailable_sheet_review_is_shown_as_not_checked(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    fake.control.sheet_check_errors = 99
    pid = await start(client, headers)
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_cast_approval"
                       and all((c["sheet_check"] or {}).get("status") == "unavailable" for c in p["characters"]))  # fmt: skip
    assert all("wasn't reviewed" in c["review"]["message"] for c in p["characters"])


@pytest.mark.workflow
async def test_world_rules_are_classified_verbatim_without_invented_detail(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    fake.control.no_continuity_rules = True  # like a Bible written before continuity rules existed
    pid = await start(client, headers)
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_cast_approval"
                       and all((c["sheet_check"] or {}).get("status") not in (None, "pending") for c in p["characters"]))  # fmt: skip
    bible = p["bible"]
    assert bible["continuity_rules"] == [] and len(bible["world_rules"]) == 2
    rule_text = bible["world_rules"][1]  # "Kiro always wears the red apron"
    v = p["cast_version"]
    altered = await client.put(f"/v1/projects/{pid}/continuity-rules", headers=headers, json={"cast_version": v, "rules": [
        {"text": rule_text + ", a small one", "applies_to": "character", "target": "kiro",
         "source": "world_rule", "world_rule_index": 1}]})  # fmt: skip
    assert altered.status_code == 422 and "original wording" in altered.text
    bad_target = await client.put(f"/v1/projects/{pid}/continuity-rules", headers=headers, json={"cast_version": v, "rules": [
        {"text": rule_text, "applies_to": "character", "target": "nobody", "source": "world_rule", "world_rule_index": 1}]})  # fmt: skip
    assert bad_target.status_code == 422
    ok = await client.put(f"/v1/projects/{pid}/continuity-rules", headers=headers, json={"cast_version": v, "rules": [
        {"text": rule_text, "applies_to": "character", "target": "kiro", "strength": "must",
         "source": "world_rule", "world_rule_index": 1}]})  # fmt: skip
    assert ok.status_code == 200, ok.text
    p = ok.json()
    (rule,) = p["bible"]["continuity_rules"]
    assert rule["text"] == rule_text and rule["source"] == "world_rule" and p["cast_version"] == v + 1
    assert p["bible"]["world_rules"] == bible["world_rules"]  # the original rules are kept as written
    stale = await client.post(
        f"/v1/projects/{pid}/characters/approve", json={"cast_version": v}, headers=headers
    )
    assert stale.status_code == 409  # the approval covers the rules
    # Kiro's sheet was reviewed before the new rule existed: confirming that is explicit
    kiro = next(c for c in p["characters"] if c["slug"] == "kiro")
    assert kiro["sheet_rules_current"] is False
    assert kiro["review"]["kind"] == "Reference sheet checked against older rules"
    r = await client.post(f"/v1/projects/{pid}/characters/approve", headers=headers,
                          json={"cast_version": v + 1, "acknowledged": [kiro["review"]["key"]]})  # fmt: skip
    assert r.status_code == 200, r.text
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_script_approval")
    locked = await client.put(f"/v1/projects/{pid}/continuity-rules", headers=headers,
                              json={"cast_version": p["cast_version"], "rules": []})  # fmt: skip
    assert locked.status_code == 409  # not while the script is being reviewed
    await client.post(
        f"/v1/projects/{pid}/script/approve", json={"script_version": p["script_version"]}, headers=headers
    )
    await wait_for(client, pid, headers, lambda p: p["status"] == "completed")
    panel_prompts = [r.prompt for r in fake.control.sync_requests if "speech bubbles, captions" in r.prompt]
    assert any(
        f"Must be clearly visible: {rule_text}." in pr for pr in panel_prompts
    )  # reaches panel prompts


@pytest.mark.workflow
async def test_missing_required_element_is_repaired_uncertain_goes_to_review(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    fake.control.rule_missing_attempts = 1  # the first attempt of every panel lacks the apron
    pid, p = await to_script_gate(client, headers)
    await client.post(
        f"/v1/projects/{pid}/script/approve", json={"script_version": p["script_version"]}, headers=headers
    )
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "completed")
    kiro_panels = [
        pn
        for pg in p["pages"]
        for pn in pg["panels"]
        if any(c["id"] == "kiro" for c in pn["spec"]["characters"])
    ]
    assert kiro_panels and all(pn["status"] == "ok" for pn in kiro_panels)  # repaired on the next attempt
    assert any(
        "Draw this clearly" in str(r.prompt) for r in fake.control.sync_requests
    )  # the fix note was used


# ------------------------------------------------------------------ acknowledgements are bound to content
@pytest.mark.workflow
async def test_acknowledgements_are_labelled_and_bound_to_the_version_and_check(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    fake.control.consistency_uncertain = {"apron_always"}
    pid, p = await to_script_gate(client, headers)
    v = p["script_version"]
    items = p["script"]["review_required"]
    assert items and all(i["page"] == 1 and i["panel"] and i["rule_id"] == "apron_always" for i in items)
    assert all(
        i["rule_text"] and i["kind"] and i["issue"] and not i["issue"].startswith("Panel") for i in items
    )
    assert len({(i["page"], i["panel"]) for i in items}) == len(items)  # every item names a different panel
    old_keys = [i["key"] for i in items]
    # another tab saves an edit: a new version with the same kind of findings
    script = p["script"]["content"]
    script["pages"][0]["panels"][0]["mood"] = "warm, a little nervous"
    r = await client.patch(
        f"/v1/projects/{pid}/script", headers=headers, json={"base_version": v, "script": script}
    )
    assert r.status_code == 200
    p = await wait_checked(client, pid, headers)
    new_keys = [i["key"] for i in p["script"]["review_required"]]
    assert new_keys and not set(new_keys) & set(old_keys)  # same findings, new version: new keys
    # the first tab still shows the old version
    r = await client.post(f"/v1/projects/{pid}/script/approve", json={"script_version": v, "acknowledged": old_keys},
                          headers=headers)  # fmt: skip
    assert r.status_code == 409 and r.json()["error"]["code"] == "version_conflict"
    # confirmations made before the edit do not approve the edited version
    r = await client.post(f"/v1/projects/{pid}/script/approve",
                          json={"script_version": v + 1, "acknowledged": old_keys}, headers=headers)  # fmt: skip
    assert r.status_code == 422 and "out of date" in r.json()["error"]["message"]
    r = await client.post(f"/v1/projects/{pid}/script/approve",
                          json={"script_version": v + 1, "acknowledged": new_keys[:-1]}, headers=headers)  # fmt: skip
    assert r.status_code == 422  # every item must be confirmed
    r = await client.post(f"/v1/projects/{pid}/script/approve",
                          json={"script_version": v + 1, "acknowledged": new_keys}, headers=headers)  # fmt: skip
    assert r.status_code == 200
    assert [o["key"] for o in await overrides(pid, "script")] == new_keys


@pytest.mark.workflow
async def test_sheet_confirmation_is_bound_to_the_cast_version(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    fake.control.sheet_flags = {"kiro"}
    pid = await start(client, headers)
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_cast_approval"
                       and all((c["sheet_check"] or {}).get("status") not in (None, "pending") for c in p["characters"]))  # fmt: skip
    kiro = next(c for c in p["characters"] if c["slug"] == "kiro")
    old = kiro["review"]["key"]
    assert kiro["review"]["character"] == "Kiro" and kiro["review"]["kind"] == "Reference sheet flagged"
    rules = [
        {
            k: v
            for k, v in r.items()
            if k in ("id", "text", "applies_to", "target", "strength", "visual", "source")
        }
        for r in p["bible"]["continuity_rules"]
    ][:1]  # the Bible changes: one rule removed
    r = await client.put(f"/v1/projects/{pid}/continuity-rules", headers=headers,
                         json={"cast_version": p["cast_version"], "rules": rules})  # fmt: skip
    assert r.status_code == 200
    p = r.json()
    new = next(c for c in p["characters"] if c["slug"] == "kiro")["review"]["key"]
    assert new != old
    r = await client.post(f"/v1/projects/{pid}/characters/approve",
                          json={"cast_version": p["cast_version"], "acknowledged": [old]}, headers=headers)  # fmt: skip
    assert r.status_code == 422
    r = await client.post(f"/v1/projects/{pid}/characters/approve",
                          json={"cast_version": p["cast_version"], "acknowledged": [new]}, headers=headers)  # fmt: skip
    assert r.status_code == 200
