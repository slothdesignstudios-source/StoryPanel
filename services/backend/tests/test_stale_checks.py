"""Continuity-rule changes mark older check results as stale; they are never shown as current.

Every reference-sheet review and panel check stores a fingerprint of the exact rules it judged
against. When the rules change, results made against other rules report `*_rules_current: false`;
a sheet awaiting cast approval then needs an explicit, recorded confirmation. Results stored before
fingerprints existed report `null` (unknown). Simulated providers.
"""

from __future__ import annotations

from typing import Any

import pytest

from storypanel.db.models import Character
from storypanel.domain.schemas import ContinuityRule
from storypanel.pipeline import consistency
from storypanel.services.projects import panel_rules_current, sheet_rules_current
from tests.conftest import wait_for
from tests.test_story_fidelity import overrides, start, to_script_gate

SCARF = {"text": "Kiro always wears a blue scarf", "applies_to": "character", "target": "kiro",
         "strength": "must", "visual": True, "source": "user"}  # fmt: skip


def _rules(p: dict[str, Any]) -> list[dict[str, Any]]:
    keep = ("id", "text", "applies_to", "target", "strength", "visual", "source", "world_rule_index")
    return [{k: v for k, v in r.items() if k in keep} for r in p["bible"]["continuity_rules"]]


def test_fingerprint_changes_with_wording_scope_and_strength_only() -> None:
    a = ContinuityRule(id="apron", text="Kiro wears the red apron", applies_to="character", target="kiro")
    fp = consistency.rules_fingerprint
    assert fp([a]) == fp([a.model_copy()]) and fp([a, a.model_copy(update={"id": "b"})]) == fp(
        [a.model_copy(update={"id": "b"}), a]
    )  # order does not matter
    for change in (
        {"text": "Kiro wears a blue apron"},
        {"target": "mei"},
        {"strength": "prefer"},
        {"visual": False},
    ):
        assert fp([a.model_copy(update=change)]) != fp([a]), change
    assert fp([]) != fp([a])


def test_results_without_a_fingerprint_are_unknown_not_current() -> None:
    c = Character(slug="kiro", name="Kiro", spec={"id": "kiro"}, sheet_version=1,
                  sheet_check={"sheet_version": 1, "status": "ok", "checked_at": "2026-11-01T00:00:00+00:00"})  # fmt: skip
    assert sheet_rules_current(c, []) is None
    spec = {"shot": "medium", "angle": "eye-level", "location_id": "stall", "time_of_day": "night",
            "characters": [{"id": "kiro", "position": "left", "pose": "p", "expression": "e"}],
            "action": "a", "mood": "m"}  # fmt: skip
    assert panel_rules_current({"identity": 0.9}, spec, []) is None
    assert panel_rules_current(None, spec, []) is None


@pytest.mark.workflow
async def test_a_rule_change_marks_the_sheet_review_stale_and_needs_confirmation(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    pid = await start(client, headers)
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_cast_approval"
                       and all((c["sheet_check"] or {}).get("status") not in (None, "pending") for c in p["characters"]))  # fmt: skip
    assert all(c["sheet_rules_current"] is True and c["review"] is None for c in p["characters"])
    r = await client.put(f"/v1/projects/{pid}/continuity-rules", headers=headers,
                         json={"cast_version": p["cast_version"], "rules": [*_rules(p), SCARF]})  # fmt: skip
    assert r.status_code == 200, r.text
    p = r.json()
    by = {c["slug"]: c for c in p["characters"]}
    assert by["kiro"]["sheet_rules_current"] is False
    item = by["kiro"]["review"]
    assert item["kind"] == "Reference sheet checked against older rules" and item["character"] == "Kiro"
    others = [c for s, c in by.items() if s != "kiro"]
    assert others and all(
        c["sheet_rules_current"] is True and c["review"] is None for c in others
    )  # unaffected
    r = await client.post(f"/v1/projects/{pid}/characters/approve", headers=headers,
                          json={"cast_version": p["cast_version"]})  # fmt: skip
    assert r.status_code == 422  # a stale review is never approved silently
    r = await client.post(f"/v1/projects/{pid}/characters/approve", headers=headers,
                          json={"cast_version": p["cast_version"], "acknowledged": [item["key"]]})  # fmt: skip
    assert r.status_code == 200, r.text
    assert [o["key"] for o in await overrides(pid, "cast")] == [item["key"]]


@pytest.mark.workflow
async def test_a_rule_change_after_drawing_marks_affected_panel_checks_stale(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    pid, p = await to_script_gate(client, headers)
    await client.post(f"/v1/projects/{pid}/script/approve", json={"script_version": p["script_version"]},
                      headers=headers)  # fmt: skip
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "completed")
    panels = [pn for pg in p["pages"] for pn in pg["panels"]]
    assert all(pn["qa_rules_current"] is True for pn in panels)
    r = await client.put(f"/v1/projects/{pid}/continuity-rules", headers=headers,
                         json={"cast_version": p["cast_version"], "rules": [*_rules(p), SCARF]})  # fmt: skip
    assert r.status_code == 200, r.text
    panels = [pn for pg in r.json()["pages"] for pn in pg["panels"]]
    with_kiro = [pn for pn in panels if any(c["id"] == "kiro" for c in pn["spec"]["characters"])]
    without = [pn for pn in panels if pn not in with_kiro]
    assert with_kiro and all(pn["qa_rules_current"] is False for pn in with_kiro)
    assert all(pn["qa_rules_current"] is True for pn in without)
    # restoring the old rules makes the old results current again (they were judged against them)
    r = await client.put(f"/v1/projects/{pid}/continuity-rules", headers=headers,
                         json={"cast_version": r.json()["cast_version"], "rules": _rules(p)})  # fmt: skip
    assert all(pn["qa_rules_current"] is True for pg in r.json()["pages"] for pn in pg["panels"])
