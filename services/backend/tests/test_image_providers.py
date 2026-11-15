"""OpenAI and Gemini image adapters (request contracts through the real SDKs over a mock HTTP
transport) and the paid-request safeguards for synchronous image calls (simulated providers).
No network: every HTTP exchange below is answered in-process."""

from __future__ import annotations

import base64
import io
import json
import uuid
from decimal import Decimal
from typing import Any

import httpx
import openai
import pytest
from google import genai
from google.genai import types as gt
from PIL import Image
from sqlalchemy import select

from storypanel.config import Settings, get_settings
from storypanel.db.models import Asset, ProviderAttempt, User
from storypanel.db.session import transaction
from storypanel.errors import AppError, ErrorCode
from storypanel.providers import fake, gemini, metering, openai_image, pricing
from storypanel.providers.base import CallContext, ImageInput, ImageRequest


def _png(w: int = 64, h: int = 48, color: str = "red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


LIVE_KEYS = dict(_env_file=None, openai_api_key="sk-test", gemini_api_key="g-test", gemini_qa_model="gemini-3.8-flash",
                 openai_image_model="gpt-image-2.5-sunburst", gemini_image_model="gemini-3.1-flash-image")  # fmt: skip


# ------------------------------------------------------------------ OpenAI contract
@pytest.mark.parametrize(("aspect", "size"), [("4:3", "1536x1152"), ("3:4", "1152x1536"), ("16:9", "1536x864"),
                                              ("1:1", "1536x1536"), ("21:9", "1536x656")])  # fmt: skip
def test_openai_sizes_follow_the_documented_rules(aspect: str, size: str) -> None:
    w, h = map(int, openai_image.size_for(aspect).split("x"))
    assert openai_image.size_for(aspect) == size
    assert w % 16 == 0 and h % 16 == 0 and 1 / 3 <= w / h <= 3 and max(w, h) <= 3840


async def test_openai_edit_sends_base_then_references_and_records_usage() -> None:
    seen: dict[str, Any] = {}
    png = _png()

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"], seen["body"] = str(req.url), req.content
        return httpx.Response(200, headers={"x-request-id": "req_123"}, json={
            "created": 1, "data": [{"b64_json": base64.b64encode(_png(96, 72)).decode()}],
            "usage": {"input_tokens": 3100, "input_tokens_details": {"image_tokens": 3000, "text_tokens": 100},
                      "output_tokens": 4000, "total_tokens": 7100,
                      "output_tokens_details": {"image_tokens": 4000, "text_tokens": 0}}})  # fmt: skip

    p = openai_image.OpenAIImageProvider(Settings(**LIVE_KEYS))  # type: ignore[arg-type]
    p._client = openai.AsyncOpenAI(
        api_key="x", max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    refs = tuple(ImageInput(_png(color=c)) for c in ("blue", "green"))
    req = ImageRequest("Draw panel 2.", "4:3", refs, mode="edit", image=png)
    out = await p.generate(
        req, CallContext("panel_generate", str(uuid.uuid4())), model="gpt-image-2.5-sunburst"
    )
    assert seen["url"] == "https://api.openai.com/v1/images/edits"
    body = seen["body"].decode("latin-1")
    assert body.count('name="image[]"') == 3  # base image + two references, as files
    for field, value in (("model", "gpt-image-2.5-sunburst"), ("size", "1536x1152"), ("quality", "medium"),
                         ("output_format", "png"), ("n", "1")):  # fmt: skip
        assert f'name="{field}"\r\n\r\n{value}' in body
    assert 'name="mask"' not in body  # no provider-side masking is claimed or used
    assert 'name="safety_identifier"' in body and "Draw panel 2." in body
    assert (out.request_id, out.width, out.height) == ("req_123", 96, 72)
    assert out.usage == pricing.ImageUsage(100, 3000, 0, 4000)


async def test_openai_generate_without_inputs_uses_the_generations_endpoint() -> None:
    urls = []

    def handler(req: httpx.Request) -> httpx.Response:
        urls.append(str(req.url))
        assert json.loads(req.content)["size"] == "1536x864"
        return httpx.Response(
            200, json={"created": 1, "data": [{"b64_json": base64.b64encode(_png()).decode()}]}
        )

    p = openai_image.OpenAIImageProvider(Settings(**LIVE_KEYS))  # type: ignore[arg-type]
    p._client = openai.AsyncOpenAI(
        api_key="x", max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    out = await p.generate(
        ImageRequest("A pier.", "16:9"), CallContext("location_ref"), model="gpt-image-2.5-sunburst"
    )
    assert urls == ["https://api.openai.com/v1/images/generations"]
    assert not out.usage.complete  # no usage reported: the attempt will stay unresolved, not guessed


def _status(status: int, code: str | None = None) -> openai.APIStatusError:
    resp = httpx.Response(status, request=httpx.Request("POST", "https://api.openai.com/v1/images/edits"),
                          json={"error": {"code": code, "message": "m"}})  # fmt: skip
    cls = {400: openai.BadRequestError, 401: openai.AuthenticationError, 429: openai.RateLimitError,
           500: openai.InternalServerError}[status]  # fmt: skip
    return cls("m", response=resp, body={"code": code, "message": "m"})


@pytest.mark.parametrize(("exc", "code", "retryable"), [
    (lambda: _status(400, "moderation_blocked"), ErrorCode.MODERATION_BLOCKED, False),
    (lambda: _status(400, "invalid_value"), ErrorCode.PROVIDER_ERROR, False),
    (lambda: _status(401), ErrorCode.PROVIDER_ERROR, False),
    (lambda: _status(429), ErrorCode.PROVIDER_THROTTLED, True),
    (lambda: _status(500), ErrorCode.PROVIDER_ERROR, True),
    (lambda: openai.APITimeoutError(request=httpx.Request("POST", "https://api.openai.com")), ErrorCode.PROVIDER_AMBIGUOUS, True),
    (lambda: openai.APIConnectionError(request=httpx.Request("POST", "https://api.openai.com")), ErrorCode.PROVIDER_AMBIGUOUS, True),
])  # fmt: skip
def test_openai_errors_map_to_known_outcomes(exc: Any, code: ErrorCode, retryable: bool) -> None:
    mapped = openai_image.map_error(exc())
    assert mapped.code is code and mapped.retryable is retryable


def test_openai_refuses_mask_inpainting_explicitly() -> None:
    with pytest.raises(AppError, match="by instruction, not by mask"):
        openai_image.build_arguments(ImageRequest("p", "1:1", mode="inpaint"), model="m", quality="medium",
                                     max_refs=5, safety_identifier=None)  # fmt: skip


# ------------------------------------------------------------------ Gemini contract
def _gemini_client(handler: Any) -> genai.Client:
    return genai.Client(api_key="x", http_options=gt.HttpOptions(async_client_args={"transport": httpx.MockTransport(handler)},
                                                                   retry_options=gt.HttpRetryOptions(attempts=1)))  # fmt: skip


async def test_gemini_image_sends_prompt_then_images_with_aspect_and_size() -> None:
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"], seen["body"] = str(req.url), json.loads(req.content)
        return httpx.Response(200, json={"responseId": "resp_1", "candidates": [{"content": {"role": "model", "parts": [
            {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(_png(80, 60)).decode()}}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 3400, "candidatesTokenCount": 1120, "thoughtsTokenCount": 250,
                              "promptTokensDetails": [{"modality": "TEXT", "tokenCount": 40}, {"modality": "IMAGE", "tokenCount": 3360}],
                              "candidatesTokensDetails": [{"modality": "IMAGE", "tokenCount": 1120}]}})  # fmt: skip

    p = gemini.GeminiImageProvider(Settings(**LIVE_KEYS))  # type: ignore[arg-type]
    p._client = _gemini_client(handler)
    refs = tuple(ImageInput(_png(color=c)) for c in ("blue", "green"))
    out = await p.generate(ImageRequest("Draw the pier.", "4:3", refs, mode="edit", image=_png()),
                           CallContext("panel_generate"), model="gemini-3.1-flash-image")  # fmt: skip
    assert seen["url"].endswith("/models/gemini-3.1-flash-image:generateContent")
    parts = seen["body"]["contents"][0]["parts"]
    assert parts[0]["text"] == "Draw the pier." and sum("inlineData" in x for x in parts) == 3
    cfg = seen["body"]["generationConfig"]
    assert cfg["responseModalities"] == ["IMAGE"] and cfg["imageConfig"] == {
        "aspectRatio": "4:3",
        "imageSize": "1K",
    }
    assert (out.request_id, out.width, out.height) == ("resp_1", 80, 60)
    assert out.usage == pricing.ImageUsage(40, 3360, 250, 1120)  # thinking billed as text output


@pytest.mark.parametrize("reason", ["IMAGE_SAFETY", "PROHIBITED_CONTENT", "SAFETY"])
async def test_gemini_refusal_is_a_moderation_block_with_its_usage(reason: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": []}, "finishReason": reason}],
                                         "usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 0,
                                                           "promptTokensDetails": [{"modality": "TEXT", "tokenCount": 50}],
                                                           "candidatesTokensDetails": []}})  # fmt: skip

    p = gemini.GeminiImageProvider(Settings(**LIVE_KEYS))  # type: ignore[arg-type]
    p._client = _gemini_client(handler)
    with pytest.raises(AppError) as exc:
        await p.generate(
            ImageRequest("p", "1:1"), CallContext("panel_generate"), model="gemini-3.1-flash-image"
        )
    assert exc.value.code is ErrorCode.MODERATION_BLOCKED and not exc.value.retryable
    assert exc.value.meta["reason"] == reason and exc.value.meta["usage"]["input_text_tokens"] == 50


@pytest.mark.parametrize(("status", "code", "retryable"), [(429, ErrorCode.PROVIDER_THROTTLED, True),
                                                           (400, ErrorCode.PROVIDER_ERROR, False),
                                                           (503, ErrorCode.PROVIDER_ERROR, True)])  # fmt: skip
async def test_gemini_http_errors_map_to_known_outcomes(
    status: int, code: ErrorCode, retryable: bool
) -> None:
    p = gemini.GeminiImageProvider(Settings(**LIVE_KEYS))  # type: ignore[arg-type]
    p._client = _gemini_client(
        lambda req: httpx.Response(status, json={"error": {"code": status, "message": "m", "status": "X"}})
    )
    with pytest.raises(AppError) as exc:
        await p.generate(ImageRequest("p", "1:1"), CallContext("x"), model="gemini-3.1-flash-image")
    assert exc.value.code is code and exc.value.retryable is retryable


async def test_gemini_timeout_is_ambiguous() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=req)

    p = gemini.GeminiImageProvider(Settings(**LIVE_KEYS))  # type: ignore[arg-type]
    p._client = _gemini_client(handler)
    with pytest.raises(AppError) as exc:
        await p.generate(ImageRequest("p", "1:1"), CallContext("x"), model="gemini-3.1-flash-image")
    assert exc.value.code is ErrorCode.PROVIDER_AMBIGUOUS


async def test_gemini_visual_qa_validates_the_structured_answer() -> None:
    from storypanel.domain.schemas import IdentityVisualReport

    answers = iter(['{"characters": [], "outcome": "pass", "notes": []}', "not json"])

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        assert "responseJsonSchema" in body["generationConfig"]
        return httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [{"text": next(answers)}]},
                                                         "finishReason": "STOP"}],
                                         "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}})  # fmt: skip

    p = gemini.GeminiVisionProvider(Settings(**LIVE_KEYS))  # type: ignore[arg-type]
    p._client = _gemini_client(handler)
    ok, usage = await p.structured(role="vision", system="s", user="u", schema=IdentityVisualReport, ctx=CallContext("x"),
                                   images=(ImageInput(_png(), label="Panel"),))  # fmt: skip
    assert ok.outcome == "pass" and usage.model == "gemini-3.8-flash"
    with pytest.raises(AppError) as exc:  # an unusable answer is an error, never a pass
        await p.structured(
            role="vision", system="s", user="u", schema=IdentityVisualReport, ctx=CallContext("x")
        )
    assert exc.value.code is ErrorCode.PROVIDER_BAD_OUTPUT


