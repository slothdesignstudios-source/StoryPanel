"""Provider contracts, pricing policy and paid-request lifecycle.

Contract tests check the request bodies we build against fal's *published* endpoint
types (tests/fixtures/fal_schema_fields.json, extracted from @fal-ai/client). They prove
schema conformance offline; they do not prove the live service behaves as documented.
"""

from __future__ import annotations

import asyncio
import io
import json
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fal_client.client import Completed, InProgress
from PIL import Image
from sqlalchemy import select, text

from storypanel.config import Settings, get_settings
from storypanel.db.models import ProviderUsage, User
from storypanel.db.session import transaction
from storypanel.errors import AppError, ErrorCode
from storypanel.providers import fake, fal_image, metering, pricing
from storypanel.providers.anthropic_text import FALLBACK_BETA, AnthropicTextProvider
from storypanel.providers.base import CallContext, ImageRequest, RequestPending, RequestUnknown
from storypanel.providers.fal_profiles import PROFILES

SCHEMA = json.loads((Path(__file__).parent / "fixtures" / "fal_schema_fields.json").read_text())["endpoints"]


def png(w: int = 64, h: int = 48) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, format="PNG")
    return buf.getvalue()


# ------------------------------------------------------------------ fal contracts
@pytest.mark.parametrize("endpoint", sorted(PROFILES))
@pytest.mark.parametrize("aspect", ["4:3", "3:4", "16:9", "2:3", "1:1", "21:9"])
@pytest.mark.parametrize("audience", ["all_ages", "teen", "mature"])
def test_request_bodies_conform_to_published_schema(endpoint: str, aspect: str, audience: str) -> None:
    profile, schema = PROFILES[endpoint], SCHEMA[endpoint]["input"]
    if "upscale" in profile.roles:
        args = {**profile.fixed, "image_url": "https://x/a.png", "scale": 2}
    else:
        req = ImageRequest("a robot ladles noodles", aspect, seed=7, audience=audience,
                           mode="inpaint" if "inpaint" in profile.roles else "generate")  # fmt: skip
        args = fal_image.build_arguments(
            profile, req, reference_urls=["https://x/r1.png", "https://x/r2.png"] if profile.reference_field else [],
            image_url="https://x/i.png" if profile.image_field else None,
            mask_url="https://x/m.png" if profile.mask_field else None)  # fmt: skip
    assert set(args) <= set(schema), f"unknown fields {set(args) - set(schema)}"
    missing = {k for k, v in schema.items() if v["required"]} - set(args)
    assert not missing, f"required fields missing: {missing}"
    for key, value in args.items():
        enum = schema[key]["enum"]
        if enum and isinstance(value, str):
            assert value in enum, f"{key}={value!r} not in {enum}"
    if profile.aspect_mode == "image_size_wh":
        size = args["image_size"]
        assert profile.min_pixels <= size["width"] * size["height"] <= profile.max_pixels
        w, h = (int(x) for x in aspect.split(":"))
        assert abs(size["width"] / size["height"] - w / h) < 0.05


def test_reference_limits_and_required_inputs() -> None:
    edit = PROFILES["fal-ai/nano-banana-2/edit"]
    req = ImageRequest("p", "4:3")
    urls = [f"https://x/{n}.png" for n in range(20)]
    assert (
        len(
            fal_image.build_arguments(edit, req, reference_urls=urls, image_url=None, mask_url=None)[
                "image_urls"
            ]
        )
        == 14
    )
    seedream = PROFILES["fal-ai/bytedance/seedream/v5/lite/edit"]
    assert len(fal_image.build_arguments(seedream, req, reference_urls=urls, image_url=None,
                                         mask_url=None)["image_urls"]) == 10  # fmt: skip
    with pytest.raises(AppError):  # edit endpoints need references
        fal_image.build_arguments(edit, req, reference_urls=[], image_url=None, mask_url=None)
    with pytest.raises(AppError):  # text-to-image endpoints must not receive them
        fal_image.build_arguments(PROFILES["fal-ai/nano-banana-2"], req, reference_urls=urls[:1], image_url=None,
                                  mask_url=None)  # fmt: skip
    with pytest.raises(AppError):  # inpainting needs a mask
        fal_image.build_arguments(PROFILES["fal-ai/flux-pro/v1/fill"], req, reference_urls=[],
                                  image_url="https://x/i.png", mask_url=None)  # fmt: skip


