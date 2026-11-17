"""Failure modes, driven through the API with a real Temporal worker and fake providers."""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select

from storypanel.config import get_settings
from storypanel.db.models import (
    Approval,
    Asset,
    Export,
    Panel,
    PanelVersion,
    ProviderUsage,
    User,
)  # fmt: skip
from storypanel.db.session import transaction
from storypanel.errors import AppError, ErrorCode
from storypanel.pipeline import stages_art
from storypanel.providers import fake
from storypanel.services import credits
from tests.conftest import SPANS, run_to_completion, wait_checked, wait_for

pytestmark = pytest.mark.workflow
STRIP = "A robot and a girl share noodles in the rain, as a 4 panel strip"


def panels_of(p: dict[str, Any]) -> list[dict[str, Any]]:
    return [x for page in p["pages"] for x in page["panels"]]


async def start(client: Any, headers: dict[str, str], prompt: str = STRIP) -> str:
    r = await client.post("/v1/projects", json={"prompt": prompt}, headers=headers)
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def balances() -> tuple[int, int]:
    async with transaction() as db:
        u = (await db.execute(select(User))).scalar_one()
        assert await credits.reconcile(db) == []
        return u.credit_balance, u.credit_reserved


# ------------------------------------------------------------------ QA loop
async def test_exhausted_quality_retries_become_needs_attention(client: Any, headers: dict[str, str],
                                                                worker: Any) -> None:  # fmt: skip
    fake.control.qa_fail_attempts = 99  # every attempt fails the vision checklist
    p = await run_to_completion(client, headers, STRIP)
    assert p["status"] == "completed"
    panels = panels_of(p)
    assert len(panels) == 4 and all(x["status"] == "needs_attention" for x in panels)
    assert all(x["image_url"] and x["qa_problems"] for x in panels)  # visible, with a badge
    async with transaction() as db:
        per_panel = (await db.execute(select(func.count()).select_from(PanelVersion)
                                      .group_by(PanelVersion.panel_id))).scalars().all()  # fmt: skip
        # first attempt + one repair; the same problems survived it, so the second repair is not paid for
        assert per_panel == [2, 2, 2, 2]
        flagged = (
            (await db.execute(select(PanelVersion.qa_scores).where(PanelVersion.attempt == 1)))
            .scalars()
            .all()
        )
        assert all(f.get("repeated_after_repair") for f in flagged)
        notes = (await db.execute(select(PanelVersion.qa_scores))).scalars().first()
        assert notes["fix_notes"]
    assert await balances() == (200 - 8 - 4, 0)  # retries are free; one credit per panel


