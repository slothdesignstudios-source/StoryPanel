"""Stripe integration: Checkout, the customer portal and verified, idempotent webhooks.

Webhook handling rules:
- the signature is verified against the raw body before anything is parsed;
- each Stripe event id is processed at most once (unique row, same transaction as the work);
- credit grants carry their own dedupe keys (invoice / session / refund ids), so two
  different events about the same payment cannot both grant;
- subscription state only moves forward in time (out-of-order deliveries are ignored).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import stripe
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from storypanel.config import Settings, get_settings
from storypanel.db.models import CreditLedger, User, WebhookEvent
from storypanel.errors import AppError, ErrorCode
from storypanel.obs import metrics
from storypanel.obs.logging import get_logger
from storypanel.services import credits

log = get_logger("billing")


def enabled(s: Settings | None = None) -> bool:
    return (s or get_settings()).stripe_secret_key is not None


def _client() -> stripe.StripeClient:
    s = get_settings()
    if s.stripe_secret_key is None:
        raise AppError(ErrorCode.INVALID_STATE, "Billing isn't configured in this environment.")
    return stripe.StripeClient(s.stripe_secret_key.get_secret_value(), max_network_retries=2)


def _price_plans(s: Settings) -> dict[str, str]:
    return {p: plan for p, plan in ((s.stripe_price_creator, "creator"), (s.stripe_price_pro, "pro")) if p}


async def _customer(db: AsyncSession, user: User) -> str:
    if user.stripe_customer_id:
        return user.stripe_customer_id
    try:
        customer = await _client().v1.customers.create_async(
            params={"metadata": {"user_id": str(user.id)}, **({"email": user.email} if user.email else {})},
            options={"idempotency_key": f"customer:{user.id}"})  # fmt: skip
    except stripe.StripeError as exc:
        raise AppError(ErrorCode.PROVIDER_ERROR, "The payment service is unavailable.") from exc
    user.stripe_customer_id = customer.id
    return str(customer.id)


async def create_checkout(db: AsyncSession, user_id: uuid.UUID, product: str) -> str:
    s = get_settings()
    price = {"creator": s.stripe_price_creator, "pro": s.stripe_price_pro,
             "credit_pack": s.stripe_price_credit_pack}[product]  # fmt: skip
    if price is None:
        raise AppError(ErrorCode.INVALID_STATE, "That product isn't available yet.")
    user = (await db.execute(select(User).where(User.id == user_id).with_for_update())).scalar_one()
    customer = await _customer(db, user)
    mode = "payment" if product == "credit_pack" else "subscription"
    try:
        session = await _client().v1.checkout.sessions.create_async(params={
            "mode": mode, "customer": customer, "line_items": [{"price": price, "quantity": 1}],
            "success_url": f"{s.web_base_url}/billing?checkout=success",
            "cancel_url": f"{s.web_base_url}/billing?checkout=cancelled",
            "client_reference_id": str(user.id),
            "metadata": {"user_id": str(user.id), "product": product}})  # fmt: skip
    except stripe.StripeError as exc:
        raise AppError(ErrorCode.PROVIDER_ERROR, "The payment service is unavailable.") from exc
    return str(session.url)


async def create_portal(db: AsyncSession, user_id: uuid.UUID) -> str:
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
    if not user.stripe_customer_id:
        raise AppError(ErrorCode.INVALID_STATE, "You don't have a billing account yet.")
    try:
        session = await _client().v1.billing_portal.sessions.create_async(params={
            "customer": user.stripe_customer_id,
            "return_url": f"{get_settings().web_base_url}/billing"})  # fmt: skip
    except stripe.StripeError as exc:
        raise AppError(ErrorCode.PROVIDER_ERROR, "The payment service is unavailable.") from exc
    return str(session.url)


def verify_webhook(payload: bytes, signature: str | None) -> dict[str, Any]:
    s = get_settings()
    if s.stripe_webhook_secret is None:
        raise AppError(ErrorCode.INVALID_STATE, "Billing isn't configured in this environment.")
    try:
        stripe.WebhookSignature.verify_header(
            payload.decode("utf-8"), signature or "", s.stripe_webhook_secret.get_secret_value(),
            tolerance=300)  # fmt: skip
    except Exception as exc:  # bad signature, stale timestamp, undecodable body
        metrics.webhook_events.labels("unknown", "bad_signature").inc()
        raise AppError(ErrorCode.FORBIDDEN, "Invalid webhook signature.") from exc
    import json

    event: dict[str, Any] = json.loads(payload)
    return event


async def _user_for(db: AsyncSession, obj: dict[str, Any]) -> User | None:
    meta_id = (obj.get("metadata") or {}).get("user_id") or obj.get("client_reference_id")
    if meta_id:
        try:
            user = (await db.execute(select(User).where(User.id == uuid.UUID(meta_id))
                                     .with_for_update())).scalar_one_or_none()  # fmt: skip
            if user:
                return user
        except ValueError:
            pass
    customer = obj.get("customer")
    if isinstance(customer, str):
        return (await db.execute(select(User).where(User.stripe_customer_id == customer)
                                 .with_for_update())).scalar_one_or_none()  # fmt: skip
    return None


async def handle_event(db: AsyncSession, event: dict[str, Any]) -> str:
    """Process one verified event inside the caller's transaction. Returns the outcome."""
    s = get_settings()
    event_id, type_ = str(event["id"]), str(event["type"])
    created = datetime.fromtimestamp(int(event.get("created", 0)), UTC)
    claimed = (await db.execute(
        insert(WebhookEvent).values(provider="stripe", event_id=event_id, type=type_,
                                    event_created=created, processed_at=datetime.now(UTC))
        .on_conflict_do_nothing(index_elements=["provider", "event_id"])
        .returning(WebhookEvent.id))).scalar_one_or_none()  # fmt: skip
    if claimed is None:
        metrics.webhook_events.labels(type_, "duplicate").inc()
        return "duplicate"
    obj: dict[str, Any] = event["data"]["object"]
    user = await _user_for(db, obj)
    outcome = "ignored"
    if user is None:
        log.warning("webhook_user_unknown", type=type_)
    elif type_ == "checkout.session.completed":
        if obj.get("customer") and not user.stripe_customer_id:
            user.stripe_customer_id = obj["customer"]
        if obj.get("mode") == "payment" and obj.get("payment_status") == "paid" \
                and (obj.get("metadata") or {}).get("product") == "credit_pack":  # fmt: skip
            await credits.grant(db, user.id, s.credit_pack_size, kind="purchase",
                                reason="Credit pack", dedupe_key=f"purchase:{obj['id']}")  # fmt: skip
            outcome = "credits_granted"
    elif type_ == "invoice.paid":
        prices = _price_plans(s)
        plan = next((prices[line["price"]["id"]] for line in obj.get("lines", {}).get("data", [])
                     if (line.get("price") or {}).get("id") in prices), None)  # fmt: skip
        if plan:
            await credits.grant(db, user.id, s.plan_monthly_credits.get(plan, 0), kind="grant",
                                reason=f"{plan.title()} plan credits",
                                dedupe_key=f"invoice:{obj['id']}")  # fmt: skip
            outcome = "credits_granted"
    elif type_ in ("customer.subscription.created", "customer.subscription.updated",
                   "customer.subscription.deleted"):  # fmt: skip
        if user.subscription_event_at and created < user.subscription_event_at:
            outcome = "stale"  # an older event arrived late: newer state already applied
        else:
            status = "canceled" if type_.endswith("deleted") else str(obj.get("status"))
            items = obj.get("items", {}).get("data", [])
            price_id = (items[0].get("price") or {}).get("id") if items else None
            plan = _price_plans(s).get(price_id or "", "free")
            user.plan = plan if status in ("active", "trialing", "past_due") else "free"
            user.subscription_id, user.subscription_status = obj.get("id"), status
            user.subscription_event_at = created
            outcome = "subscription_updated"
    elif type_ == "charge.refunded":
        amount, refunded = int(obj.get("amount", 0)), int(obj.get("amount_refunded", 0))
        if amount > 0 and refunded > 0 and not obj.get("invoice"):
            # One-off credit-pack purchase: claw back the refunded share. The key includes
            # the cumulative refunded amount, so partial refunds each apply once.
            target = round(s.credit_pack_size * refunded / amount)
            already = -int((await db.execute(
                select(func.coalesce(func.sum(CreditLedger.delta), 0)).where(
                    CreditLedger.user_id == user.id,
                    CreditLedger.dedupe_key.like(f"refund:{obj['id']}:%")))).scalar_one())  # fmt: skip
            if target > already:
                await credits.grant(db, user.id, -(target - already), kind="refund",
                                    reason="Refund", dedupe_key=f"refund:{obj['id']}:{refunded}")  # fmt: skip
            outcome = "credits_clawed_back"
    metrics.webhook_events.labels(type_, outcome).inc()
    return outcome