@pytest.mark.parametrize("endpoint", sorted(PROFILES))
def test_result_parsing_matches_published_output(endpoint: str) -> None:
    profile, out = PROFILES[endpoint], SCHEMA[endpoint]["output"]
    sample: dict[str, Any] = ({"image": {"url": "https://cdn/x.png"}} if profile.result == "image"
                              else {"images": [{"url": "https://cdn/x.png"}], "seed": 1})  # fmt: skip
    assert set(sample) <= set(out) | {"seed"}
    assert fal_image.extract_result_url(profile, sample) == "https://cdn/x.png"
    with pytest.raises(AppError) as e:
        fal_image.extract_result_url(profile, {"unexpected": True})
    assert e.value.code is ErrorCode.PROVIDER_BAD_OUTPUT
    if profile.nsfw_flags_field:
        assert profile.nsfw_flags_field in out
        with pytest.raises(AppError) as e:
            fal_image.extract_result_url(profile, {**sample, profile.nsfw_flags_field: [True]})
        assert e.value.code is ErrorCode.MODERATION_BLOCKED


class _Http(Exception):
    pass


async def test_queue_outcomes_are_classified_from_sdk_status() -> None:
    """The fal SDK reports a failed queued request as Completed(error=...); a 404 on status
    means the id is unknown. Neither may be mistaken for success or for 'still running'."""
    from fal_client.client import FalClientHTTPError

    class Client:
        def __init__(self, status: Any) -> None:
            self._status = status

        async def status(self, app: str, rid: str) -> Any:
            if isinstance(self._status, Exception):
                raise self._status
            return self._status

        async def result(self, app: str, rid: str) -> Any:
            return {"images": [{"url": "https://cdn/x.png"}]}

    def provider(status: Any) -> fal_image.FalImageProvider:
        p = fal_image.FalImageProvider.__new__(fal_image.FalImageProvider)
        p._client = Client(status)  # type: ignore[assignment]
        return p

    with pytest.raises(AppError) as e:
        await provider(Completed(logs=None, metrics={}, error="CUDA OOM", error_type="internal")).fetch(
            "fal-ai/nano-banana-2/edit", "r1", wait_seconds=0
        )
    assert e.value.code is ErrorCode.PROVIDER_ERROR and e.value.retryable
    with pytest.raises(AppError) as e:
        await provider(
            Completed(logs=None, metrics={}, error="blocked", error_type="content_policy_violation")
        ).fetch("fal-ai/nano-banana-2/edit", "r1", wait_seconds=0)
    assert e.value.code is ErrorCode.MODERATION_BLOCKED and not e.value.retryable
    with pytest.raises(RequestPending):
        await provider(InProgress(logs=None)).fetch("fal-ai/nano-banana-2/edit", "r1", wait_seconds=0)
    resp = SimpleNamespace(status_code=404)
    with pytest.raises(RequestUnknown):
        await provider(FalClientHTTPError("not found", 404, {}, response=resp)).fetch(  # type: ignore[arg-type]
            "fal-ai/nano-banana-2/edit", "r1", wait_seconds=0
        )


# ------------------------------------------------------------------ anthropic
def _anthropic(**over: Any) -> AnthropicTextProvider:
    s = Settings(_env_file=None, anthropic_api_key="x", text_model_story="claude-sonnet-5-5",  # type: ignore[call-arg]
                 text_model_light="claude-haiku-5-5", vision_model="claude-haiku-5-5", **over)  # fmt: skip
    return AnthropicTextProvider(s)


