"""Complete comic workflows with only Anthropic, OpenAI and Gemini enabled (the default; simulated
providers): provider choice and persistence, reference-image propagation, Gemini visual identity QA
outcomes, native-resolution exports and account deletion. fal and Voyage are disabled throughout."""

from __future__ import annotations

import asyncio
import io
import uuid
from typing import Any

import pytest
from sqlalchemy import select, text

from storypanel.config import get_settings
from storypanel.db.models import Asset, CharacterRef, PanelVersion, Project, ProviderAttempt
from storypanel.db.session import transaction
from storypanel.errors import AppError, ErrorCode
from storypanel.providers import fake
from storypanel.providers.registry import get_providers, set_providers
from tests.conftest import wait_for

STRIP = "A lighthouse keeper and a cheeky seagull argue over a sandwich, as a 3 panel strip"


async def _create(client: Any, headers: dict[str, str], **body: Any) -> str:
    r = await client.post("/v1/projects", json={"prompt": STRIP, **body}, headers=headers)
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _through_cast(client: Any, headers: dict[str, str], pid: str) -> dict[str, Any]:
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_cast_approval")
    await client.post(
        f"/v1/projects/{pid}/characters/approve", json={"cast_version": p["cast_version"]}, headers=headers
    )
    return await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_script_approval")


async def _finish(client: Any, headers: dict[str, str], pid: str, p: dict[str, Any]) -> dict[str, Any]:
    await client.post(
        f"/v1/projects/{pid}/script/approve", json={"script_version": p["script_version"]}, headers=headers
    )
    return await wait_for(client, pid, headers, lambda p: p["status"] in ("completed", "partially_completed", "failed"),
                          timeout=120)  # fmt: skip


def _panels(p: dict[str, Any]) -> list[dict[str, Any]]:
    return [pn for pg in p["pages"] for pn in pg["panels"]]


async def _image_attempts() -> list[ProviderAttempt]:
    async with transaction() as db:
        return list((await db.execute(select(ProviderAttempt).where(ProviderAttempt.operation.like("image%"))
                                      .order_by(ProviderAttempt.created_at))).scalars())  # fmt: skip


async def _export(client: Any, headers: dict[str, str], pid: str, fmt: str) -> dict[str, Any]:
    r = await client.post(f"/v1/projects/{pid}/exports", json={"format": fmt},
                          headers={**headers, "Idempotency-Key": f"{fmt}-{uuid.uuid4().hex}"})  # fmt: skip
    assert r.status_code in (200, 202), r.text
    eid = r.json()["id"]
    p = await wait_for(client, pid, headers, lambda q: any(e["id"] == eid and e["status"] in ("ready", "failed")
                                                           for e in q["exports"]), timeout=60)  # fmt: skip
    return next(e for e in p["exports"] if e["id"] == eid)


