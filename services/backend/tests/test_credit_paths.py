"""Credit-path regressions: every way a comic can move credits, checked against the ledger.

After each scenario the invariant holds: the cached balance equals the sum of the append-only
ledger, the cached hold equals the open reservations, and (once the comic has ended) nothing is
left held. Simulated providers only.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import select

from storypanel.db.models import CreditLedger, User
from storypanel.db.session import transaction
from storypanel.providers import fake
from storypanel.services import credits
from tests.conftest import wait_for

pytestmark = pytest.mark.workflow
STRIP = "A robot and a girl share noodles in the rain, as a 4 panel strip"


async def start(client: Any, headers: dict[str, str]) -> str:
    r = await client.post("/v1/projects", json={"prompt": STRIP}, headers=headers)
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def state() -> tuple[int, int]:
    """(balance, held), after checking both against the ledger and the open reservations."""
    async with transaction() as db:
        u = (await db.execute(select(User))).scalar_one()
        assert await credits.reconcile(db) == []
        return u.credit_balance, u.credit_reserved


async def to_script_gate(client: Any, headers: dict[str, str]) -> tuple[str, dict[str, Any]]:
    pid = await start(client, headers)
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_cast_approval")
    r = await client.post(f"/v1/projects/{pid}/characters/approve",
                          json={"cast_version": p["cast_version"]}, headers=headers)  # fmt: skip
    assert r.status_code == 200
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_script_approval")
    return pid, p


async def patch(client: Any, headers: dict[str, str], pid: str, p: dict[str, Any], action: str) -> int:
    script = p["script"]["content"]
    script["pages"][0]["panels"][0]["action"] = action
    r = await client.patch(f"/v1/projects/{pid}/script", headers=headers,
                           json={"base_version": p["script_version"], "script": script})  # fmt: skip
    assert r.status_code == 200, r.text
    return int(r.json()["version"])


async def test_rejected_approval_releases_its_panel_hold(client: Any, headers: dict[str, str], worker: Any) -> None:
    """A user-edited script that fails moderation after approval must not keep its panel hold:
    otherwise the next approval holds again and the available balance drops twice."""
    pid, p = await to_script_gate(client, headers)
    assert await state() == (200 - 8, 0)
    v = await patch(client, headers, pid, p, "The robot waves. [[unsafe]]")
    r = await client.post(f"/v1/projects/{pid}/script/approve", json={"script_version": v}, headers=headers)
    assert r.status_code == 200
    await wait_for(client, pid, headers, lambda p: p["approved_script_version"] is None
                   and p["status"] == "awaiting_script_approval")  # fmt: skip
    await asyncio.sleep(0.3)
    assert await state() == (200 - 8, 0)  # the rejected version's hold was released
    again = await client.post(f"/v1/projects/{pid}/script/approve", json={"script_version": v}, headers=headers)
    assert again.status_code == 422  # a rejected version cannot be approved again (no hold, no signal)
    assert await state() == (200 - 8, 0)
    p = (await client.get(f"/v1/projects/{pid}", headers=headers)).json()
    v2 = await patch(client, headers, pid, p, "The robot waves at the girl.")
    r = await client.post(f"/v1/projects/{pid}/script/approve", json={"script_version": v2}, headers=headers)
    assert r.status_code == 200
    p = await wait_for(client, pid, headers, lambda p: p["status"] in ("completed", "partially_completed"))
    assert await state() == (200 - 8 - 4, 0)  # 4 panels charged once; nothing left held


async def test_completed_comic_ledger_and_holds_reconcile(client: Any, headers: dict[str, str], worker: Any) -> None:
    pid, p = await to_script_gate(client, headers)
    r = await client.post(f"/v1/projects/{pid}/script/approve",
                          json={"script_version": p["script_version"]}, headers=headers)  # fmt: skip
    assert r.status_code == 200
    assert (await state())[1] == 4  # the illustration hold
    await wait_for(client, pid, headers, lambda p: p["status"] == "completed")
    assert await state() == (200 - 12, 0)


async def test_needs_attention_panels_are_charged_failed_and_blocked_are_not(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    """Documents the current (owner-approved) policy: a delivered panel that still needs
    attention is charged; failed and safety-blocked panels are not."""
    fake.control.qa_fail_attempts = 99  # every attempt fails QA: panels end "needs attention"
    pid, p = await to_script_gate(client, headers)
    await client.post(f"/v1/projects/{pid}/script/approve",
                      json={"script_version": p["script_version"]}, headers=headers)  # fmt: skip
    p = await wait_for(client, pid, headers, lambda p: p["status"] in ("completed", "partially_completed"))
    statuses = [pn["status"] for pg in p["pages"] for pn in pg["panels"]]
    assert statuses == ["needs_attention"] * 4
    assert await state() == (200 - 8 - 4, 0)


async def test_cancel_at_script_gate_releases_nothing_extra(client: Any, headers: dict[str, str], worker: Any) -> None:
    pid, _ = await to_script_gate(client, headers)
    assert (await client.post(f"/v1/projects/{pid}/cancel", headers=headers)).status_code == 202
    await wait_for(client, pid, headers, lambda p: p["status"] == "cancelled")
    assert await state() == (200 - 8, 0)  # cast and script were delivered; no hold remains


async def test_cancel_while_drawing_releases_undelivered_panels(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    fake.control.pending_fetches = 0
    pid, p = await to_script_gate(client, headers)
    fake.control.crashes["sync:openai"] = 0
    await client.post(f"/v1/projects/{pid}/script/approve",
                      json={"script_version": p["script_version"]}, headers=headers)  # fmt: skip
    await wait_for(client, pid, headers, lambda p: p["status"] == "illustrating")
    assert (await client.post(f"/v1/projects/{pid}/cancel", headers=headers)).status_code == 202
    p = await wait_for(client, pid, headers, lambda p: p["status"] in ("cancelled", "completed"))
    await asyncio.sleep(1.0)  # let any in-flight activity finish
    p = (await client.get(f"/v1/projects/{pid}", headers=headers)).json()
    drawn = sum(1 for pg in p["pages"] for pn in pg["panels"] if pn["status"] in ("ok", "needs_attention"))
    async with transaction() as db:
        charged = len((await db.execute(select(CreditLedger.id).where(
            CreditLedger.reason == "Panel generate"))).all())  # fmt: skip
    # Only delivered panels are charged, and nothing stays held. A panel whose result lands
    # after the cancel released the hold is kept but not charged (the hold is gone).
    assert charged <= drawn
    assert await state() == (200 - 8 - charged, 0)


async def test_regenerate_and_edit_charge_once_each(client: Any, headers: dict[str, str], worker: Any) -> None:
    pid, p = await to_script_gate(client, headers)
    await client.post(f"/v1/projects/{pid}/script/approve",
                      json={"script_version": p["script_version"]}, headers=headers)  # fmt: skip
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "completed")
    panel = p["pages"][0]["panels"][0]
    r = await client.post(f"/v1/panels/{panel['id']}/regenerate", headers=headers)
    assert r.status_code == 202
    assert (await state())[1] == 1  # the regenerate hold
    await wait_for(client, pid, headers, lambda p: not p["pages"][0]["panels"][0]["busy"])
    await asyncio.sleep(0.3)
    r = await client.post(f"/v1/panels/{panel['id']}/edit", headers=headers, json={"instruction": "add rain"})
    assert r.status_code == 202
    await wait_for(client, pid, headers, lambda p: not p["pages"][0]["panels"][0]["busy"])
    await asyncio.sleep(0.3)
    assert await state() == (200 - 12 - 2, 0)


async def test_rejected_character_revision_is_released(client: Any, headers: dict[str, str], worker: Any) -> None:
    pid = await start(client, headers)
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_cast_approval")
    slug = p["characters"][0]["slug"]
    r = await client.post(f"/v1/projects/{pid}/characters/{slug}/revise", headers=headers,
                          json={"instruction": "make it [[unsafe]]"})  # fmt: skip
    assert r.status_code == 202
    await wait_for(client, pid, headers, lambda p: not any(c["revising"] for c in p["characters"]))
    await asyncio.sleep(0.3)
    assert await state() == (200 - 6, 2)  # revision hold released; the script part stays held
