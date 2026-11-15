"""Cancellation race, made deterministic.

Observed: a panel's work was still running when the comic was cancelled; its result landed after
the cancellation and was kept (owner decision: kept, not charged; prices unchanged).

Here the order is fixed by a gate in the simulated provider instead of by timing: panel 1's
image is drawn, its safety check is held open, the user cancels, the workflow records the
cancellation, and only then do the late steps run, in the order the race produced. Checks:
no new paid request starts once the cancel is recorded; the cost of the request in flight
stays counted; the late result is kept without a charge and cannot revive the comic; after an
account purge a late result writes nothing.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import select, text

from storypanel.db.models import CreditLedger, Panel, PanelVersion, Project, ProviderAttempt
from storypanel.db.session import transaction
from storypanel.errors import AppError
from storypanel.pipeline import stages_art
from storypanel.providers import fake
from tests.conftest import wait_for
from tests.test_credit_paths import state, to_script_gate

pytestmark = pytest.mark.workflow


async def _attempts(pid: str) -> list[ProviderAttempt]:
    async with transaction() as db:
        return list((await db.execute(select(ProviderAttempt).where(
            ProviderAttempt.project_id == uuid.UUID(pid)).order_by(ProviderAttempt.created_at))).scalars())  # fmt: skip


async def _settled(pid: str, timeout: float = 15.0) -> list[ProviderAttempt]:
    """Wait until no attempt of this comic is still reserved or being submitted."""
    end = asyncio.get_event_loop().time() + timeout
    while True:
        rows = await _attempts(pid)
        if not [a for a in rows if a.status in ("reserved", "submitting")]:
            return rows
        assert asyncio.get_event_loop().time() < end, [(a.stage, a.status) for a in rows]
        await asyncio.sleep(0.2)


async def test_late_panel_after_a_recorded_cancel_is_kept_uncharged_and_starts_no_paid_work(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    pid, p = await to_script_gate(client, headers)
    fake.control.gate_stage, fake.control.gate = "image_safety", asyncio.Event()
    fake.control.gate_entered = asyncio.Event()
    await client.post(f"/v1/projects/{pid}/script/approve", json={"script_version": p["script_version"]},
                      headers=headers)  # fmt: skip
    await asyncio.wait_for(fake.control.gate_entered.wait(), 30)  # an image is drawn; its check is in flight

    assert (await client.post(f"/v1/projects/{pid}/cancel", headers=headers)).status_code == 202
    async with transaction() as db:
        # The cancel is committed: any reservation transaction that starts after this instant sees it.
        recorded = (await db.execute(text("select clock_timestamp()"))).scalar_one()
        assert (await db.execute(select(Project.cancel_requested_at).where(
            Project.id == uuid.UUID(pid)))).scalar_one() is not None  # fmt: skip
    await wait_for(client, pid, headers, lambda p: p["status"] == "cancelled")
    fake.control.gate.set()
    rows = await _settled(pid)

    # 1. No paid request was reserved after the cancellation was recorded.
    assert [a for a in rows if a.created_at > recorded] == [], [(a.stage, a.status) for a in rows]
    # 2. Every request made, including the one in flight, keeps a counted cost and a state
    #    (a call cut off mid-way stays "unresolved" for reconciliation, never zero or lost).
    assert all(a.cost_counted_usd is not None and a.cost_counted_usd > 0 for a in rows)
    drawn = [a for a in rows if a.stage == "panel_generate"]
    assert drawn and all(a.status == "succeeded" for a in drawn)

    # 3. The race: the drawn panel is finalized after the cancel (by the workflow, which
    #    finalizes paid work when its check is cancelled, or by a delayed finalize; both orders
    #    are covered by finalizing any leftover version here).
    async with transaction() as db:
        made = (await db.execute(select(PanelVersion).join(Panel, Panel.id == PanelVersion.panel_id).where(
            Panel.project_id == uuid.UUID(pid)))).scalars().all()  # fmt: skip
    assert made  # the image drawn before the cancel
    unchecked = [v for v in made if v.qa_status == "pending"]
    assert unchecked  # the check held at the gate never finished
    with pytest.raises(AppError, match="cancelled"):  # finishing it now would be new paid work: refused
        await stages_art.stage_qa_panel(unchecked[0].id, 0)
    for v in made:
        await stages_art.stage_finalize_panel(v.panel_id, version_ids=[str(v.id)], kind="generate",
                                              request_key="initial", intent_seq=0)  # fmt: skip
    async with transaction() as db:
        charges = (
            (await db.execute(select(CreditLedger).where(CreditLedger.reason == "Panel generate")))
            .scalars()
            .all()
        )
        project = (await db.execute(select(Project).where(Project.id == uuid.UUID(pid)))).scalar_one()
        kept = (
            (await db.execute(select(Panel).where(Panel.id.in_([v.panel_id for v in made])))).scalars().all()
        )
        other = (await db.execute(select(Panel.id).where(Panel.project_id == uuid.UUID(pid),
                                                         Panel.current_version_id.is_(None)))).scalars().first()  # fmt: skip
    assert all(k.current_version_id is not None for k in kept)  # kept (owner decision)
    assert charges == []  # never charged, in either order
    assert project.status == "cancelled"
    assert await state() == (200 - 8, 0)  # plan charge only; nothing held; ledger reconciles

    # 4. Late steps cannot start paid work or revive the comic.
    assert other is not None
    with pytest.raises(AppError, match="cancelled"):
        await stages_art.stage_generate_panel(other, request_key="initial", attempt=0)
    assert (await stages_art.stage_finish(uuid.UUID(pid)))["status"] == "cancelled"
    assert len(await _attempts(pid)) == len(rows)  # nothing new reserved by any of these
    p = (await client.get(f"/v1/projects/{pid}", headers=headers)).json()
    assert p["status"] == "cancelled"
    assert (await client.post(f"/v1/projects/{pid}/resume", headers=headers)).status_code == 409


async def test_a_late_result_after_an_account_purge_writes_nothing(
    client: Any, headers: dict[str, str], worker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from storypanel.config import get_settings
    from storypanel.services import deletion
    from tests.test_deletion import _counts, _drive, _uid

    pid, p = await to_script_gate(client, headers)
    fake.control.gate_stage, fake.control.gate = "image_safety", asyncio.Event()
    fake.control.gate_entered = asyncio.Event()
    await client.post(f"/v1/projects/{pid}/script/approve", json={"script_version": p["script_version"]},
                      headers=headers)  # fmt: skip
    await asyncio.wait_for(fake.control.gate_entered.wait(), 30)
    async with transaction() as db:
        late = (await db.execute(select(PanelVersion.id, PanelVersion.panel_id).join(
            Panel, Panel.id == PanelVersion.panel_id).where(Panel.project_id == uuid.UUID(pid)))).first()  # fmt: skip
    assert late is not None
    uid = await _uid(client, headers)
    assert (await client.delete("/v1/me", headers=headers)).status_code == 204
    monkeypatch.setattr(get_settings(), "deletion_drain_seconds", 0)
    fake.control.gate.set()
    await _drive(uid)
    assert (await deletion.advance(uid)) == "completed"
    with pytest.raises(Exception):  # the panel is gone: nothing is re-created
        await stages_art.stage_finalize_panel(late.panel_id, version_ids=[str(late.id)], kind="generate",
                                              request_key="initial", intent_seq=0)  # fmt: skip
    async with transaction() as db:
        left = (
            await db.execute(text("select count(*) from credit_ledger where reason = 'Panel generate'"))
        ).scalar_one()
    c = await _counts()
    # nothing re-created, and the late results were not charged (the ledger itself is kept)
    assert c["projects"] == 0 and c["assets"] == 0 and left == 0
