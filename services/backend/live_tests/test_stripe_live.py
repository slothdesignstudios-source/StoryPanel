"""Stripe test mode, end to end: real API calls, real signed webhook deliveries (forwarded to the
local API by `stripe listen --forward-to http://127.0.0.1:8000/webhooks/stripe`), and a test clock
to cross billing periods. Each step asserts on the application's own state (plan, credits ledger,
entitlements), not on Stripe's.

Needs: SP_STRIPE_SECRET_KEY (sk_test_...), SP_STRIPE_WEBHOOK_SECRET (from `stripe listen`),
SP_STRIPE_PRICE_CREATOR, SP_STRIPE_PRICE_PRO, SP_STRIPE_PRICE_CREDIT_PACK, the API running locally
with the same settings, and the webhook endpoint's API version = SP_STRIPE_API_VERSION.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any

import httpx
import pytest

from live_tests.conftest import record, require

API = os.environ.get("SP_SMOKE_API", "http://127.0.0.1:8000")


@pytest.fixture(autouse=True)
def _live() -> None:
    require(
        "stripe",
        "SP_STRIPE_SECRET_KEY",
        "SP_STRIPE_WEBHOOK_SECRET",
        "SP_STRIPE_PRICE_CREATOR",
        "SP_STRIPE_PRICE_PRO",
    )
    assert os.environ["SP_STRIPE_SECRET_KEY"].startswith("sk_test_"), (
        "live billing tests run in test mode only"
    )


async def _until(fn: Any, what: str, timeout: float = 90) -> Any:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = await fn()
        if v:
            return v
        await asyncio.sleep(2)
    raise AssertionError(f"timed out waiting for {what} (is `stripe listen` forwarding to {API}?)")


async def test_subscription_lifecycle_with_real_webhooks() -> None:
    import stripe

    from storypanel.config import get_settings

    s = get_settings()
    sc = stripe.StripeClient(os.environ["SP_STRIPE_SECRET_KEY"], stripe_version=s.stripe_api_version)
    evidence: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=30) as h:
        tok = (
            await h.post(
                f"{API}/v1/dev/login", json={"email": f"stripe-live-{uuid.uuid4().hex[:6]}@example.com"}
            )
        ).json()["token"]
        H = {"Authorization": f"Bearer {tok}"}
        uid = (await h.get(f"{API}/v1/me", headers=H)).json()["id"]

        async def credits() -> dict[str, Any]:
            return (await h.get(f"{API}/v1/credits", headers=H)).json()  # type: ignore[no-any-return]

        # Checkout: our endpoint creates a real session (idempotent per click)
        url1 = (
            await h.post(
                f"{API}/v1/billing/checkout",
                headers={**H, "Idempotency-Key": "c1"},
                json={"product": "creator"},
            )
        ).json()["url"]
        url2 = (
            await h.post(
                f"{API}/v1/billing/checkout",
                headers={**H, "Idempotency-Key": "c1"},
                json={"product": "creator"},
            )
        ).json()["url"]
        assert url1 == url2 and url1.startswith("https://checkout.stripe.com/")
        evidence["checkout_session_idempotent"] = True
        # Completing hosted Checkout needs a browser; the subscription is created through the API
        # on a test clock instead, for the same customer, with Stripe's test card.
        me = await credits()
        clock = sc.v1.test_helpers.test_clocks.create(params={"frozen_time": int(time.time())})
        cust = sc.v1.customers.create(
            params={
                "test_clock": clock.id,
                "metadata": {"user_id": uid},
                "payment_method": "pm_card_visa",
                "invoice_settings": {"default_payment_method": "pm_card_visa"},
            }
        )
        # map the customer to our user exactly as checkout.session.completed would
        from sqlalchemy import update

        from storypanel.db.models import User
        from storypanel.db.session import transaction

        async with transaction() as db:
            await db.execute(update(User).where(User.id == uuid.UUID(uid)).values(stripe_customer_id=cust.id))
        sub = sc.v1.subscriptions.create(
            params={
                "customer": cust.id,
                "items": [{"price": os.environ["SP_STRIPE_PRICE_CREATOR"]}],
                "metadata": {"user_id": uid},
            }
        )
        start = me["balance"]
        c = await _until(lambda: _grown(credits, start), "initial invoice.paid grant")
        evidence["initial_grant"] = c["balance"] - start
        assert c["plan"] == "creator"
        # duplicate delivery: resend the same event; nothing more is granted
        inv = sc.v1.invoices.list(params={"subscription": sub.id, "limit": 1}).data[0]
        ev = next(
            e
            for e in sc.v1.events.list(params={"type": "invoice.paid", "limit": 20}).data
            if e.data.object.id == inv.id
        )
        proc = await asyncio.create_subprocess_exec("stripe", "events", "resend", ev.id,
                                                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)  # fmt: skip
        assert await proc.wait() == 0, "the Stripe CLI must be logged in to the same test account"
        await asyncio.sleep(10)
        assert (await credits())["balance"] == c["balance"]
        evidence["duplicate_delivery_no_regrant"] = True
        # upgrade with immediate proration invoice -> top-up to the pro allowance
        item = sub["items"]["data"][0]["id"]
        sc.v1.subscriptions.update(
            sub.id,
            params={
                "items": [{"id": item, "price": os.environ["SP_STRIPE_PRICE_PRO"]}],
                "proration_behavior": "always_invoice",
            },
        )
        c2 = await _until(lambda: _grown(credits, c["balance"]), "upgrade top-up")
        evidence["upgrade_top_up"] = c2["balance"] - c["balance"]
        # renewal: advance the clock past the period end
        period_end = sc.v1.subscriptions.retrieve(sub.id)["items"]["data"][0]["current_period_end"]
        sc.v1.test_helpers.test_clocks.advance(clock.id, params={"frozen_time": period_end + 3600})
        c3 = await _until(lambda: _grown(credits, c2["balance"]), "renewal grant", timeout=240)
        evidence["renewal_grant"] = c3["balance"] - c2["balance"]
        # downgrade: no clawback
        sc.v1.subscriptions.update(
            sub.id,
            params={
                "items": [{"id": item, "price": os.environ["SP_STRIPE_PRICE_CREATOR"]}],
                "proration_behavior": "create_prorations",
            },
        )
        await asyncio.sleep(15)
        assert (await credits())["balance"] >= c3["balance"]
        evidence["downgrade_no_clawback"] = True
        # refund of a subscription invoice: routed to review, no automatic clawback
        paid = sc.v1.invoices.list(params={"subscription": sub.id, "status": "paid", "limit": 1}).data[0]
        pi = sc.v1.invoice_payments.list(params={"invoice": paid.id}).data[0].payment.payment_intent
        sc.v1.refunds.create(params={"payment_intent": pi})
        await asyncio.sleep(15)
        assert (await credits())["balance"] >= c3["balance"]
        evidence["subscription_refund_needs_review"] = True
        # cancellation: plan drops to free when Stripe deletes the subscription
        sc.v1.subscriptions.cancel(sub.id)
        await _until(lambda: _plan_is(credits, "free"), "cancellation")
        evidence["cancellation_plan_free"] = True
        sc.v1.test_helpers.test_clocks.delete(clock.id)
    record("stripe", "subscription_lifecycle_with_real_webhooks", evidence)


async def _grown(get: Any, than: int) -> Any:
    c = await get()
    return c if c["balance"] > than else None


async def _plan_is(get: Any, plan: str) -> bool:
    return bool((await get())["plan"] == plan)
