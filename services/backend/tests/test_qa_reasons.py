"""What a flagged panel tells the user: the actual reason, with "not checked" kept apart from
confirmed defects, and workflow completion kept apart from quality."""

from __future__ import annotations

from typing import Any

import pytest

from storypanel.pipeline.qa import qa_reasons
from storypanel.providers import fake
from tests.conftest import wait_for


@pytest.mark.parametrize(("status", "problems", "state", "categories"), [
    ("pass", [], "passed", []),
    ("fail", ["identity_visual:kiro"], "issues", ["visual_mismatch"]),
    ("fail", ["missing:kiro", "rule_missing:apron"], "issues", ["missing", "missing"]),
    ("review", ["identity_uncertain:kiro"], "uncertain", ["uncertain"]),
    ("review", ["identity_check_invalid"], "not_checked", ["not_checked"]),
    ("review", ["identity_unverified:kiro", "rule_uncertain:apron"], "not_checked", ["not_checked", "uncertain"]),
    ("pending", [], "not_checked", ["not_checked"]),  # checks never completed: not a defect
    ("fail", ["features:kiro", "identity_check_invalid"], "issues", ["visual_mismatch", "not_checked"]),
])  # fmt: skip
def test_problem_codes_map_to_reasons(
    status: str, problems: list[str], state: str, categories: list[str]
) -> None:
    got_state, reasons = qa_reasons(
        status, problems, names={"kiro": "Kiro"}, rules={"apron": "the red apron"}
    )
    assert got_state == state and [r["category"] for r in reasons] == categories


def test_labels_name_the_character_and_the_rule_and_never_call_an_error_a_defect() -> None:
    _, r = qa_reasons("fail", ["identity_visual:kiro", "rule_missing:apron"], names={"kiro": "Kiro"},
                      rules={"apron": "the red apron"})  # fmt: skip
    assert [x["label"] for x in r] == [
        "Kiro looks different from the approved reference",
        "Not visible: the red apron",
    ]
    _, r = qa_reasons("review", ["identity_check_invalid"])
    assert "unusable answer" in r[0]["label"] and r[0]["category"] == "not_checked"


@pytest.mark.workflow
async def test_api_separates_drawn_from_checked(client: Any, headers: dict[str, str], worker: Any) -> None:
    fake.control.identity_uncertain_attempts = 99  # every identity judgment is "uncertain"
    r = await client.post("/v1/projects", json={"prompt": "A robot and a girl share noodles in the rain, as a 4 panel strip"},
                          headers=headers)  # fmt: skip
    pid = r.json()["id"]
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_cast_approval")
    await client.post(
        f"/v1/projects/{pid}/characters/approve", json={"cast_version": p["cast_version"]}, headers=headers
    )
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_script_approval")
    await client.post(
        f"/v1/projects/{pid}/script/approve", json={"script_version": p["script_version"]}, headers=headers
    )
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "completed")
    panels = [pn for pg in p["pages"] for pn in pg["panels"]]
    assert all(pn["status"] == "needs_attention" and pn["qa_state"] == "uncertain" for pn in panels)
    assert all(r["category"] == "uncertain" for pn in panels for r in pn["qa_reasons"])
    assert p["panel_counts"] == {"needs_attention": 4}  # the workflow finished; the checks did not all pass
    listed = (await client.get("/v1/projects", headers=headers)).json()["projects"][0]
    assert listed["panel_counts"] == {"needs_attention": 4}
    versions = (await client.get(f"/v1/panels/{panels[0]['id']}/versions", headers=headers)).json()[
        "versions"
    ]
    assert versions and versions[0]["qa_state"] == "uncertain"
