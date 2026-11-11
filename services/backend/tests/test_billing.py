"""Subscription entitlements and Stripe payload handling (review finding 8).

Event payloads follow the field layout of the pinned API version (Settings.stripe_api_version,
the version the installed stripe SDK's generated types describe). `test_payload_paths_exist_in_pinned_sdk_types`
checks every field path the handler reads against those generated types, so fixtures cannot
drift from the schema silently. They are still not captured from a real Stripe account (B3).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from typing import Any

import httpx
from sqlalchemy import func, select

from storypanel.db.models import CreditLedger, User
from storypanel.db.session import transaction

PERIOD = (1_790_000_000, 1_792_592_000)  # one monthly period
NEXT = (1_792_592_000, 1_795_270_400)


def _sign(payload: bytes, secret: str = "whsec_test_secret") -> str:
    ts = int(time.time())
    return f"t={ts},v1={hmac.new(secret.encode(), f'{ts}.'.encode() + payload, hashlib.sha256).hexdigest()}"


async def hook(client: Any, type_: str, obj: dict[str, Any], *, created: int = 0, eid: str | None = None,
               api_version: str = "2026-10-28.endive") -> httpx.Response:  # fmt: skip
    event = {"id": eid or f"evt_{uuid.uuid4().hex[:12]}", "object": "event", "type": type_, "api_version": api_version,
             "created": created or int(time.time()), "data": {"object": obj}}  # fmt: skip
    payload = json.dumps(event).encode()
    return await client.post(
        "/webhooks/stripe", content=payload, headers={"stripe-signature": _sign(payload)}
    )


def line(
    price: str, period: tuple[int, int], amount: int, *, proration: bool = False, sub: str = "sub_1"
) -> dict[str, Any]:
    return {"id": f"il_{uuid.uuid4().hex[:8]}", "object": "line_item", "amount": amount,
            "period": {"start": period[0], "end": period[1]},
            "pricing": {"type": "price_details", "price_details": {"price": price, "product": "prod_x"}},
            "parent": {"type": "subscription_item_details", "subscription_item_details": {
                "subscription": sub, "subscription_item": "si_1", "proration": proration,
                "invoice_item": None, "proration_details": None}}}  # fmt: skip


def invoice(inv_id: str, reason: str, lines: list[dict[str, Any]], *, paid: int | None = None, sub: str = "sub_1",
            customer: str = "cus_1") -> dict[str, Any]:  # fmt: skip
    return {"id": inv_id, "object": "invoice", "customer": customer, "billing_reason": reason, "status": "paid",
            "amount_paid": sum(x["amount"] for x in lines) if paid is None else paid,
            "total": sum(x["amount"] for x in lines),
            "parent": {"type": "subscription_details", "subscription_details": {"subscription": sub, "metadata": {}}},
            "lines": {"object": "list", "has_more": False, "data": lines}}  # fmt: skip


async def user_with_customer(client: Any, headers: dict[str, str], customer: str = "cus_1") -> uuid.UUID:
    from sqlalchemy import update

    uid = uuid.UUID((await client.get("/v1/me", headers=headers)).json()["id"])
    async with transaction() as db:
        await db.execute(update(User).where(User.id == uid).values(stripe_customer_id=customer))
    return uid


async def plan_credits(uid: uuid.UUID) -> int:
    async with transaction() as db:
        return int((await db.execute(select(func.coalesce(func.sum(CreditLedger.delta), 0)).where(
            CreditLedger.user_id == uid, CreditLedger.kind == "grant",
            CreditLedger.reason.like("%plan%")))).scalar_one())  # fmt: skip


# ------------------------------------------------------------------ reproductions (pre-fix)
async def test_two_invoices_in_one_period_grant_one_allowance(client: Any, headers: dict[str, str]) -> None:
    """Review finding 8: a renewal plus another qualifying invoice in the same period."""
    uid = await user_with_customer(client, headers)
    await hook(
        client, "invoice.paid", invoice("in_a", "subscription_cycle", [line("price_pro", PERIOD, 2900)])
    )
    await hook(
        client, "invoice.paid", invoice("in_b", "subscription_update", [line("price_pro", PERIOD, 2900)])
    )
    assert await plan_credits(uid) == 240  # pro allowance once


async def test_subscription_refund_is_not_treated_as_a_credit_pack(
    client: Any, headers: dict[str, str]
) -> None:
    """Charges carry no `invoice` field in the pinned API version."""
    uid = await user_with_customer(client, headers)
    await hook(
        client, "invoice.paid", invoice("in_a", "subscription_create", [line("price_pro", PERIOD, 2900)])
    )
    before = (await client.get("/v1/me", headers=headers)).json()["balance"]
    await hook(client, "charge.refunded", {"id": "ch_sub", "object": "charge", "customer": "cus_1", "amount": 2900,
                                           "amount_refunded": 2900, "payment_intent": "pi_sub", "refunded": True})  # fmt: skip
    assert (await client.get("/v1/me", headers=headers)).json()["balance"] == before
    assert uid


# ------------------------------------------------------------------ entitlement policy
async def _status(client: Any, headers: dict[str, str]) -> dict[str, Any]:
    return (await client.get("/v1/credits", headers=headers)).json()  # type: ignore[no-any-return]


def sub_obj(status: str, price: str = "price_pro", sub: str = "sub_1", period: tuple[int, int] = PERIOD,
            uid: uuid.UUID | None = None) -> dict[str, Any]:  # fmt: skip
    return {"id": sub, "object": "subscription", "customer": "cus_1", "status": status, "cancel_at_period_end": False,
            "metadata": {"user_id": str(uid)} if uid else {},
            "items": {"object": "list", "data": [{"id": "si_1", "object": "subscription_item", "price": {"id": price},
                      "current_period_start": period[0], "current_period_end": period[1]}]}}  # fmt: skip


async def test_purchase_renewal_upgrade_downgrade(client: Any, headers: dict[str, str]) -> None:
    uid = await user_with_customer(client, headers)
    r = await hook(
        client, "invoice.paid", invoice("in_1", "subscription_create", [line("price_creator", PERIOD, 900)])
    )
    assert r.json() == {"status": "credits_granted"} and await plan_credits(uid) == 80
    # redelivery under a new event id, and a second invoice for the same period: nothing more
    assert (await hook(client, "invoice.paid", invoice("in_1", "subscription_create",
                                                       [line("price_creator", PERIOD, 900)]))).json()["status"] == "already_granted"  # fmt: skip
    # mid-period upgrade, prorations invoiced immediately: credit line for creator, debit for pro
    up = invoice("in_up", "subscription_update", [line("price_creator", (PERIOD[0] + 86400 * 10, PERIOD[1]), -600, proration=True),
                                                  line("price_pro", (PERIOD[0] + 86400 * 10, PERIOD[1]), 1300, proration=True)])  # fmt: skip
    assert (await hook(client, "invoice.paid", up)).json()["status"] == "credits_granted"
    assert await plan_credits(uid) == 240  # topped up to the pro allowance for this period (80 + 160)
    # renewal: a new period at the pro allowance
    await hook(client, "invoice.paid", invoice("in_2", "subscription_cycle", [line("price_pro", NEXT, 2900)]))
    assert await plan_credits(uid) == 480
    # downgrade mid-period: no clawback; the next period is granted at the lower allowance
    down = invoice("in_down", "subscription_update", [line("price_pro", (NEXT[0] + 5, NEXT[1]), -2000, proration=True),
                                                      line("price_creator", (NEXT[0] + 5, NEXT[1]), 600, proration=True)])  # fmt: skip
    await hook(client, "invoice.paid", down)
    assert await plan_credits(uid) == 480
    third = (NEXT[1], NEXT[1] + 2_600_000)
    await hook(
        client, "invoice.paid", invoice("in_3", "subscription_cycle", [line("price_creator", third, 900)])
    )
    assert await plan_credits(uid) == 560


async def test_deferred_upgrade_prorations_on_the_next_invoice(client: Any, headers: dict[str, str]) -> None:
    uid = await user_with_customer(client, headers)
    await hook(
        client, "invoice.paid", invoice("in_1", "subscription_create", [line("price_creator", PERIOD, 900)])
    )
    # Stripe's default proration: the upgrade's prorations ride on the next cycle invoice
    nxt = invoice("in_2", "subscription_cycle", [
        line("price_creator", (PERIOD[0] + 100, PERIOD[1]), -600, proration=True),
        line("price_pro", (PERIOD[0] + 100, PERIOD[1]), 1300, proration=True),
        line("price_pro", NEXT, 2900)])  # fmt: skip
    await hook(client, "invoice.paid", nxt)
    assert await plan_credits(uid) == 80 + 160 + 240


async def test_trial_zero_value_and_failed_payments_grant_nothing(
    client: Any, headers: dict[str, str]
) -> None:
    uid = await user_with_customer(client, headers)
    trial = invoice("in_t", "subscription_create", [line("price_pro", PERIOD, 0)], paid=0)
    assert (await hook(client, "invoice.paid", trial)).json()["status"] == "zero_value"
    failed = invoice("in_f", "subscription_cycle", [line("price_pro", NEXT, 2900)], paid=0)
    assert (await hook(client, "invoice.payment_failed", failed)).json()["status"] == "payment_failed"
    assert await plan_credits(uid) == 0
    # the retried payment succeeds days later: granted then, once
    assert (await hook(client, "invoice.paid", invoice("in_f", "subscription_cycle",
                                                       [line("price_pro", NEXT, 2900)]))).json()["status"] == "credits_granted"  # fmt: skip
    assert await plan_credits(uid) == 240


async def test_subscription_state_ordering_without_api_access(client: Any, headers: dict[str, str]) -> None:
    uid = await user_with_customer(client, headers)
    r = await hook(client, "customer.subscription.updated", sub_obj("active", uid=uid), created=200)
    assert r.json()["status"] == "subscription_updated" and (await _status(client, headers))["plan"] == "pro"
    assert (
        await hook(client, "customer.subscription.updated", sub_obj("past_due", uid=uid), created=150)
    ).json()["status"] == "stale"
    # equal timestamps: a non-terminal status does not overwrite; a terminal one does
    assert (
        await hook(client, "customer.subscription.updated", sub_obj("incomplete", uid=uid), created=200)
    ).json()["status"] == "stale"
    assert (
        await hook(client, "customer.subscription.deleted", sub_obj("canceled", uid=uid), created=200)
    ).json()["status"] == "subscription_updated"
    out = await _status(client, headers)
    assert out["plan"] == "free" and out["subscription_status"] == "canceled"


async def test_refunds_claw_back_only_known_credit_packs(client: Any, headers: dict[str, str]) -> None:
    uid = await user_with_customer(client, headers)
    pack = {"id": "cs_pack", "object": "checkout.session", "mode": "payment", "payment_status": "paid",
            "customer": "cus_1", "payment_intent": "pi_pack", "amount_total": 1000, "currency": "usd",
            "client_reference_id": str(uid), "metadata": {"user_id": str(uid), "product": "credit_pack"}}  # fmt: skip
    assert (await hook(client, "checkout.session.completed", pack)).json()["status"] == "credits_granted"
    base = (await _status(client, headers))["balance"]
    charge = lambda refunded: {
        "id": "ch_pack",
        "object": "charge",
        "customer": "cus_1",
        "amount": 1000,  # noqa: E731
        "amount_refunded": refunded,
        "payment_intent": "pi_pack",
    }
    await hook(client, "charge.refunded", charge(500))
    await hook(client, "charge.refunded", charge(500))  # redelivered under a new id: applied once
    assert (await _status(client, headers))["balance"] == base - 20
    await hook(client, "charge.refunded", charge(1000))
    assert (await _status(client, headers))["balance"] == base - 40
    unknown = {"id": "ch_other", "object": "charge", "customer": "cus_1", "amount": 2900, "amount_refunded": 2900,
               "payment_intent": "pi_sub_invoice"}  # fmt: skip
    assert (await hook(client, "charge.refunded", unknown)).json()["status"] == "needs_review"
    assert (await _status(client, headers))["balance"] == base - 40


async def test_duplicate_events_and_api_version_mismatch(client: Any, headers: dict[str, str]) -> None:
    uid = await user_with_customer(client, headers)
    ev = invoice("in_1", "subscription_create", [line("price_pro", PERIOD, 2900)])
    assert (await hook(client, "invoice.paid", ev, eid="evt_same")).json()["status"] == "credits_granted"
    assert (await hook(client, "invoice.paid", ev, eid="evt_same")).json()["status"] == "duplicate"
    # an event in another API version is refused (Stripe retries) and not marked processed
    other = invoice("in_9", "subscription_cycle", [line("price_pro", NEXT, 2900)])
    r = await hook(client, "invoice.paid", other, eid="evt_v", api_version="2024-06-20")
    assert r.status_code == 500
    assert (await hook(client, "invoice.paid", other, eid="evt_v")).json()["status"] == "credits_granted"
    assert await plan_credits(uid) == 480


async def test_payload_paths_exist_in_pinned_sdk_types() -> None:
    """Every field path the handler reads exists in the stripe SDK's generated types for the
    pinned API version, so a version bump that moves a field fails here first."""
    import stripe

    from storypanel.config import get_settings

    assert stripe.api_version == get_settings().stripe_api_version

    def fields(cls: Any) -> set[str]:
        return {k for c in cls.__mro__ for k in getattr(c, "__annotations__", {})}

    def has(cls: Any, path: str) -> None:
        for part in path.split("."):
            assert part in fields(cls), f"{cls.__name__} has no field {part!r} in {stripe.api_version}"
            inner = getattr(cls, "".join(w.title() for w in part.split("_")), None)
            cls = inner if isinstance(inner, type) else cls

    from stripe import Charge, Invoice, InvoiceLineItem, Subscription, SubscriptionItem
    from stripe.checkout import Session

    for cls, path in [
        (Invoice, "billing_reason"), (Invoice, "amount_paid"), (Invoice, "customer"), (Invoice, "lines"),
        (Invoice, "parent.subscription_details.subscription"),
        (InvoiceLineItem, "amount"), (InvoiceLineItem, "period.start"), (InvoiceLineItem, "period.end"),
        (InvoiceLineItem, "pricing.price_details.price"),
        (InvoiceLineItem, "parent.subscription_item_details.proration"),
        (Subscription, "status"), (Subscription, "items"), (Subscription, "cancel_at_period_end"),
        (Subscription, "customer"), (Subscription, "metadata"),
        (SubscriptionItem, "price"), (SubscriptionItem, "current_period_start"), (SubscriptionItem, "current_period_end"),
        (Charge, "amount"), (Charge, "amount_refunded"), (Charge, "payment_intent"), (Charge, "customer"),
        (Session, "mode"), (Session, "payment_status"), (Session, "payment_intent"), (Session, "subscription"),
        (Session, "metadata"), (Session, "client_reference_id"), (Session, "amount_total"),
    ]:  # fmt: skip
        has(cls, path)
    # and the fields the previous implementation read are indeed absent in this version
    assert "price" not in fields(InvoiceLineItem)
    assert "invoice" not in fields(Charge)