def test_anthropic_request_options_and_refusal_handling() -> None:
    p = _anthropic()
    story = p.request_args("story", "claude-sonnet-5-5")
    assert story["fallbacks"] == "default" and story["betas"] == [FALLBACK_BETA]
    assert story["output_config"] == {"effort": "medium"}
    light = p.request_args("light", "claude-haiku-5-5")
    assert "fallbacks" not in light and light["output_config"] == {"effort": "low"}  # no fallback on Haiku
    assert "fallbacks" not in _anthropic(text_refusal_fallback=False).request_args(
        "story", "claude-sonnet-5-5"
    )

    usage = SimpleNamespace(
        input_tokens=100, output_tokens=50, cache_read_input_tokens=10, cache_creation_input_tokens=None
    )
    refused = SimpleNamespace(
        stop_reason="refusal", parsed_output=None, usage=usage, model="claude-sonnet-5-5"
    )
    with pytest.raises(AppError) as e:
        p.interpret(refused, model="claude-sonnet-5-5", started=0)
    assert e.value.code is ErrorCode.MODERATION_BLOCKED and not e.value.retryable
    truncated = SimpleNamespace(stop_reason="max_tokens", parsed_output=object(), usage=usage, model="m")
    with pytest.raises(AppError):
        p.interpret(truncated, model="m", started=0)
    served = SimpleNamespace(
        stop_reason="end_turn", parsed_output={"ok": 1}, usage=usage, model="claude-opus-4-8"
    )
    out, u = p.interpret(served, model="claude-sonnet-5-5", started=0)
    assert u.model == "claude-opus-4-8" and u.input_tokens == 110  # the fallback model is what gets priced


# ------------------------------------------------------------------ pricing policy
def _entry(**over: Any) -> dict[str, Any]:
    return {"provider": "fal", "model": "fal-ai/nano-banana-2/edit", "unit": "per_image", "usd": "0.08",
            "source": "fal model page", "verified_at": date.today().isoformat(), **over}  # fmt: skip


@pytest.mark.parametrize(("over", "message"), [
    ({"usd": "0"}, "price is zero"), ({"usd": "-1"}, "non-negative"), ({"usd": "abc"}, "not a number"),
    ({"source": ""}, "'source' is required"), ({"verified_at": None}, "'verified_at' is required"),
    ({"verified_at": "last week"}, "ISO date"), ({"unit": "per_banana"}, "unit must be"),
    ({"unit": "per_mtok", "input_usd": "2", "output_usd": "0"}, "price is zero"),
])  # fmt: skip
def test_price_table_rejects_bad_entries(over: dict[str, Any], message: str) -> None:
    with pytest.raises(pricing.PricingError, match=message):
        pricing.parse_table({"entries": [_entry(**over)]})


def test_price_table_accepts_explicit_free_and_rejects_duplicates() -> None:
    t = pricing.parse_table({"entries": [_entry(usd="0", free=True)]})
    assert t[("fal", "fal-ai/nano-banana-2/edit")].free
    with pytest.raises(pricing.PricingError, match="duplicate"):
        pricing.parse_table({"entries": [_entry(), _entry()]})


LIVE = dict(
    _env_file=None, provider_mode="live", anthropic_api_key="x", fal_key="x", voyage_api_key="x",
    text_model_story="claude-sonnet-5-5", text_model_light="claude-haiku-5-5", vision_model="claude-haiku-5-5",
    image_model_generate="fal-ai/nano-banana-2", image_model_edit="fal-ai/nano-banana-2/edit",
    upscale_model="fal-ai/esrgan", embedding_model="voyage-multimodal-3.5",
)  # fmt: skip


def _table(**override: Any) -> str:
    today = date.today().isoformat()
    entries = [
        {"provider": "anthropic", "model": m, "unit": "per_mtok", "input_usd": i, "output_usd": o,
         "source": "Anthropic model table", "verified_at": today}
        for m, i, o in (("claude-sonnet-5-5", "2", "10"), ("claude-haiku-5-5", "0.1", "0.5"))
    ] + [_entry(model=m, usd=p) for m, p in (("fal-ai/nano-banana-2", "0.08"), ("fal-ai/nano-banana-2/edit", "0.08"),
                                             ("fal-ai/esrgan", "0.01"))]  # fmt: skip
    entries.append({"provider": "voyage", "model": "voyage-multimodal-3.5", "unit": "per_billion_pixels",
                    "usd": "0.6", "source": "Voyage pricing", "verified_at": today})  # fmt: skip
    for e in entries:
        e.update(override.get(e["model"], {}))
    return json.dumps({"entries": entries})


