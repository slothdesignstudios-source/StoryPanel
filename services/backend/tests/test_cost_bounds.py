"""Cost-bound regression tests for the first live smoke test (2026-11-12 cost audit).

Each test pins one property the smoke-test spend analysis (docs/smoke-test-cost.md) relies on:
- a text reservation covers the provider's own input-token formula, not a typing heuristic;
- a compute-time-billed request is cancelled at the time it reserved, not waited on for 40 minutes;
- optional fal add-ons with their own charges are explicitly switched off.
"""

from __future__ import annotations

import asyncio
import io
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from PIL import Image
from sqlalchemy import select

from storypanel.db.models import ProviderAttempt, User
from storypanel.db.session import transaction
from storypanel.errors import AppError, ErrorCode
from storypanel.providers import fake, fal_image, fal_profiles, metering, pricing
from storypanel.providers.base import CallContext, ImageRequest


def _png(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, format="PNG")
    return buf.getvalue()


# ------------------------------------------------------------------ text reservations
def test_text_input_bound_covers_the_documented_token_rules() -> None:
    # Non-ASCII text: the old chars/3 heuristic assumed 1,000 tokens for 3,000 CJK characters;
    # a tokenizer can need more than one token per character. The bound uses UTF-8 bytes
    # (byte-level tokens are at least one byte each).
    cjk = "灯" * 3000
    assert pricing.text_input_bound(input_bytes=len(cjk.encode()), images_wh=[]) >= len(cjk.encode())
    # Images: official rule ceil(w/28) x ceil(h/28) visual tokens, at most 4,784 (Claude 4.7+).
    # A 100x30 crop costs 4 x 2 = 8 tokens; px/750 gave 4.
    small = pricing.text_input_bound(input_bytes=0, images_wh=[(100, 30)]) - pricing.TEXT_OVERHEAD_TOKENS
    assert small >= 8
    big = pricing.text_input_bound(input_bytes=0, images_wh=[(8000, 8000)]) - pricing.TEXT_OVERHEAD_TOKENS
    assert big == 4784  # downscaled by the API: capped, not 81,796
    one = pricing.text_input_bound(input_bytes=0, images_wh=[(1024, 1024)]) - pricing.TEXT_OVERHEAD_TOKENS
    assert one == 37 * 37


def test_text_reservation_is_the_full_output_cap_plus_the_input_bound() -> None:
    price = pricing.parse_table({"entries": [{"provider": "a", "model": "m", "unit": "per_mtok", "input_usd": "2",
                                               "output_usd": "10", "source": "s", "verified_at": "2026-01-01"}]})[("a", "m")]  # fmt: skip
    est = pricing.text_estimate(price, input_bytes=9_000, images_wh=[(1024, 1024)], max_tokens=16_000)
    tokens_in = 9_000 + 37 * 37 + pricing.TEXT_OVERHEAD_TOKENS
    assert est == (Decimal(tokens_in) * 2 + Decimal(16_000) * 10) / Decimal(1_000_000)


