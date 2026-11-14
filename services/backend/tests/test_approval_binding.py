"""Approvals are bound to the exact persisted versions, on the server.

The API locks the project row for saves and approvals, so they serialise. The workflow checks an
approval and then leaves the gate in a separate step; that step re-checks the approval under the
same lock, so a save landing in between sends the comic back to the gate instead of scripting or
drawing an unapproved version.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select

from storypanel.db.models import CreditReservation, Page, User
from storypanel.db.session import transaction
from storypanel.pipeline import stages_art, stages_plan
from storypanel.services import credits
from tests.conftest import wait_for

pytestmark = pytest.mark.workflow
STRIP = "A robot and a girl share noodles in the rain, as a 4 panel strip"


async def start(client: Any, headers: dict[str, str]) -> str:
    r = await client.post("/v1/projects", json={"prompt": STRIP}, headers=headers)
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def held() -> int:
    async with transaction() as db:
        assert await credits.reconcile(db) == []
        return int((await db.execute(select(User.credit_reserved))).scalar_one())


async def test_bible_saved_after_the_approval_check_sends_the_comic_back_to_the_gate(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    pid = await start(client, headers)
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_cast_approval")
    v = p["cast_version"]
    async with transaction() as db:  # the approval exists and was checked...
        await stages_plan.record_approval(
            db, uuid.UUID(pid), "cast", v, (await db.execute(select(User.id))).scalar_one()
        )
    r = await client.patch(f"/v1/projects/{pid}/bible", headers=headers,
                           json={"cast_version": v, "title": "A different title"})  # fmt: skip
    assert r.status_code == 200  # ...then a save lands before the transition
    out = await stages_plan.stage_begin_scripting(uuid.UUID(pid), v)
    assert out == {"stale": True}
    p = (await client.get(f"/v1/projects/{pid}", headers=headers)).json()
    assert p["status"] == "awaiting_cast_approval" and p["cast_version"] == v + 1


async def test_script_saved_after_the_approval_check_is_not_drawn_and_its_hold_is_released(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    pid = await start(client, headers)
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_cast_approval")
    await client.post(
        f"/v1/projects/{pid}/characters/approve", json={"cast_version": p["cast_version"]}, headers=headers
    )
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_script_approval")
    s = p["script_version"]
    async with transaction() as db:
        uid = (await db.execute(select(User.id))).scalar_one()
        await stages_plan.record_approval(db, uuid.UUID(pid), "script", s, uid)
        await credits.reserve(db, uid, 4, purpose="panels", project_id=uuid.UUID(pid),
                              idempotency_key=f"panels:{pid}:{s}")  # fmt: skip
    assert await held() == 4
    script = p["script"]["content"]
    script["pages"][0]["panels"][0]["action"] = "Edited after the approval was checked."
    r = await client.patch(
        f"/v1/projects/{pid}/script", headers=headers, json={"base_version": s, "script": script}
    )
    assert r.status_code == 200
    out = await stages_art.stage_plan_pages(uuid.UUID(pid), s)
    assert out["stale"] is True
    async with transaction() as db:
        assert (await db.execute(select(func.count()).select_from(Page).where(
            Page.project_id == uuid.UUID(pid)))).scalar_one() == 0  # fmt: skip
        status = (await db.execute(select(CreditReservation.status).where(
            CreditReservation.idempotency_key == f"panels:{pid}:{s}"))).scalar_one()  # fmt: skip
    assert status == "closed" and await held() == 0


async def test_save_racing_an_approval_never_scripts_an_unapproved_bible(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    """Approve and save at the same moment (two tabs). Whatever order the server takes them in,
    the comic only leaves the gate with an approval of the version that is current then."""
    pid = await start(client, headers)
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_cast_approval")
    v = p["cast_version"]
    approve, save = await asyncio.gather(
        client.post(f"/v1/projects/{pid}/characters/approve", json={"cast_version": v}, headers=headers),
        client.patch(
            f"/v1/projects/{pid}/bible", headers=headers, json={"cast_version": v, "title": "Tab B title"}
        ),
    )
    assert save.status_code in (200, 409) and approve.status_code in (200, 409)
    await asyncio.sleep(2.0)
    p = (await client.get(f"/v1/projects/{pid}", headers=headers)).json()
    if save.status_code == 200:
        # the save won (or followed the approval): the old approval is stale, nothing proceeded
        assert p["status"] == "awaiting_cast_approval" and p["cast_version"] == v + 1
        assert p["approved_cast_version"] is None
        r = await client.post(
            f"/v1/projects/{pid}/characters/approve", json={"cast_version": v + 1}, headers=headers
        )
        assert r.status_code == 200
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_script_approval")
    assert p["approved_cast_version"] == p["cast_version"]


async def test_approval_from_a_stale_tab_is_rejected(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    pid = await start(client, headers)
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_cast_approval")
    v = p["cast_version"]
    r = await client.patch(
        f"/v1/projects/{pid}/bible", headers=headers, json={"cast_version": v, "tone": "wistful"}
    )
    assert r.status_code == 200  # tab B saved
    stale = await client.post(
        f"/v1/projects/{pid}/characters/approve", json={"cast_version": v}, headers=headers
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"  # tab A
    await asyncio.sleep(0.5)
    p = (await client.get(f"/v1/projects/{pid}", headers=headers)).json()
    assert p["status"] == "awaiting_cast_approval" and p["bible"]["tone"] == "wistful"


async def test_script_save_right_after_approval_holds_once_and_draws_the_new_version(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    pid = await start(client, headers)
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_cast_approval")
    await client.post(
        f"/v1/projects/{pid}/characters/approve", json={"cast_version": p["cast_version"]}, headers=headers
    )
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_script_approval")
    s = p["script_version"]
    script = p["script"]["content"]
    script["pages"][0]["panels"][0]["dialogue"][0]["text"] = "The newest line."
    approve, save = await asyncio.gather(
        client.post(f"/v1/projects/{pid}/script/approve", json={"script_version": s}, headers=headers),
        client.patch(
            f"/v1/projects/{pid}/script", headers=headers, json={"base_version": s, "script": script}
        ),
    )
    await asyncio.sleep(2.0)
    p = (await client.get(f"/v1/projects/{pid}", headers=headers)).json()
    if p["status"] == "awaiting_script_approval":  # the save made the approval stale
        assert await held() == 0  # its hold was released
        r = await client.post(f"/v1/projects/{pid}/script/approve",
                              json={"script_version": p["script_version"]}, headers=headers)  # fmt: skip
        assert r.status_code == 200
    else:
        assert save.status_code == 409  # the approval took the lock first; the save was refused
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "completed")
    assert p["approved_script_version"] == p["script_version"]
    async with transaction() as db:
        assert await credits.reconcile(db) == []
        bal, res = (await db.execute(select(User.credit_balance, User.credit_reserved))).one()
    assert (bal, res) == (200 - 12, 0)  # illustration held and charged once