def test_live_configuration_requires_verified_models_and_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in [k for k in __import__("os").environ if k.startswith("SP_")]:
        monkeypatch.delenv(key)
    pricing.reset_cache()
    assert Settings(**LIVE, pricing_json=_table())  # type: ignore[arg-type]
    pricing.reset_cache()
    with pytest.raises(ValueError, match="No verified price is configured for fal/fal-ai/esrgan"):
        Settings(**LIVE, pricing_json=_table(**{"fal-ai/esrgan": {"model": "other"}}))  # type: ignore[arg-type]
    pricing.reset_cache()
    stale = (date.today() - timedelta(days=90)).isoformat()
    with pytest.raises(ValueError, match="last verified 90 days ago"):
        Settings(**LIVE, pricing_json=_table(**{"fal-ai/nano-banana-2/edit": {"verified_at": stale}}))  # type: ignore[arg-type]
    pricing.reset_cache()
    with pytest.raises(ValueError, match="price is zero"):
        Settings(**LIVE, pricing_json=_table(**{"fal-ai/esrgan": {"usd": "0"}}))  # type: ignore[arg-type]
    pricing.reset_cache()
    with pytest.raises(ValueError, match="no verified fal schema"):
        Settings(**{**LIVE, "image_model_edit": "fal-ai/some-new-model"}, pricing_json=_table())  # type: ignore[arg-type]
    pricing.reset_cache()
    with pytest.raises(ValueError, match="not a Voyage model known to accept images"):
        Settings(**{**LIVE, "embedding_model": "voyage-3.5"}, pricing_json=_table())  # type: ignore[arg-type]
    pricing.reset_cache()
    with pytest.raises(ValueError, match="pricing table is invalid"):
        Settings(**LIVE, pricing_json="{not json")  # type: ignore[arg-type]
    pricing.reset_cache()
    with pytest.raises(ValueError, match="No verified price"):  # no table at all
        Settings(**LIVE)  # type: ignore[arg-type]
    pricing.reset_cache()


def test_cost_estimates_are_upper_bounds() -> None:
    price = pricing.parse_table({"entries": [{"provider": "a", "model": "m", "unit": "per_mtok", "input_usd": "2",
                                               "output_usd": "10", "source": "s", "verified_at": "2026-01-01"}]})[("a", "m")]  # fmt: skip
    est = pricing.text_estimate(price, input_chars=30_000, images_px=[1_000_000], max_tokens=16_000)
    actual = pricing.text_cost(price, input_tokens=9_000, output_tokens=2_000)
    assert est > actual and est >= Decimal("0.16")  # full output budget is reserved


# ------------------------------------------------------------------ reservations & lifecycle
async def _owner() -> tuple[str, str]:
    async with transaction() as db:
        u = User(id=uuid.uuid4(), auth_subject=f"t:{uuid.uuid4().hex}")
        db.add(u)
    return str(u.id), str(uuid.uuid4())


async def test_concurrent_reservations_never_exceed_the_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "spend_limit_project_usd", Decimal("0.40"))
    uid, pid = await _owner()
    ctx = CallContext("panel_generate", uid, pid)

    async def one(n: int) -> str:
        try:
            await metering.generate_image(ImageRequest(f"p{n}", "4:3"), ctx, key=f"race:{pid}:{n}")
            return "ok"
        except AppError as exc:
            return exc.code.value

    results = await asyncio.gather(*[one(n) for n in range(20)])
    assert results.count("ok") == 10 and results.count("spend_limit_reached") == 10  # 10 x $0.04 = $0.40
    async with transaction() as db:
        total = (await db.execute(text("select sum(cost_counted_usd) from provider_usage where project_id = :p"),
                                  {"p": pid})).scalar_one()  # fmt: skip
    assert Decimal(total) == Decimal("0.40")