async def test_retry_recovers_after_one_failed_attempt(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    fake.control.qa_fail_attempts = 1
    p = await run_to_completion(client, headers, STRIP)
    assert all(x["status"] == "ok" for x in panels_of(p))
    async with transaction() as db:
        rows = (await db.execute(select(PanelVersion.attempt, PanelVersion.qa_status)
                                 .order_by(PanelVersion.attempt))).all()  # fmt: skip
        assert sorted(set(rows)) == [(0, "fail"), (1, "pass")]


@pytest.mark.usefixtures("fal_voyage")
async def test_missing_localisation_is_insufficient_identity_evidence(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    """If the judge gives no box for a present character, there is nothing to embed: the
    identity check must fail (and retry), not pass by default."""
    fake.control.unlocalised_attempts = 1
    p = await run_to_completion(client, headers, STRIP)
    assert all(x["status"] == "ok" for x in panels_of(p))
    async with transaction() as db:
        rows = (await db.execute(select(PanelVersion.attempt, PanelVersion.qa_status, PanelVersion.qa_scores)
                                 .order_by(PanelVersion.attempt))).all()  # fmt: skip
    first = [r for r in rows if r[0] == 0 and r[2] and any(c for c in (r[2].get("problems") or []))]
    assert first and all(r[1] == "fail" for r in first)
    assert all(any(pr.startswith("unlocalised:") for pr in r[2]["problems"]) for r in first)
    assert all(r[1] == "pass" for r in rows if r[0] == 1)


async def test_safety_failures_stay_hidden_after_retries(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    """Safety is not a quality problem: running out of retries must never reveal the image."""
    pid = await start(client, headers)
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_cast_approval")
    await client.post(
        f"/v1/projects/{pid}/characters/approve", json={"cast_version": p["cast_version"]}, headers=headers
    )
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_script_approval")
    fake.control.unsafe_attempts = 99  # from here on, every image fails the safety check
    await client.post(
        f"/v1/projects/{pid}/script/approve", json={"script_version": p["script_version"]}, headers=headers
    )
    p = await wait_for(client, pid, headers, lambda p: p["status"] in ("completed", "partially_completed"))
    assert p["status"] == "partially_completed"
    panels = panels_of(p)
    assert all(x["status"] == "blocked" and x["image_url"] is None and x["current_version_id"] is None
               for x in panels)  # fmt: skip
    versions = (await client.get(f"/v1/panels/{panels[0]['id']}/versions", headers=headers)).json()
    assert versions["versions"] == []  # nothing to compare or restore
    async with transaction() as db:
        n_versions = (await db.execute(select(func.count()).select_from(PanelVersion))).scalar_one()
        quarantined = (await db.execute(select(func.count()).select_from(Asset).where(
            Asset.kind == "panel", Asset.quarantined.is_(True)))).scalar_one()  # fmt: skip
        assert n_versions == quarantined == 12
        vid = (await db.execute(select(PanelVersion.id).limit(1))).scalar_one()
    restore = await client.post(f"/v1/panels/{panels[0]['id']}/versions/{vid}/restore", headers=headers)
    assert restore.status_code == 404
    # audit every surface that hands out URLs: project JSON (pages, previews, cast, exports),
    # the dashboard listing and every panel's version history
    async with transaction() as db:
        bad_keys = (
            (await db.execute(select(Asset.storage_key).where(Asset.quarantined.is_(True)))).scalars().all()
        )
    surfaces = [p, (await client.get("/v1/projects", headers=headers)).json()]
    surfaces += [(await client.get(f"/v1/panels/{x['id']}/versions", headers=headers)).json() for x in panels]
    blob = __import__("json").dumps(surfaces)
    assert bad_keys and not any(key in blob for key in bad_keys)
    assert all(pg["preview_url"] for pg in p["pages"])  # previews exist, drawn with empty frames
    edit = await client.post(
        f"/v1/panels/{panels[0]['id']}/edit", json={"instruction": "add rain"}, headers=headers
    )
    assert edit.status_code == 409  # there is no image to edit
    assert await balances() == (200 - 8, 0)  # blocked panels are never charged
    export = await client.post(f"/v1/projects/{pid}/exports", json={"format": "pdf"}, headers=headers)
    assert export.status_code == 409  # nothing exportable


# ------------------------------------------------------------------ moderation
async def test_llm_moderation_blocks_prompt(client: Any, headers: dict[str, str], worker: Any) -> None:
    pid = await start(client, headers, "A gentle story about noodles [[unsafe]] and friendship")
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "blocked")
    assert p["failure"]["code"] == "moderation_blocked" and p["characters"] == []
    assert (await client.post(f"/v1/projects/{pid}/resume", headers=headers)).status_code == 409
    assert await balances() == (200, 0)


# ------------------------------------------------------------------ provider faults
@pytest.mark.usefixtures("fal_voyage")
async def test_throttling_and_malformed_output_are_retried(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    fake.control.malformed_text = 1
    fake.control.errors["image:submit"] = [AppError(ErrorCode.PROVIDER_THROTTLED, "busy")]
    pid = await start(client, headers)
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_cast_approval")
    assert all(c["sheet_status"] == "ready" for c in p["characters"])
    async with transaction() as db:
        failed = (await db.execute(select(ProviderUsage.error_code).where(
            ProviderUsage.status == "failed"))).scalars().all()  # fmt: skip
        assert failed == ["provider_bad_output"]  # the malformed answer is on record
        images = (await db.execute(select(ProviderUsage.status).where(
            ProviderUsage.operation.like("image_%")))).scalars().all()  # fmt: skip
        assert images == ["succeeded"] * 8  # the throttled submission was retried, once each
    assert len(fake.control.submits) == 8


@pytest.mark.usefixtures("fal_voyage")
async def test_ambiguous_timeout_is_reconciled_not_repaid(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    """A timeout after submission resumes the same provider request instead of resubmitting."""
    fake.control.errors["image:fetch"] = [AppError(ErrorCode.PROVIDER_AMBIGUOUS, "timeout", retryable=True)]
    fake.control.pending_fetches = 1  # another request is still running at the provider
    pid = await start(client, headers)
    await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_cast_approval")
    # 2 characters x (portrait, turnaround, expressions) + 2 locations = 8 paid requests
    assert len(fake.control.submits) == 8
    async with transaction() as db:
        rows = (await db.execute(select(ProviderUsage.status, ProviderUsage.provider_request_id).where(
            ProviderUsage.operation.like("image_%")))).all()  # fmt: skip
        assert len(rows) == 8 and all(s == "succeeded" and rid for s, rid in rows)
        assert len({rid for _, rid in rows}) == 8


async def test_outage_fails_cleanly_and_resumes(client: Any, headers: dict[str, str], worker: Any) -> None:
    fake.control.errors["text:bible"] = [AppError(ErrorCode.PROVIDER_ERROR, "down", retryable=False)]
    pid = await start(client, headers)
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "failed")
    assert p["failure"]["code"] == "provider_error" and p["resume_from"] == "planning"
    assert "down" in p["failure"]["message"] and "Traceback" not in str(p["failure"])
    assert await balances() == (200, 0)  # hold released, nothing charged
    r = await client.post(f"/v1/projects/{pid}/resume", headers=headers)
    assert r.status_code == 202, r.text
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_cast_approval")
    assert p["failure"] is None and len(p["characters"]) == 2
    assert await balances() == (200 - 6, 2)


@pytest.mark.usefixtures("fal_voyage")
async def test_partial_failure_then_retry_failed_panels(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    pid = await start(client, headers)
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_cast_approval")
    await client.post(
        f"/v1/projects/{pid}/characters/approve", json={"cast_version": p["cast_version"]}, headers=headers
    )
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_script_approval")
    hard = AppError(ErrorCode.PROVIDER_ERROR, "rejected", retryable=False)
    fake.control.errors["image:submit"] = [hard, hard]
    await client.post(
        f"/v1/projects/{pid}/script/approve", json={"script_version": p["script_version"]}, headers=headers
    )
    p = await wait_for(client, pid, headers, lambda p: p["status"] in ("completed", "partially_completed"))
    assert p["status"] == "partially_completed"
    statuses = sorted(x["status"] for x in panels_of(p))
    assert statuses.count("failed") >= 1 and statuses.count("ok") >= 1
    drawn = statuses.count("ok")
    assert await balances() == (200 - 8 - drawn, 0)  # only delivered panels were charged
    assert all(pg["status"] == "ready" for pg in p["pages"])  # the page is still lettered
    r = await client.post(f"/v1/projects/{pid}/resume", headers=headers)
    assert r.status_code == 202
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "completed")
    assert all(x["status"] == "ok" and x["image_url"] for x in panels_of(p))
    assert await balances() == (200 - 8 - 4, 0)


# ------------------------------------------------------------------ approvals
async def test_approvals_are_scoped_to_exact_versions(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    pid = await start(client, headers)
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_cast_approval")
    v1 = p["cast_version"]
    slug = p["characters"][0]["slug"]
    r = await client.post(f"/v1/projects/{pid}/characters/{slug}/revise", headers=headers,
                          json={"instruction": "shorter hair, add a scarf"})  # fmt: skip
    assert r.status_code == 202
    busy = await client.post(
        f"/v1/projects/{pid}/characters/approve", json={"cast_version": v1}, headers=headers
    )
    assert busy.status_code == 409  # a revision is in flight
    # the revised sheet is reviewed before the cast can be approved: wait for that review too
    p = await wait_for(client, pid, headers, lambda p: p["cast_version"] == v1 + 1 and not any(
        c["revising"] or (c["sheet_check"] or {}).get("status") == "pending" for c in p["characters"]))  # fmt: skip
    assert next(c for c in p["characters"] if c["slug"] == slug)["sheet_version"] == 2
    assert "(revised)" in next(c for c in p["characters"] if c["slug"] == slug)["spec"]["visual"]["outfit"]
    stale = await client.post(
        f"/v1/projects/{pid}/characters/approve", json={"cast_version": v1}, headers=headers
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"
    assert p["status"] == "awaiting_cast_approval"  # nothing downstream was spent
    ok = [
        await client.post(
            f"/v1/projects/{pid}/characters/approve", json={"cast_version": v1 + 1}, headers=headers
        )
        for _ in range(3)
    ]  # duplicate approvals are harmless
    assert [r.status_code for r in ok] == [200, 200, 200]
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_script_approval")
    assert await balances() == (200 - 8 - 1, 0)  # cast + script + one revision

    # --- script: an edit creates a new version and invalidates the approval target
    s1 = p["script_version"]
    script = p["script"]["content"]
    script["pages"][0]["panels"][0]["dialogue"][0]["text"] = " ".join(["word"] * 30)
    bad = await client.patch(
        f"/v1/projects/{pid}/script", headers=headers, json={"base_version": s1, "script": script}
    )
    assert bad.status_code == 200 and bad.json()["has_errors"] and bad.json()["version"] == s1 + 1
    conflict = await client.patch(
        f"/v1/projects/{pid}/script", headers=headers, json={"base_version": s1, "script": script}
    )
    assert conflict.status_code == 409  # concurrent edit from a stale copy
    old = await client.post(
        f"/v1/projects/{pid}/script/approve", json={"script_version": s1}, headers=headers
    )
    assert old.status_code == 409
    invalid = await client.post(
        f"/v1/projects/{pid}/script/approve", json={"script_version": s1 + 1}, headers=headers
    )
    assert invalid.status_code == 422 and invalid.json()["error"]["fields"]
    script["pages"][0]["panels"][0]["dialogue"][0]["text"] = "A much better line."
    fixed = await client.patch(
        f"/v1/projects/{pid}/script", headers=headers, json={"base_version": s1 + 1, "script": script}
    )
    assert fixed.status_code == 200 and not fixed.json()["has_errors"]
    assert await balances() == (200 - 9, 0)  # still nothing held for illustration
    await wait_checked(client, pid, headers)  # the edited version is checked against the Story Bible first
    r = await client.post(
        f"/v1/projects/{pid}/script/approve", json={"script_version": s1 + 2}, headers=headers
    )
    assert r.status_code == 200
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "completed")
    first = panels_of(p)[0]
    assert any(b["text"] == "A much better line." for b in first["balloons"])
    async with transaction() as db:
        rows = (await db.execute(select(Approval.kind, Approval.content_version, Approval.status)
                                 .order_by(Approval.kind, Approval.content_version))).all()  # fmt: skip
        assert rows == [("cast", v1 + 1, "approved"), ("script", s1 + 2, "approved")]


@pytest.mark.usefixtures("fal_voyage")
async def test_insufficient_credits_at_script_gate_spends_nothing(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    pid = await start(client, headers)
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_cast_approval")
    await client.post(
        f"/v1/projects/{pid}/characters/approve", json={"cast_version": p["cast_version"]}, headers=headers
    )
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_script_approval")
    est = (await client.get(f"/v1/projects/{pid}/estimate", headers=headers)).json()
    assert est["step"] == "illustration" and est["credits"] == 4 and est["sufficient"]
    async with transaction() as db:
        uid = (await db.execute(select(User.id))).scalar_one()
        await credits.grant(db, uid, -190, kind="adjust", reason="t", dedupe_key="drain")
    assert not (await client.get(f"/v1/projects/{pid}/estimate", headers=headers)).json()["sufficient"]
    r = await client.post(
        f"/v1/projects/{pid}/script/approve", json={"script_version": p["script_version"]}, headers=headers
    )
    assert r.status_code == 402
    await asyncio.sleep(0.5)
    again = (await client.get(f"/v1/projects/{pid}", headers=headers)).json()
    assert again["status"] == "awaiting_script_approval" and again["pages"] == []
    assert len([s for s in fake.control.submits]) == 8  # no panel image was requested


async def test_expired_approval_can_be_resumed(client: Any, headers: dict[str, str], worker: Any,
                                               monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    monkeypatch.setattr(get_settings(), "approval_ttl_days", 0)
    pid = await start(client, headers)
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "expired")
    assert p["failure"]["code"] == "approval_expired" and p["resume_from"] == "cast_gate"
    assert await balances() == (200 - 6, 0)
    monkeypatch.setattr(get_settings(), "approval_ttl_days", 14)
    assert (await client.post(f"/v1/projects/{pid}/resume", headers=headers)).status_code == 202
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_cast_approval")
    await client.post(
        f"/v1/projects/{pid}/characters/approve", json={"cast_version": p["cast_version"]}, headers=headers
    )
    await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_script_approval")


# ------------------------------------------------------------------ cancellation & durability
async def test_cancellation_releases_credits(client: Any, headers: dict[str, str], worker: Any) -> None:
    pid = await start(client, headers)
    await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_cast_approval")
    assert (await client.post(f"/v1/projects/{pid}/cancel", headers=headers)).status_code == 202
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "cancelled")
    assert p["failure"]["code"] == "cancelled"
    assert await balances() == (200 - 6, 0)  # delivered cast is charged, the rest released
    assert (await client.post(f"/v1/projects/{pid}/cancel", headers=headers)).status_code == 409
    late = await client.post(
        f"/v1/projects/{pid}/characters/approve", json={"cast_version": 1}, headers=headers
    )
    assert late.status_code == 409


async def test_workflow_survives_worker_restart(client: Any, headers: dict[str, str], temporal_env: Any,
                                                app: Any) -> None:  # fmt: skip
    """Stop every worker while the comic waits at a gate; a new worker picks up exactly there."""
    from storypanel.services import outbox
    from storypanel.workflows.client import set_temporal_client
    from storypanel.workflows.worker import build_worker

    queue = f"sp-restart-{uuid.uuid4().hex[:8]}"
    os.environ["SP_TEMPORAL_TASK_QUEUE"] = queue
    get_settings.cache_clear()
    set_temporal_client(temporal_env.client)
    stop = asyncio.Event()
    dispatcher = asyncio.create_task(outbox.run_dispatcher(temporal_env.client, stop, app.state.outbox_wake))
    try:
        async with build_worker(temporal_env.client, queue):
            pid = await start(client, headers)
            p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_cast_approval")
        # no worker is running now: the approval is accepted and durably queued
        r = await client.post(f"/v1/projects/{pid}/characters/approve",
                              json={"cast_version": p["cast_version"]}, headers=headers)  # fmt: skip
        assert r.status_code == 200
        await asyncio.sleep(1.5)
        mid = (await client.get(f"/v1/projects/{pid}", headers=headers)).json()
        assert mid["status"] == "awaiting_cast_approval" and mid["approved_cast_version"] == p["cast_version"]
        submits_before = len(fake.control.submits)
        async with build_worker(temporal_env.client, queue):
            p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_script_approval")
        assert len(fake.control.submits) == submits_before  # finished work was not redone
        assert p["script"]["version"] == 1
    finally:
        stop.set()
        app.state.outbox_wake.set()
        await dispatcher
        get_settings.cache_clear()


async def test_late_result_cannot_overwrite_newer_choice(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    p = await run_to_completion(client, headers, STRIP)
    panel = panels_of(p)[0]
    panel_id = uuid.UUID(panel["id"])
    # a slow job produces a new version...
    made = await stages_art.stage_generate_panel(
        panel_id, request_key="slow-job", attempt=0, kind="regenerate"
    )
    await stages_art.stage_qa_panel(uuid.UUID(made["version_id"]), 0)
    # ...but meanwhile the user explicitly picks the original version again
    r = await client.post(
        f"/v1/panels/{panel_id}/versions/{panel['current_version_id']}/restore", headers=headers
    )
    assert r.status_code == 200
    out = await stages_art.stage_finalize_panel(panel_id, version_ids=[made["version_id"]], kind="regenerate",
                                                request_key="slow-job", intent_seq=0)  # fmt: skip
    assert out["applied"] is False
    async with transaction() as db:
        current = (
            await db.execute(select(Panel.current_version_id).where(Panel.id == panel_id))
        ).scalar_one()
    assert str(current) == panel["current_version_id"]  # the user's choice stands
    versions = (await client.get(f"/v1/panels/{panel_id}/versions", headers=headers)).json()["versions"]
    assert len(versions) == 2  # the late result is kept in history, not lost


async def test_export_uses_an_immutable_snapshot(client: Any, headers: dict[str, str], worker: Any) -> None:
    p = await run_to_completion(client, headers, STRIP)
    pid = p["id"]
    balloon = panels_of(p)[0]["balloons"][0]
    h = {**headers, "Idempotency-Key": "export-1"}
    a = await client.post(f"/v1/projects/{pid}/exports", json={"format": "png"}, headers=h)
    b = await client.post(f"/v1/projects/{pid}/exports", json={"format": "png"}, headers=h)
    assert a.json()["id"] == b.json()["id"]  # duplicate request, one export
    edit = await client.patch(f"/v1/balloons/{balloon['id']}", headers=headers,
                              json={"version": balloon["version"], "text": "Changed during export"})  # fmt: skip
    assert edit.status_code == 200
    p = await wait_for(client, pid, headers, lambda p: p["exports"] and p["exports"][0]["status"] == "ready")
    assert p["exports"][0]["edit_version"] < p["edit_version"]  # UI can show "out of date"
    async with transaction() as db:
        export = (await db.execute(select(Export))).scalar_one()
        texts = [b["text"] for pg in export.revision["pages"] for pn in pg["panels"] for b in pn["balloons"]]
        assert balloon["text"] in texts and "Changed during export" not in texts
        assert export.revision_sha256 == stages_art.revision_sha(export.revision)
        assert (await db.execute(select(func.count()).select_from(Export))).scalar_one() == 1
    # resumable: re-running a page that is already rendered does no work
    assert (await stages_art.stage_export_page(export.id, 0)) == {"skipped": True}


async def test_trace_context_crosses_api_outbox_workflow_and_activity(client: Any, headers: dict[str, str],
                                                                      worker: Any) -> None:  # fmt: skip
    SPANS.clear()
    trace_id = uuid.uuid4().hex
    pid = await start(client, {**headers, "traceparent": f"00-{trace_id}-{'1' * 16}-01"})
    await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_cast_approval")
    names = {s.name for s in SPANS.get_finished_spans() if format(s.context.trace_id, "032x") == trace_id}
    assert any(n.startswith("POST /v1/projects") for n in names), names
    assert "StartWorkflow:ComicWorkflow" in names and "RunWorkflow:ComicWorkflow" in names
    assert "RunActivity:intake" in names and "RunActivity:character_sheet" in names


# ------------------------------------------------------------------ targeted repair, provenance
@pytest.mark.parametrize("mode", ["reference_edit", "inpaint"])
@pytest.mark.usefixtures("fal_voyage")
async def test_local_qa_failure_is_repaired_in_place_not_redrawn(
    client: Any, headers: dict[str, str], worker: Any, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One character failing QA triggers a repair of that region; the rest of the panel is
    pixel-identical to the first attempt. reference_edit conditions the repair on the
    character's approved portrait; inpaint (no reference input) only on the description."""
    import io

    from PIL import Image, ImageChops

    from storypanel.services import repo

    monkeypatch.setattr(get_settings(), "repair_mode", mode)
    fake.control.qa_fail_attempts = 1
    p = await run_to_completion(client, headers, STRIP)
    assert all(x["status"] == "ok" for x in panels_of(p))
    async with transaction() as db:
        versions = (
            (await db.execute(select(PanelVersion).order_by(PanelVersion.panel_id, PanelVersion.attempt)))
            .scalars()
            .all()
        )
        first, second = versions[0], versions[1]
        assert (first.attempt, first.qa_status, second.attempt, second.qa_status) == (0, "fail", 1, "pass")
        assert second.references[0]["role"] == "repair_base" and second.references[0]["mode"] == mode
        if mode == "reference_edit":
            assert second.model == "fake-edit"
            assert second.references[1]["role"] == "character" and second.references[1]["view"] == "portrait"
        else:
            assert second.model == "fake-inpaint"
        repaired = (await db.execute(select(func.count()).select_from(PanelVersion).where(
            PanelVersion.attempt == 1, PanelVersion.references[0]["role"].astext == "repair_base"))).scalar_one()  # fmt: skip
        box = first.qa_scores["repair"]["box"]
        a = Image.open(io.BytesIO(await repo.asset_bytes(db, first.image_asset_id))).convert("RGB")
        b = Image.open(io.BytesIO(await repo.asset_bytes(db, second.image_asset_id))).convert("RGB")
        costs = dict((await db.execute(select(ProviderUsage.model, ProviderUsage.cost_usd).where(
            ProviderUsage.operation.in_(("image_inpaint", "image_edit"))))).all())  # fmt: skip
    assert a.size == b.size
    diff = ImageChops.difference(a, b).getbbox()
    assert diff is not None  # something was repainted...
    w, h = a.size
    x0, y0, x1, y1 = (
        int(box["x"] * w),
        int(box["y"] * h),
        int((box["x"] + box["w"]) * w),
        int((box["y"] + box["h"]) * h),
    )
    assert (
        diff[0] >= x0 and diff[1] >= y0 and diff[2] <= x1 + 1 and diff[3] <= y1 + 1
    )  # ...only inside the mask
    assert ("fake-edit" if mode == "reference_edit" else "fake-inpaint") in costs
    assert repaired == 4  # one repair per panel, no full redraws


async def test_exports_carry_ai_provenance_labels(client: Any, headers: dict[str, str], worker: Any) -> None:
    import io
    import zipfile

    import httpx
    from PIL import Image
    from pypdf import PdfReader

    from storypanel.pipeline.render import DIGITAL_SOURCE_TYPE

    p = await run_to_completion(client, headers, STRIP)

    async def export(fmt: str) -> bytes:
        r = await client.post(f"/v1/projects/{p['id']}/exports", json={"format": fmt}, headers=headers)
        eid = r.json()["id"]
        done = await wait_for(client, p["id"], headers, lambda q: any(
            e["id"] == eid and e["status"] == "ready" for e in q["exports"]))  # fmt: skip
        url = next(e for e in done["exports"] if e["id"] == eid)["download_url"]
        async with httpx.AsyncClient() as http:
            return (await http.get(url)).content

    with Image.open(io.BytesIO(await export("png"))) as img:
        xmp = img.info["XML:com.adobe.xmp"]
        assert (
            DIGITAL_SOURCE_TYPE in xmp and "StoryPanel" in xmp and img.info["Comment"] == "AI-generated image"
        )
    cbz = zipfile.ZipFile(io.BytesIO(await export("cbz")))
    assert DIGITAL_SOURCE_TYPE.encode() in cbz.read("ComicInfo.xml")
    with Image.open(io.BytesIO(cbz.read("page_001.png"))) as img:
        assert DIGITAL_SOURCE_TYPE in img.info["XML:com.adobe.xmp"]
    meta = PdfReader(io.BytesIO(await export("pdf"))).metadata
    assert (
        meta is not None
        and DIGITAL_SOURCE_TYPE in (meta.keywords or "")
        and meta.subject == "AI-generated comic"
    )
