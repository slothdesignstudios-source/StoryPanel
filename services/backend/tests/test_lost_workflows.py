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
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from storypanel.db.models import Project
from storypanel.db.session import transaction
from storypanel.providers import fake
from storypanel.services.maintenance import approve_paid_resume, find_lost_workflows, recover_lost_workflows
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


async def test_finished_cancelled_and_deleted_comics_are_never_listed_or_revived(
    client: Any, headers: dict[str, str], worker: Any, temporal_env: Any
) -> None:
    """Only running comics are candidates: a completed, a cancelled, a stopped and a deleted comic
    whose workflows are gone are not listed, and --apply leaves them exactly as they were."""
    from tests.conftest import run_to_completion

    done = (await run_to_completion(client, headers))["id"]
    cancelled = await start(client, headers)
    await wait_for(client, cancelled, headers, lambda p: p["status"] == "awaiting_cast_approval")
    assert (await client.post(f"/v1/projects/{cancelled}/cancel", headers=headers)).status_code == 202
    await wait_for(client, cancelled, headers, lambda p: p["status"] == "cancelled")
    deleted = await start(client, headers)
    await wait_for(client, deleted, headers, lambda p: p["status"] == "awaiting_cast_approval")
    assert (await client.delete(f"/v1/projects/{deleted}", headers=headers)).status_code in (200, 202, 204)
    async with transaction() as db:
        rows = (await db.execute(select(Project.id, Project.status, Project.workflow_id, Project.deleted_at).where(
            Project.id.in_([uuid.UUID(x) for x in (done, cancelled, deleted)])))).all()  # fmt: skip
    for _, _, wid, _ in rows:  # every workflow is gone or closed
        try:
            await temporal_env.client.get_workflow_handle(wid).terminate("simulated loss")
        except Exception:  # noqa: BLE001 - already closed
            pass
    before = await state()
    assert await find_lost_workflows(temporal_env.client, min_age=timedelta(0)) == []
    assert await recover_lost_workflows(temporal_env.client, apply=True, min_age=timedelta(0)) == []
    async with transaction() as db:
        after = (await db.execute(select(Project.id, Project.status, Project.workflow_id, Project.deleted_at).where(
            Project.id.in_([uuid.UUID(x) for x in (done, cancelled, deleted)])))).all()  # fmt: skip
    assert sorted(map(tuple, after)) == sorted(map(tuple, rows)) and await state() == before
    assert (await client.post(f"/v1/projects/{cancelled}/resume", headers=headers)).status_code == 409