async def test_text_calls_reserve_the_full_output_budget_then_settle_actual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from storypanel.domain.schemas import SafetyVerdict

    uid, pid = await _owner()
    ctx = CallContext("image_safety", uid, pid)
    monkeypatch.setattr(get_settings(), "spend_limit_project_usd", Decimal("0.005"))
    with pytest.raises(AppError) as e:  # 16k output tokens at $0.50/Mtok = $0.008 > $0.005
        await metering.structured(role="vision", system="s", user="u", schema=SafetyVerdict, ctx=ctx)
    assert e.value.code is ErrorCode.SPEND_LIMIT
    monkeypatch.setattr(get_settings(), "spend_limit_project_usd", Decimal("1"))
    await metering.structured(role="vision", system="s", user="u", schema=SafetyVerdict, ctx=ctx)
    async with transaction() as db:
        row = (
            await db.execute(select(ProviderUsage).where(ProviderUsage.project_id == uuid.UUID(pid)))
        ).scalar_one()
    assert row.status == "succeeded" and row.cost_basis == "actual"
    assert row.cost_counted_usd < row.cost_estimate_usd  # settled at actual, below the reservation


async def test_pricing_missing_refuses_before_any_provider_call(monkeypatch: pytest.MonkeyPatch) -> None:
    uid, pid = await _owner()
    monkeypatch.setattr(pricing, "SIMULATED", {"entries": []})
    pricing.reset_cache()
    try:
        with pytest.raises(AppError) as e:
            await metering.generate_image(
                ImageRequest("p", "4:3"), CallContext("x", uid, pid), key=f"np:{pid}"
            )
        assert e.value.code is ErrorCode.PRICING_UNAVAILABLE and not e.value.retryable
        assert fake.control.submits == []
    finally:
        monkeypatch.undo()
        pricing.reset_cache()


async def test_ambiguous_submission_is_never_resubmitted() -> None:
    uid, pid = await _owner()
    ctx = CallContext("panel_generate", uid, pid)
    fake.control.errors["image:submit"] = [AppError(ErrorCode.PROVIDER_AMBIGUOUS, "timeout", retryable=True)]
    key = f"amb:{pid}"
    for _ in range(3):  # the activity retry and two more attempts
        with pytest.raises(AppError) as e:
            await metering.generate_image(ImageRequest("p", "4:3"), ctx, key=key)
        assert e.value.code is ErrorCode.PROVIDER_AMBIGUOUS_FINAL and not e.value.retryable
    assert len(fake.control.models_used) == 1  # submitted once, never again
    async with transaction() as db:
        row = (
            await db.execute(select(ProviderUsage).where(ProviderUsage.idempotency_key == key))
        ).scalar_one()
    assert row.status == "ambiguous" and row.cost_counted_usd == row.cost_estimate_usd > 0  # visible, counted


async def test_worker_death_mid_submission_is_treated_as_ambiguous() -> None:
    uid, pid = await _owner()
    key = f"crash:{pid}"
    price = pricing.require("fake", "fake-image")
    ctx = CallContext("panel_generate", uid, pid)
    await metering._reserve(ctx, key=key, provider="fake", model="fake-image", operation="image_generate",
                            estimate=Decimal("0.04"), price=price)  # fmt: skip
    await metering._update(key, status="submitting")  # ...and the worker died here
    with pytest.raises(AppError) as e:
        await metering.generate_image(ImageRequest("p", "4:3"), ctx, key=key)
    assert e.value.code is ErrorCode.PROVIDER_AMBIGUOUS_FINAL
    assert fake.control.models_used == []


async def test_timeout_after_submission_resumes_the_same_request() -> None:
    uid, pid = await _owner()
    ctx = CallContext("panel_generate", uid, pid)
    fake.control.pending_fetches = 2
    key = f"resume:{pid}"
    for _ in range(2):
        with pytest.raises(AppError) as e:
            await metering.generate_image(ImageRequest("p", "4:3"), ctx, key=key)
        assert e.value.code is ErrorCode.TIMEOUT and e.value.retryable
    result, _ = await metering.generate_image(ImageRequest("p", "4:3"), ctx, key=key)
    assert result.data and len(fake.control.models_used) == 1  # one submission, resumed twice


async def test_definitive_failure_allows_a_fresh_submission() -> None:
    uid, pid = await _owner()
    ctx = CallContext("panel_generate", uid, pid)
    fake.control.errors["image:fetch"] = [AppError(ErrorCode.PROVIDER_ERROR, "failed at provider")]
    key = f"fail:{pid}"
    with pytest.raises(AppError):
        await metering.generate_image(ImageRequest("p", "4:3"), ctx, key=key)
    await metering.generate_image(ImageRequest("p", "4:3"), ctx, key=key)
    async with transaction() as db:
        row = (
            await db.execute(select(ProviderUsage).where(ProviderUsage.idempotency_key == key))
        ).scalar_one()
    assert row.status == "succeeded" and row.submissions == 2