@pytest.mark.parametrize("choice", ["openai", "gemini"])
async def test_complete_comic_without_fal_or_voyage(client: Any, headers: dict[str, str], worker: Any, choice: str,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    from storypanel.providers import metering

    async def no_external(*a: Any, **k: Any) -> Any:
        raise AssertionError("an external upscaler / embedding service was called")

    monkeypatch.setattr(metering, "upscale_image", no_external)
    monkeypatch.setattr(metering, "embed_images", no_external)
    monkeypatch.setattr(metering, "generate_image", no_external)  # the fal queue path
    s = get_settings()
    assert not s.fal_enabled and not s.voyage_enabled
    pid = await _create(client, headers, **({"image_provider": choice} if choice == "gemini" else {}))
    p = await _finish(client, headers, pid, await _through_cast(client, headers, pid))
    assert p["status"] == "completed" and len(_panels(p)) == 3
    assert (p["image_provider"], p["image_model"]) == (choice, f"fake-{choice}-image")
    # every image of the comic came from the chosen provider; the fal-style queue was never used
    assert {c[0] for c in fake.control.sync_calls} == {choice}
    assert fake.control.submits == [] and fake.control.fetches == [] and fake.control.cancels == []
    providers = get_providers()
    assert providers.image is not None  # the fal adapter's fake exists in fake mode, but nothing called it
    assert providers.embedding is None and providers.upscale is None
    assert {a.model for a in await _image_attempts()} == {f"fake-{choice}-image"}
    async with transaction() as db:  # no placeholder vectors: the embedding column stays empty
        assert (
            await db.execute(select(CharacterRef.embedding).where(CharacterRef.embedding.is_not(None)))
        ).all() == []
        scores = list((await db.execute(select(PanelVersion.qa_scores))).scalars())
    assert all(
        sc["identity_visual"]["method"] == "gemini_visual" and "identity_similarity" not in sc
        for sc in scores
    )
    assert len(fake.control.identity_calls) == 3 and all(n >= 2 for _, n in fake.control.identity_calls)
    # one instruction edit, then every export, natively, with no upscaler
    first = _panels(p)[0]
    r = await client.post(f"/v1/panels/{first['id']}/edit", json={"instruction": "Make the seagull hold the sandwich."},
                          headers={**headers, "Idempotency-Key": uuid.uuid4().hex})  # fmt: skip
    assert r.status_code == 202, r.text
    await wait_for(client, pid, headers, lambda q: any(pn["id"] == first["id"] and pn["current_version_id"] != first["current_version_id"]
                                                       and not pn.get("busy") for pn in _panels(q)), timeout=60)  # fmt: skip
    edit = fake.control.sync_requests[-1]
    assert edit.mode == "edit" and edit.references and fake.control.sync_calls[-1][0] == choice
    for fmt in ("pdf", "png", "cbz"):
        e = await _export(client, headers, pid, fmt)
        assert e["status"] == "ready", e
        q = e["print_quality"]
        assert q["method"] == "local_resample" and len(q["panels"]) == 3 and q["min_dpi"] > 0
    assert fake.control.models_used == []  # no upscale (or queue) submission at all


async def test_the_comic_keeps_its_provider_across_restarts_and_configuration_changes(
    client: Any, headers: dict[str, str], worker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = await _create(client, headers)  # default: OpenAI
    p = await _through_cast(client, headers, pid)
    monkeypatch.setattr(get_settings(), "image_provider_default", "gemini")  # operator switches the default
    set_providers(None)  # and the worker's provider set is rebuilt (restart)
    p = await _finish(client, headers, pid, p)
    assert p["status"] == "completed" and p["image_provider"] == "openai"
    assert {c[0] for c in fake.control.sync_calls} == {"openai"}  # sheets, panels: never Gemini
    new = await _create(client, headers)
    async with transaction() as db:
        assert (
            await db.execute(select(Project.image_provider).where(Project.id == uuid.UUID(new)))
        ).scalar_one() == "gemini"


async def test_unavailable_providers_are_refused_with_a_reason(client: Any, headers: dict[str, str],
                                                               monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    meta = (await client.get("/v1/meta")).json()
    opts = {o["id"]: o for o in meta["image_providers"]}
    assert (
        meta["default_image_provider"] == "openai"
        and opts["openai"]["available"]
        and opts["gemini"]["available"]
    )
    assert not opts["fal"]["available"] and "disabled" in opts["fal"]["reason"]
    r = await client.post("/v1/projects", json={"prompt": STRIP, "image_provider": "fal"}, headers=headers)
    assert r.status_code == 422 and "fal is disabled" in r.text
    monkeypatch.setattr(get_settings(), "image_providers", ["openai"])
    r = await client.post("/v1/projects", json={"prompt": STRIP, "image_provider": "gemini"}, headers=headers)
    assert r.status_code == 422 and "not enabled" in r.text


async def test_reference_images_are_carried_into_every_panel_request(client: Any, headers: dict[str, str],
                                                                     worker: Any) -> None:  # fmt: skip
    pid = await _create(client, headers)
    p = await _finish(client, headers, pid, await _through_cast(client, headers, pid))
    assert p["status"] == "completed"
    panel_reqs = [r for r in fake.control.sync_requests if r.inputs.get("kind") == "panel"]
    assert len(panel_reqs) == 3
    for r in panel_reqs:  # approved character sheets (and location / previous panel) go with the prompt
        assert 1 <= len(r.references) <= get_settings().image_max_references
    sheet = [r for r in fake.control.sync_requests if r.inputs.get("kind") in ("turnaround", "expressions")]
    assert sheet and all(len(r.references) == 1 for r in sheet)  # each view is drawn from the portrait


async def test_identity_mismatch_is_repaired_with_the_same_provider(client: Any, headers: dict[str, str],
                                                                    worker: Any) -> None:  # fmt: skip
    fake.control.identity_mismatch_attempts = 1  # every panel's first image is judged inconsistent
    pid = await _create(client, headers, image_provider="gemini")
    p = await _finish(client, headers, pid, await _through_cast(client, headers, pid))
    assert p["status"] == "completed" and all(pn["status"] == "ok" for pn in _panels(p))
    async with transaction() as db:
        versions = list((await db.execute(select(PanelVersion).order_by(PanelVersion.attempt))).scalars())
    assert sorted(v.attempt for v in versions) == [0, 0, 0, 1, 1, 1]  # one bounded repair each
    first = [v for v in versions if v.attempt == 0]
    assert all(v.qa_status == "fail" and any(pr.startswith("identity_visual:") for pr in v.qa_scores["problems"])
               for v in first)  # fmt: skip
    assert {c[0] for c in fake.control.sync_calls} == {"gemini"}  # repairs stayed on the comic's provider


async def test_uncertain_identity_goes_to_manual_review_without_repainting(client: Any, headers: dict[str, str],
                                                                           worker: Any) -> None:  # fmt: skip
    fake.control.identity_uncertain_attempts = 99
    pid = await _create(client, headers)
    p = await _finish(client, headers, pid, await _through_cast(client, headers, pid))
    assert all(pn["status"] == "needs_attention" for pn in _panels(p))
    async with transaction() as db:
        versions = list((await db.execute(select(PanelVersion))).scalars())
    assert len(versions) == 3 and all(v.qa_status == "review" for v in versions)  # never silently passed
    assert all(v.qa_scores["identity_visual"]["outcome"] == "manual_review" for v in versions)


async def test_an_invalid_qa_answer_is_manual_review_not_a_pass(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    fake.control.errors["text:identity_visual"] = [
        AppError(ErrorCode.PROVIDER_BAD_OUTPUT, "garbled") for _ in range(3)
    ]
    pid = await _create(client, headers)
    p = await _finish(client, headers, pid, await _through_cast(client, headers, pid))
    assert all(pn["status"] == "needs_attention" for pn in _panels(p))
    async with transaction() as db:
        scores = list((await db.execute(select(PanelVersion.qa_scores))).scalars())
    assert all("identity_check_invalid" in sc["problems"] for sc in scores)


async def test_account_deletion_purges_provider_results_and_keeps_costs(client: Any, headers: dict[str, str],
                                                                        worker: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    from storypanel.services import deletion

    pid = await _create(client, headers)
    await _finish(client, headers, pid, await _through_cast(client, headers, pid))
    async with transaction() as db:
        uid = (await db.execute(select(Project.owner_id))).scalar_one()
        assert (
            await db.execute(text("select count(*) from assets where kind = 'provider_result'"))
        ).scalar_one() > 0
    monkeypatch.setattr(get_settings(), "deletion_drain_seconds", 0)
    assert (await client.delete("/v1/me", headers=headers)).status_code in (200, 202, 204)
    for _ in range(100):
        if await deletion.advance(uid) == "completed":
            break
        await asyncio.sleep(0.1)
    async with transaction() as db:
        assert (await db.execute(select(Asset).where(Asset.owner_id == uid))).all() == []
        rows = list(
            (await db.execute(select(ProviderAttempt).where(ProviderAttempt.user_id == uid))).scalars()
        )
    assert rows and all(r.cost_counted_usd is not None for r in rows)  # financial records retained
    assert fake.control.cancels == []  # nothing was sent to a fal-style queue


async def test_provider_errors_in_identity_qa_never_pass() -> None:
    from storypanel.domain.schemas import CharacterSpec, IdentityVisualReport
    from storypanel.pipeline import qa
    from storypanel.providers.base import CallContext
    from tests.test_unit import KIRO

    fake.control.errors["text:identity_visual"] = [AppError(ErrorCode.PROVIDER_ERROR, "503", retryable=True)]
    with pytest.raises(AppError):  # transient: raised for the activity to retry, never a pass
        await qa.check_identity_visual(image=b"x", expected=["kiro"], cast={"kiro": KIRO}, references={"kiro": [b"r"]},
                                       ctx=CallContext("panel_qa", str(uuid.uuid4()), str(uuid.uuid4())))  # fmt: skip
    # a report that leaves an expected character out is unverified, not passed
    rep = IdentityVisualReport(characters=[], outcome="pass", notes=[])
    assert qa.assess_identity(rep, ["kiro"]) == ("manual_review", ["identity_unverified:kiro"])
    # the stricter of the model's outcome and the app's own derivation wins
    lenient = IdentityVisualReport.model_validate({"characters": [
        {"id": "kiro", "present": True, "matches_reference": "no", "mismatches": [], "evidence": "e", "confidence": "high"}],
        "outcome": "pass", "notes": []})  # fmt: skip
    assert qa.assess_identity(lenient, ["kiro"])[0] == "repair"
    assert isinstance(KIRO, CharacterSpec)


def test_print_quality_warns_when_source_resolution_is_low() -> None:
    from storypanel.pipeline.stages_art import merge_print_quality, print_resolution

    buf = io.BytesIO()
    __import__("PIL.Image").Image.new("RGB", (300, 300), "white").save(buf, format="PNG")
    row = print_resolution(0, 0, buf.getvalue(), (40, 40, 920, 700), (1000, 1500))
    assert row["dpi"] == round(min(300 / (920 * 0.006), 300 / (700 * 0.006)))  # ~54 DPI at a 6 in print width
    q = merge_print_quality(None, 0, [row], "local_resample")
    assert q["warnings"] and "blurry in print" in q["warnings"][0] and "does not add detail" in q["note"]