async def test_recovery_during_drawing_never_charges_twice_or_redraws_finished_panels(
    client: Any, headers: dict[str, str], worker: Any, temporal_env: Any
) -> None:
    """Workflow lost while drawing: panel 1 finished and charged, panel 2's image request cut off.
    The dry run separates the reusable panel, the ambiguous request (may already be billed), the
    absence of retrievable requests (synchronous image API) and the panels to redraw with their
    possible cost. --apply stops the comic and releases only the unused hold, but does NOT make it
    resumable (redrawing is paid); a second --apply finds nothing. Only after the explicit
    per-comic approval can it resume; then only the missing panels are drawn, every delivered
    panel is charged exactly once and the ledger reconciles."""
    from storypanel.db.models import CreditLedger, Panel, ProviderAttempt, ProviderUsage
    from tests.test_credit_paths import to_script_gate

    pid, p = await to_script_gate(client, headers)
    # the first panel's safety check passes; the next image is drawn and saved, its check held open
    fake.control.gate_stage, fake.control.gate, fake.control.gate_skip = "image_safety", asyncio.Event(), 1
    fake.control.gate_entered = asyncio.Event()
    await client.post(f"/v1/projects/{pid}/script/approve", json={"script_version": p["script_version"]},
                      headers=headers)  # fmt: skip
    await asyncio.wait_for(fake.control.gate_entered.wait(), 30)  # a second image saved, its check in flight
    p = await wait_for(client, pid, headers, lambda p: any(pn["status"] in ("ok", "needs_attention")
                                                           for pg in p["pages"] for pn in pg["panels"]))  # fmt: skip
    finished = {pn["id"]: pn["current_version_id"] for pg in p["pages"] for pn in pg["panels"]
                if pn["status"] in ("ok", "needs_attention")}  # fmt: skip
    async with transaction() as db:
        wid = (await db.execute(select(Project.workflow_id).where(Project.id == uuid.UUID(pid)))).scalar_one()
    await temporal_env.client.get_workflow_handle(wid).terminate("simulated: dev server restarted in memory")
    # the held check then times out on the client side: the provider may or may not have billed it
    from storypanel.errors import AppError, ErrorCode

    fake.control.gate_raise = AppError(ErrorCode.TIMEOUT, "simulated client timeout", retryable=True)
    fake.control.gate.set()
    for _ in range(100):  # wait until the held attempt is closed
        async with transaction() as db:
            open_ = (await db.execute(select(ProviderAttempt.id).where(ProviderAttempt.project_id == uuid.UUID(pid),
                     ProviderAttempt.status.in_(["reserved", "submitting"])))).all()  # fmt: skip
        if not open_:
            break
        await asyncio.sleep(0.2)
    fake.control.gate, fake.control.gate_raise = None, None

    (item,) = await find_lost_workflows(temporal_env.client, min_age=timedelta(0))  # dry run: changes nothing
    assert item["resume_from"] == "illustrating" and item["credits_held"] > 0
    assert any("drawn panel(s)" in r for r in item["reusable"])
    assert item["retrievable_requests"] == []  # synchronous image API: nothing to fetch later
    cut_off = [a for a in item["ambiguous_requests"] if a["stage"] == "image_safety"]
    assert cut_off and all(Decimal(a["may_already_be_billed_up_to_usd"]) > 0 for a in cut_off)
    unfinished = {u["panel_id"] for u in item["unfinished_images"]}
    assert unfinished and not unfinished & set(
        finished
    )  # saved image whose check never finished: listed apart
    redo = [r for r in item["needs_redo"] if r.get("panel_id")]
    assert (
        redo
        and not [r for r in redo if r["panel_id"] in finished]
        and unfinished <= {r["panel_id"] for r in redo}
    )
    assert all(Decimal(r["possible_additional_cost_usd"]) > 0 for r in redo)
    assert item["resume_needs_approval"] is True
    assert (await client.get(f"/v1/projects/{pid}", headers=headers)).json()["status"] == "illustrating"
    await recover_lost_workflows(temporal_env.client, apply=True, min_age=timedelta(0))
    _, held = await state()
    assert held == 0  # the unused hold was released; finished panels stay charged
    assert (
        await recover_lost_workflows(temporal_env.client, apply=True, min_age=timedelta(0)) == []
    )  # idempotent
    p = (await client.get(f"/v1/projects/{pid}", headers=headers)).json()
    assert (
        p["status"] == "failed"
        and p["failure"]["code"] == "workflow_lost"
        and "approves" in p["failure"]["message"]
    )
    assert (
        await client.post(f"/v1/projects/{pid}/resume", headers=headers)
    ).status_code == 409  # not without approval
    assert (await approve_paid_resume([uuid.uuid4()]))[0]["approved"] is False  # unknown comics are refused
    (ok,) = await approve_paid_resume([uuid.UUID(pid)])
    assert ok["approved"] and ok["resume_from"] == "illustrating"

    async with transaction() as db:
        attempts_before = {a.id for a in (await db.execute(select(ProviderAttempt).where(
            ProviderAttempt.project_id == uuid.UUID(pid)))).scalars()}  # fmt: skip
    assert (await client.post(f"/v1/projects/{pid}/resume", headers=headers)).status_code == 202
    p = await wait_for(
        client, pid, headers, lambda p: p["status"] in ("completed", "partially_completed"), 120
    )
    panels = [pn for pg in p["pages"] for pn in pg["panels"]]
    assert all(pn["current_version_id"] == finished[pn["id"]] for pn in panels if pn["id"] in finished)
    async with transaction() as db:
        new = (await db.execute(select(ProviderUsage.target_id).join(
            ProviderAttempt, ProviderAttempt.operation_id == ProviderUsage.id).where(
            ProviderAttempt.project_id == uuid.UUID(pid), ProviderAttempt.id.not_in(attempts_before)))).scalars().all()  # fmt: skip
        charges = (await db.execute(select(CreditLedger.dedupe_key).where(
            CreditLedger.reason == "Panel generate"))).scalars().all()  # fmt: skip
        delivered = (await db.execute(select(Panel.id).where(
            Panel.project_id == uuid.UUID(pid), Panel.status.in_(["ok", "needs_attention"])))).scalars().all()  # fmt: skip
    assert new and not [
        t for t in new if t in finished
    ]  # paid calls only for panels not finished before the loss
    charged_panels = [k.split(":")[1] for k in charges]
    assert sorted(charged_panels) == sorted(str(x) for x in delivered)  # each delivered panel charged once
    await state()  # balance = ledger, holds = open reservations