async def test_cancellation_stops_the_provider_request() -> None:
    uid, pid = await _owner()
    ctx = CallContext("panel_generate", uid, pid)
    fake.control.hang_fetches = 1
    key = f"cancel:{pid}"
    task = asyncio.create_task(metering.generate_image(ImageRequest("p", "4:3"), ctx, key=key))
    for _ in range(100):
        await asyncio.sleep(0.02)
        async with transaction() as db:
            status = (await db.execute(select(ProviderUsage.status).where(
                ProviderUsage.idempotency_key == key))).scalar_one_or_none()  # fmt: skip
        if status == "started":
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.2)
    assert len(fake.control.cancelled) == 1
    async with transaction() as db:
        row = (
            await db.execute(select(ProviderUsage).where(ProviderUsage.idempotency_key == key))
        ).scalar_one()
    assert row.status == "cancelled" and row.cost_counted_usd > 0  # may have been billed: kept counted


async def test_outage_opens_breaker_and_routes_new_requests_to_fallback() -> None:
    uid, pid = await _owner()
    ctx = CallContext("panel_generate", uid, pid)
    fake.control.errors["image:submit"] = [AppError(ErrorCode.PROVIDER_ERROR, "503") for _ in range(5)]
    for n in range(5):
        with pytest.raises(AppError):
            await metering.generate_image(ImageRequest("p", "4:3"), ctx, key=f"bk:{pid}:{n}")
    result, usage = await metering.generate_image(ImageRequest("p", "4:3"), ctx, key=f"bk:{pid}:after")
    assert result.model == "fake-fallback" and usage.cost_usd == Decimal("0.035")
    assert fake.control.models_used[-1] == "fake-fallback"
    # content refusals never trip the breaker
    from storypanel.services.events import redis_client

    await redis_client().delete("sp:breaker:fake-image", "sp:breakerfail:fake-image")
    fake.control.errors["image:submit"] = [AppError(ErrorCode.MODERATION_BLOCKED, "no") for _ in range(6)]
    for n in range(6):
        with pytest.raises(AppError):
            await metering.generate_image(ImageRequest("p", "4:3"), ctx, key=f"mod:{pid}:{n}")
    assert not await redis_client().exists("sp:breaker:fake-image")


async def test_reconciliation_resolves_abandoned_requests() -> None:
    uid, pid = await _owner()
    ctx = CallContext("panel_generate", uid, pid)
    price = pricing.require("fake", "fake-image")
    provider = fake.FakeImageProvider()
    from storypanel.providers import registry

    p = registry.get_providers()
    registry.set_providers(registry.Providers(p.text, provider, p.embedding, p.upscale, "fake"))
    done = await provider.submit(ImageRequest("p", "4:3"), ctx, model="fake-image")
    states = {"never-sent": None, "lost-id": "nope", "finished": done}
    for name, rid in states.items():
        await metering._reserve(ctx, key=f"rc:{name}:{pid}", provider="fake", model="fake-image",
                                operation="image_generate", estimate=Decimal("0.04"), price=price)  # fmt: skip
        if rid:
            await metering._update(f"rc:{name}:{pid}", status="started", provider_request_id=rid)
    async with transaction() as db:
        await db.execute(text("update provider_usage set updated_at = now() - interval '2 hours' where project_id = :p"),
                         {"p": pid})  # fmt: skip
    out = await metering.reconcile_open_requests()
    assert out["released"] == 2 and out["succeeded_unused"] == 1
    async with transaction() as db:
        rows = dict((await db.execute(select(ProviderUsage.idempotency_key, ProviderUsage.cost_counted_usd).where(
            ProviderUsage.project_id == uuid.UUID(pid)))).all())  # fmt: skip
    assert rows[f"rc:never-sent:{pid}"] == 0 and rows[f"rc:lost-id:{pid}"] == 0
    assert rows[f"rc:finished:{pid}"] == Decimal("0.04")  # produced: counted even though unused
    registry.set_providers(None)
