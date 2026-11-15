"""A comic whose workflow was lost (local Temporal restarted in memory) can be found and resumed.

The loss is simulated by terminating the workflow of a comic paused at the cast gate: the
approval is accepted by the API but nothing resumes (the signal has no target). Recovery marks it
stopped at the gate and releases its hold; Resume restarts it at the gate without redrawing
anything, and it then continues normally.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from storypanel.db.models import Project
from storypanel.db.session import transaction
from storypanel.providers import fake
from storypanel.services.maintenance import find_lost_workflows, recover_lost_workflows
from tests.conftest import wait_for
from tests.test_credit_paths import state
from tests.test_story_fidelity import start

pytestmark = pytest.mark.workflow


async def test_a_comic_with_a_lost_workflow_is_found_and_resumes_at_its_gate(
    client: Any, headers: dict[str, str], worker: Any, temporal_env: Any
) -> None:
    pid = await start(client, headers)
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_cast_approval"
                       and all((c["sheet_check"] or {}).get("status") not in (None, "pending") for c in p["characters"]))  # fmt: skip
    healthy = await start(client, headers)  # a second comic whose workflow keeps running
    await wait_for(client, healthy, headers, lambda p: p["status"] == "awaiting_cast_approval")
    assert await find_lost_workflows(temporal_env.client, min_age=timedelta(0)) == []

    async with transaction() as db:
        wid = (await db.execute(select(Project.workflow_id).where(Project.id == uuid.UUID(pid)))).scalar_one()
    await temporal_env.client.get_workflow_handle(wid).terminate("simulated: dev server restarted in memory")
    r = await client.post(f"/v1/projects/{pid}/characters/approve", json={"cast_version": p["cast_version"]},
                          headers=headers)  # fmt: skip
    assert r.status_code == 200
    await asyncio.sleep(2)
    assert (await client.get(f"/v1/projects/{pid}", headers=headers)).json()[
        "status"
    ] == "awaiting_cast_approval"  # stuck

    _, held_before = await state()
    assert held_before > 0
    lost = await find_lost_workflows(temporal_env.client, min_age=timedelta(0))
    assert [(x["project_id"], x["resume_from"]) for x in lost] == [
        (pid, "cast_gate")
    ]  # the healthy one is not listed
    assert await recover_lost_workflows(temporal_env.client, apply=False, min_age=timedelta(0)) == lost
    assert (await client.get(f"/v1/projects/{pid}", headers=headers)).json()[
        "status"
    ] == "awaiting_cast_approval"  # listing changes nothing
    await recover_lost_workflows(temporal_env.client, apply=True, min_age=timedelta(0))
    p = (await client.get(f"/v1/projects/{pid}", headers=headers)).json()
    assert (
        p["status"] == "failed"
        and p["failure"]["code"] == "workflow_lost"
        and "Resume" in p["failure"]["message"]
    )
    healthy_status = (await client.get(f"/v1/projects/{healthy}", headers=headers)).json()["status"]
    assert healthy_status == "awaiting_cast_approval"
    (
        _,
        held_after,
    ) = await state()  # its hold was released (the healthy comic's stays), and the ledger reconciles
    assert held_after < held_before

    drawn = len(fake.control.sync_calls)
    assert (await client.post(f"/v1/projects/{pid}/resume", headers=headers)).status_code == 202
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_cast_approval")
    assert len(fake.control.sync_calls) == drawn  # back at the gate; no sheet redrawn
    r = await client.post(f"/v1/projects/{pid}/characters/approve", json={"cast_version": p["cast_version"]},
                          headers=headers)  # fmt: skip
    assert r.status_code == 200
    await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_script_approval")