async def test_structured_call_reserves_schema_and_labels_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reservation counts the system prompt, user text, image labels and the output schema
    (structured outputs send it), all as UTF-8 bytes."""
    from storypanel.domain.schemas import ModerationVerdict
    from storypanel.providers.base import ImageInput

    seen: list[Decimal] = []
    real = metering._begin_attempt

    async def spy(ctx: CallContext, **kw: Any) -> Any:
        seen.append(kw["estimate"])
        return await real(ctx, **kw)

    monkeypatch.setattr(metering, "_begin_attempt", spy)
    async with transaction() as db:
        u = User(id=uuid.uuid4(), auth_subject=f"t:{uuid.uuid4().hex}")
        db.add(u)
    ctx = CallContext("moderation", str(u.id), str(uuid.uuid4()))
    system, user, label = "sÿstem", "ユーザー" * 10, "Panel to check:"
    await metering.structured(role="vision", system=system, user=user, schema=ModerationVerdict, ctx=ctx,
                              images=(ImageInput(_png(100, 30), label=label),))  # fmt: skip
    import json

    schema_bytes = len(json.dumps(ModerationVerdict.model_json_schema()).encode())
    in_bytes = len(system.encode()) + len(user.encode()) + len(label.encode()) + schema_bytes
    price = pricing.require("fake", "fake-vision")
    assert seen == [
        pricing.text_estimate(price, input_bytes=in_bytes, images_wh=[(100, 30)], max_tokens=16_000)
    ]


# ------------------------------------------------------------------ compute-time billing
@pytest.fixture
def compute_billed_upscale(monkeypatch: pytest.MonkeyPatch) -> Any:
    table = {"entries": [dict(e) for e in pricing.SIMULATED["entries"]]}
    for e in table["entries"]:
        if e["model"] == "fake-upscale":
            e.update(unit="per_compute_second", usd="0.00111")
    monkeypatch.setattr(pricing, "SIMULATED", table)
    pricing.reset_cache()
    fake.control.reset()
    yield
    fake.control.reset()
    monkeypatch.undo()
    pricing.reset_cache()


async def _ctx() -> CallContext:
    async with transaction() as db:
        u = User(id=uuid.uuid4(), auth_subject=f"t:{uuid.uuid4().hex}")
        db.add(u)
    return CallContext("export_upscale", str(u.id), str(uuid.uuid4()))


async def _rows(pid: str | None) -> list[ProviderAttempt]:
    async with transaction() as db:
        return list((await db.execute(select(ProviderAttempt).where(
            ProviderAttempt.project_id == uuid.UUID(pid)).order_by(ProviderAttempt.attempt_no))).scalars())  # fmt: skip


@pytest.mark.usefixtures("fal_voyage")
async def test_compute_billed_request_is_cancelled_once_its_reserved_time_is_used(
    compute_billed_upscale: Any,
) -> None:
    """ESRGAN is billed per compute second. The reservation holds rate x budget; once that much
    time has passed the request is cancelled and the attempt closed, instead of being resumed by
    activity retries for up to the 40-minute schedule-to-close window."""
    ctx = await _ctx()
    fake.control.pending_upscale_fetches = 99  # the job never finishes
    key = f"up:{ctx.project_id}"
    with pytest.raises(AppError) as first:
        await metering.upscale_image(_png(32, 24), ctx, key=key, wait_seconds=0.3)
    assert first.value.retryable  # budget not used up yet: resume later
    await asyncio.sleep(0.4)
    with pytest.raises(AppError) as second:
        await metering.upscale_image(_png(32, 24), ctx, key=key, wait_seconds=0.3)
    assert second.value.code is ErrorCode.PROVIDER_ERROR and not second.value.retryable
    assert second.value.meta.get("phase") == "time_budget"
    assert len(fake.control.cancels) == 1  # cancelled at the provider through the attempt's endpoint
    (row,) = await _rows(ctx.project_id)
    assert row.status == "cancelled" and row.cost_basis == "unresolved"  # still a potential charge
    assert row.error_code == "time_budget_exceeded"
    assert row.cost_counted_usd == Decimal("0.000333")  # 0.3 s x $0.00111, held until a charge is imported
    assert len([s for s in fake.control.models_used if s == "fake-upscale"]) == 1  # never resubmitted


@pytest.mark.usefixtures("fal_voyage")
async def test_reconciliation_cancels_an_abandoned_compute_billed_request(
    compute_billed_upscale: Any,
) -> None:
    ctx = await _ctx()
    fake.control.pending_upscale_fetches = 99
    with pytest.raises(AppError):
        await metering.upscale_image(_png(32, 24), ctx, key=f"up:{ctx.project_id}", wait_seconds=0.2)
    await asyncio.sleep(0.3)  # the worker went away; nobody resumes it
    out = await metering.reconcile_open_requests(older_than=timedelta(0))
    assert out["cancelled_over_budget"] == 1
    assert len(fake.control.cancels) == 1
    (row,) = await _rows(ctx.project_id)
    assert (row.status, row.cost_basis, row.error_code) == ("cancelled", "unresolved", "time_budget_exceeded")


# ------------------------------------------------------------------ fal add-ons
@pytest.mark.parametrize("endpoint", ["fal-ai/nano-banana-2", "fal-ai/nano-banana-2/edit"])
def test_nano_banana_paid_add_ons_are_explicitly_off(endpoint: str) -> None:
    """fal lists web search (+$0.015) and thinking (+$0.002) as add-ons billed per request. The
    request turns web search off and pins the lowest thinking level instead of relying on defaults."""
    profile = fal_profiles.PROFILES[endpoint]
    urls = ["https://example.com/a.png"] if profile.reference_field else []
    args = fal_image.build_arguments(profile, ImageRequest("p", "4:3", seed=1), reference_urls=urls,
                                     image_url=None, mask_url=None)  # fmt: skip
    assert args["enable_web_search"] is False
    assert args["thinking_level"] == "minimal"
    assert args["num_images"] == 1 and args["resolution"] == "1K"