# ------------------------------------------------------------------ paid-request safeguards (simulated)
async def _ctx() -> CallContext:
    async with transaction() as db:
        u = User(id=uuid.uuid4(), auth_subject=f"t:{uuid.uuid4().hex}")
        db.add(u)
    return CallContext("panel_generate", str(u.id), str(uuid.uuid4()))


async def _rows(ctx: CallContext) -> list[ProviderAttempt]:
    async with transaction() as db:
        return list((await db.execute(select(ProviderAttempt).where(ProviderAttempt.user_id == uuid.UUID(str(ctx.user_id)))
                                      .order_by(ProviderAttempt.created_at))).scalars())  # fmt: skip


def _req() -> ImageRequest:
    return ImageRequest("panel", "4:3", (ImageInput(_png()),), mode="edit")


@pytest.fixture(autouse=True)
def _reset() -> Any:
    fake.control.reset()
    yield
    fake.control.reset()


async def test_success_is_settled_from_reported_usage_and_stored_before_success() -> None:
    ctx = await _ctx()
    res, usage = await metering.generate_image_sync(
        _req(), ctx, key="k1", choice="openai", model="fake-openai-image"
    )
    (att,) = await _rows(ctx)
    price = pricing.require("fake", "fake-openai-image")
    expected = pricing.multimodal_cost(price, pricing.ImageUsage(len(b"panel") // 4, 1000, 0, 4000))
    assert (att.status, att.cost_basis, att.cost_counted_usd) == ("succeeded", "calculated", expected)
    assert att.cost_estimate_usd > expected and att.operation == "image_sync_edit"
    assert att.provider == "fake" and att.model == "fake-openai-image" and att.provider_request_id
    assert att.usage_detail == {"input_text_tokens": 1, "input_image_tokens": 1000, "output_text_tokens": 0,
                                "output_image_tokens": 4000}  # fmt: skip
    async with transaction() as db:  # the image was stored before the attempt was marked succeeded
        asset = (await db.execute(select(Asset).where(Asset.id == att.result_asset_id))).scalar_one()
    assert asset.kind == "provider_result" and usage.cost_usd == expected


async def test_a_retry_after_success_reuses_the_stored_image_and_never_pays_twice() -> None:
    ctx = await _ctx()
    first, _ = await metering.generate_image_sync(
        _req(), ctx, key="k2", choice="gemini", model="fake-gemini-image"
    )
    again, _ = await metering.generate_image_sync(
        _req(), ctx, key="k2", choice="gemini", model="fake-gemini-image"
    )
    assert again.data == first.data and len(fake.control.sync_calls) == 1 and len(await _rows(ctx)) == 1


@pytest.mark.parametrize("kind", ["timeout", "connection"])
async def test_ambiguous_timeout_is_never_resent_and_stays_a_potential_charge(kind: str) -> None:
    ctx = await _ctx()
    fake.control.sync_errors["openai"] = [
        AppError(ErrorCode.PROVIDER_AMBIGUOUS, "t", retryable=True, meta={"kind": kind})
    ]
    for _ in range(3):  # the activity retries: none of them re-sends the paid request
        with pytest.raises(AppError) as exc:
            await metering.generate_image_sync(
                _req(), ctx, key="k3", choice="openai", model="fake-openai-image"
            )
        assert exc.value.code is ErrorCode.PROVIDER_AMBIGUOUS_FINAL and not exc.value.retryable
    assert len(fake.control.sync_calls) == 1
    (att,) = await _rows(ctx)
    assert (att.status, att.cost_basis) == (
        "ambiguous",
        "unresolved",
    ) and att.cost_counted_usd == att.cost_estimate_usd
    # recovery path: an explicit user retry is a new operation key (a separately reserved attempt)
    await metering.generate_image_sync(
        _req(), ctx, key="k3-user-retry", choice="openai", model="fake-openai-image"
    )
    assert len(fake.control.sync_calls) == 2


async def test_an_unresolved_timeout_is_resolved_by_attempt_id_from_the_dashboard() -> None:
    """Recovery path for a charge with no request id: the operator imports the dashboard figure
    against the attempt id; the counted cost moves from the reservation to the confirmed charge."""
    ctx = await _ctx()
    fake.control.sync_errors["gemini"] = [AppError(ErrorCode.PROVIDER_AMBIGUOUS, "t", retryable=True)]
    with pytest.raises(AppError):
        await metering.generate_image_sync(_req(), ctx, key="k3b", choice="gemini", model="fake-gemini-image")
    (att,) = await _rows(ctx)
    assert att.provider_request_id is None and att.cost_basis == "unresolved"
    with pytest.raises(AppError):
        await metering.record_provider_charges([("fake", "attempt:not-a-uuid", Decimal(0), "x")])
    with pytest.raises(AppError):
        await metering.record_provider_charges([("fake", f"attempt:{att.id}", Decimal(-1), "x")])
    assert await metering.record_provider_charges([("other", f"attempt:{att.id}", Decimal("0.05"), "x")]) == 0
    n = await metering.record_provider_charges([("fake", f"attempt:{att.id}", Decimal("0.05"), "usage page")])
    (row,) = await _rows(ctx)
    assert n == 1 and (row.cost_basis, row.cost_counted_usd, row.confirmed_source) == (
        "provider_confirmed",
        Decimal("0.05"),
        "usage page",
    )


async def test_worker_death_mid_call_is_ambiguous_on_resume() -> None:
    ctx = await _ctx()
    price = pricing.require("fake", "fake-openai-image")
    att = await metering._begin_attempt(ctx, key="k4", provider="fake", model="fake-openai-image",
                                        operation="image_sync_edit", estimate=Decimal("0.3"), price=price, after=0)  # fmt: skip
    await metering._mark(att, status="submitting")  # the worker died here
    with pytest.raises(AppError) as exc:
        await metering.generate_image_sync(_req(), ctx, key="k4", choice="openai", model="fake-openai-image")
    assert exc.value.code is ErrorCode.PROVIDER_AMBIGUOUS_FINAL and fake.control.sync_calls == []
    (row,) = await _rows(ctx)
    assert (row.status, row.cost_basis, row.cost_counted_usd) == ("ambiguous", "unresolved", Decimal("0.3"))


async def test_server_error_counts_by_policy_and_permits_a_new_attempt() -> None:
    ctx = await _ctx()
    fake.control.sync_errors["gemini"] = [AppError(ErrorCode.PROVIDER_ERROR, "5xx", retryable=True)]
    with pytest.raises(AppError):
        await metering.generate_image_sync(_req(), ctx, key="k5", choice="gemini", model="fake-gemini-image")
    await metering.generate_image_sync(_req(), ctx, key="k5", choice="gemini", model="fake-gemini-image")
    a1, a2 = await _rows(ctx)
    assert (a1.status, a1.cost_basis, a1.error_code) == (
        "failed",
        "unresolved",
        "server_error",
    )  # counted (policy)
    assert (a2.status, a2.attempt_no) == ("succeeded", 2)


@pytest.mark.parametrize("code", [ErrorCode.PROVIDER_THROTTLED, ErrorCode.PROVIDER_ERROR])
async def test_rejected_before_work_is_released(code: ErrorCode) -> None:
    ctx = await _ctx()
    fake.control.sync_errors["openai"] = [
        AppError(code, "no", retryable=code is ErrorCode.PROVIDER_THROTTLED and True)
    ]
    if code is ErrorCode.PROVIDER_ERROR:
        fake.control.sync_errors["openai"] = [AppError(code, "401", retryable=False)]
    with pytest.raises(AppError):
        await metering.generate_image_sync(_req(), ctx, key="k6", choice="openai", model="fake-openai-image")
    (att,) = await _rows(ctx)
    assert (att.status, att.cost_basis, att.cost_counted_usd) == ("released", "released", Decimal(0))


async def test_a_refusal_is_recorded_and_never_routed_to_the_other_provider() -> None:
    ctx = await _ctx()
    usage = {
        "input_text_tokens": 40,
        "input_image_tokens": 1120,
        "output_text_tokens": 0,
        "output_image_tokens": 0,
    }
    fake.control.sync_errors["gemini"] = [AppError(ErrorCode.MODERATION_BLOCKED, "refused", retryable=False,
                                                   meta={"usage": usage})]  # fmt: skip
    with pytest.raises(AppError) as exc:
        await metering.generate_image_sync(_req(), ctx, key="k7", choice="gemini", model="fake-gemini-image")
    assert exc.value.code is ErrorCode.MODERATION_BLOCKED
    assert [c[0] for c in fake.control.sync_calls] == ["gemini"]  # OpenAI was not asked instead
    (att,) = await _rows(ctx)
    price = pricing.require("fake", "fake-gemini-image")
    assert (att.status, att.cost_basis) == ("failed", "calculated")
    assert att.cost_counted_usd == pricing.multimodal_cost(price, pricing.ImageUsage(**usage))


async def test_missing_usage_keeps_the_reservation_unresolved() -> None:
    ctx = await _ctx()
    fake.control.sync_omit_usage = True
    await metering.generate_image_sync(_req(), ctx, key="k8", choice="openai", model="fake-openai-image")
    (att,) = await _rows(ctx)
    assert (att.status, att.cost_basis, att.cost_counted_usd) == (
        "succeeded",
        "unresolved",
        att.cost_estimate_usd,
    )


async def test_spending_cap_refuses_before_the_call() -> None:
    from storypanel.db.models import SpendCap

    ctx = await _ctx()
    async with transaction() as db:
        db.add(SpendCap(user_id=uuid.UUID(str(ctx.user_id)), cap_usd=Decimal("0.05"), reason="test"))
    with pytest.raises(AppError) as exc:
        await metering.generate_image_sync(_req(), ctx, key="k9", choice="openai", model="fake-openai-image")
    assert exc.value.code is ErrorCode.SPEND_LIMIT and fake.control.sync_calls == []


async def test_reconciliation_and_deletion_never_query_a_synchronous_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import timedelta

    from sqlalchemy import text

    ctx = await _ctx()
    price = pricing.require("fake", "fake-openai-image")
    att = await metering._begin_attempt(ctx, key="k10", provider="fake", model="fake-openai-image",
                                        operation="image_sync_generate", estimate=Decimal("0.3"), price=price, after=0)  # fmt: skip
    await metering._mark(att, status="submitting")
    async with transaction() as db:
        await db.execute(text("update provider_attempts set updated_at = now() - interval '2 hours'"))
    out = await metering.reconcile_open_requests(older_than=timedelta(minutes=30))
    assert out["ambiguous"] == 1 and fake.control.fetches == [] and fake.control.cancels == []
    (row,) = await _rows(ctx)
    assert (row.status, row.cost_basis) == ("ambiguous", "unresolved")
    # account deletion's stop path: a synchronous call in flight cannot be cancelled, stays unresolved
    att2 = await metering._begin_attempt(ctx, key="k11", provider="fake", model="fake-openai-image",
                                         operation="image_sync_generate", estimate=Decimal("0.3"), price=price, after=0)  # fmt: skip
    await metering._mark(att2, status="submitting")
    await metering.stop_attempts([await metering.latest_attempt("k11")], reason="account_deleted")  # type: ignore[list-item]
    rows = await _rows(ctx)
    assert (rows[-1].status, rows[-1].cost_basis, rows[-1].error_code) == (
        "ambiguous",
        "unresolved",
        "account_deleted",
    )
    assert fake.control.cancels == []


def test_disabled_fal_and_voyage_are_never_constructed_or_imported() -> None:
    """Live provider set built with only Anthropic, OpenAI and Gemini keys: no fal or Voyage client
    exists, and their SDKs are not even imported (run in a fresh interpreter)."""
    import subprocess
    import sys
    from pathlib import Path

    table = Path(__file__).resolve().parents[3] / "packages/pricing/pricing.2026-11-15.json"
    code = (
        "import json,sys; from storypanel.config import Settings; from storypanel.providers.registry import build_providers\n"
        f"s=Settings(_env_file=None, provider_mode='live', anthropic_api_key='x', openai_api_key='x', gemini_api_key='x',"
        " text_model_story='claude-sonnet-5-5', text_model_light='claude-haiku-5-5', vision_model='claude-haiku-5-5',"
        " openai_image_model='gpt-image-2.5-sunburst', gemini_image_model='gemini-3.1-flash-image',"
        f" gemini_qa_model='gemini-3.8-flash', pricing_file={str(table)!r}, pricing_max_age_days=3650)\n"
        "p=build_providers(s)\n"
        "print(json.dumps({'image': p.image is None, 'embedding': p.embedding is None, 'upscale': p.upscale is None,"
        " 'drawers': sorted(p.drawers), 'identity': type(p.identity).__name__,"
        " 'fal_imported': 'fal_client' in sys.modules, 'voyage_imported': 'voyageai' in sys.modules}))"
    )
    env = {k: v for k, v in __import__("os").environ.items() if not k.startswith("SP_")}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True)
    assert json.loads(out.stdout.strip().splitlines()[-1]) == {
        "image": True, "embedding": True, "upscale": True, "drawers": ["gemini", "openai"],
        "identity": "GeminiVisionProvider", "fal_imported": False, "voyage_imported": False}  # fmt: skip


def test_default_settings_need_no_fal_or_voyage() -> None:
    s = get_settings()
    assert (s.fal_enabled, s.voyage_enabled, s.export_upscale, s.identity_qa) == (
        False,
        False,
        "local",
        "gemini_visual",
    )
    assert not {"fal_key", "voyage_api_key", "upscale_model", "embedding_model"} & set(
        Settings(_env_file=None, provider_mode="fake").required_live_settings()
    )  # type: ignore[call-arg]
