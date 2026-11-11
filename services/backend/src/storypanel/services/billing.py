"""Stripe integration: Checkout, the customer portal and verified, idempotent webhooks.

All requests and payloads use one pinned API version (`SP_STRIPE_API_VERSION`); field paths
follow the installed SDK's generated types for that version. Events in another version are
refused so they are retried after the configuration is fixed, never misread.

Webhook rules:
- the signature is verified against the raw body before anything is parsed;
- each Stripe event id is processed at most once (unique row, same transaction as the work);
- subscription state is fetched from Stripe's API on every subscription event (authoritative,
  so delivery order and equal timestamps do not matter) and reconciled periodically;
- plan credits are an *entitlement per subscription billing period* (docs/billing-policy.md):
  granted once per (subscription, period start) on the first paid invoice for that period,
  topped up on a paid upgrade, never granted for zero-value invoices, prorations alone or
  failed payments;
- credit packs are recorded as purchases; refunds claw back only refunds of a known
  purchase (matched by payment intent). Subscription refunds are left to support.
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
from storypanel.db.models import (
    BillingSubscription,
    CreditLedger,
    CreditPurchase,
    SubscriptionEntitlement,
    User,
    WebhookEvent,
)  # fmt: skip
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
    return stripe.StripeClient(s.stripe_secret_key.get_secret_value(), stripe_version=s.stripe_api_version,
                               max_network_retries=2)  # fmt: skip


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


LIVE_STATUSES = ("active", "trialing", "past_due", "unpaid", "incomplete", "paused")
PLAN_STATUSES = ("active", "trialing", "past_due")  # statuses that keep the paid plan
_RANK = {"free": 0, "creator": 1, "pro": 2}


async def create_checkout(
    db: AsyncSession, user_id: uuid.UUID, product: str, request_key: str | None = None
) -> str:
    s = get_settings()
    price = {"creator": s.stripe_price_creator, "pro": s.stripe_price_pro,
             "credit_pack": s.stripe_price_credit_pack}[product]  # fmt: skip
    if price is None:
        raise AppError(ErrorCode.INVALID_STATE, "That product isn't available yet.")
    user = (await db.execute(select(User).where(User.id == user_id).with_for_update())).scalar_one()
    mode = "payment" if product == "credit_pack" else "subscription"
    if mode == "subscription":
        live = (await db.execute(select(BillingSubscription.id).where(
            BillingSubscription.user_id == user_id, BillingSubscription.status.in_(LIVE_STATUSES)))).first()  # fmt: skip
        if live is not None or (user.subscription_status or "") in LIVE_STATUSES:
            # Changing plan goes through the billing portal (proration handled by Stripe);
            # a second Checkout would create a second, parallel subscription.
            raise AppError(ErrorCode.CONFLICT, "You already have a plan. Use “Manage billing” to change it.",
                           retryable=False)  # fmt: skip
    customer = await _customer(db, user)
    # The same client request (Idempotency-Key) maps to the same Checkout Session.
    options = {"idempotency_key": f"checkout:{user.id}:{product}:{request_key}"} if request_key else {}
    try:
        session = await _client().v1.checkout.sessions.create_async(options=options, params={
            "mode": mode, "customer": customer, "line_items": [{"price": price, "quantity": 1}],
            "success_url": f"{s.web_base_url}/billing?checkout=success",
            "cancel_url": f"{s.web_base_url}/billing?checkout=cancelled",
            "client_reference_id": str(user.id),
            "metadata": {"user_id": str(user.id), "product": product},
            **({"subscription_data": {"metadata": {"user_id": str(user.id)}}} if mode == "subscription" else {}),
        })  # fmt: skip
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


def _ts(value: Any) -> datetime | None:
    return datetime.fromtimestamp(int(value), UTC) if isinstance(value, int | float) and value else None


def _price_of_line(line: dict[str, Any]) -> str | None:
    pricing = line.get("pricing") or {}
    price = (pricing.get("price_details") or {}).get("price")
    if isinstance(price, dict):  # expanded
        price = price.get("id")
    return price if isinstance(price, str) else None


def _invoice_subscription(inv: dict[str, Any]) -> str | None:
    details = ((inv.get("parent") or {}).get("subscription_details") or {}).get("subscription")
    if isinstance(details, dict):
        details = details.get("id")
    return details if isinstance(details, str) else None


async def _refresh_plan(db: AsyncSession, user: User) -> None:
    """User plan = best plan among subscriptions in a plan-keeping status."""
    subs = (await db.execute(select(BillingSubscription).where(BillingSubscription.user_id == user.id)
                             .execution_options(populate_existing=True))).scalars().all()  # fmt: skip
    keeping = [x for x in subs if x.status in PLAN_STATUSES and x.plan]
    best = max(keeping, key=lambda x: _RANK.get(x.plan or "free", 0), default=None)
    live = [x for x in subs if x.status in LIVE_STATUSES]
    if len(live) > 1:
        metrics.billing_anomalies.labels("multiple_live_subscriptions").inc()
        log.warning("billing_multiple_live_subscriptions", count=len(live))
    latest = best or max(subs, key=lambda x: x.synced_at, default=None)
    user.plan = best.plan if best and best.plan else "free"
    user.subscription_id = latest.id if latest else None
    user.subscription_status = latest.status if latest else None


async def _store_subscription(db: AsyncSession, user: User, sub: dict[str, Any], *, source: str,
                              event_created: datetime | None) -> None:  # fmt: skip
    s = get_settings()
    items = (sub.get("items") or {}).get("data") or []
    price_id = None
    start = end = None
    if items:
        price = items[0].get("price") or {}
        price_id = price.get("id") if isinstance(price, dict) else price
        start, end = _ts(items[0].get("current_period_start")), _ts(items[0].get("current_period_end"))
    values = dict(user_id=user.id, customer_id=sub.get("customer") if isinstance(sub.get("customer"), str) else None,
                  status=str(sub.get("status")), plan=_price_plans(s).get(price_id or ""), price_id=price_id,
                  current_period_start=start, current_period_end=end,
                  cancel_at_period_end=bool(sub.get("cancel_at_period_end")), source=source,
                  event_created=event_created, synced_at=datetime.now(UTC))  # fmt: skip
    await db.execute(insert(BillingSubscription).values(id=str(sub["id"]), **values)
                     .on_conflict_do_update(index_elements=["id"], set_=values))  # fmt: skip
    await _refresh_plan(db, user)


async def sync_subscription(db: AsyncSession, user: User, sub_id: str, payload: dict[str, Any],
                            event_created: datetime) -> str:  # fmt: skip
    """Apply a subscription's state. With API access the current state is fetched from
    Stripe, so stale or equal-timestamp deliveries cannot roll it back. Without it (local
    development only: deployed configs require the key) events are ordered by timestamp,
    and at equal timestamps a terminal status wins."""
    if enabled():
        try:
            current = await _client().v1.subscriptions.retrieve_async(sub_id)
        except stripe.StripeError as exc:
            raise AppError(ErrorCode.PROVIDER_ERROR, "The payment service is unavailable.") from exc
        await _store_subscription(db, user, current.to_dict(), source="api", event_created=event_created)
        return "subscription_synced"
    row = (
        await db.execute(select(BillingSubscription).where(BillingSubscription.id == sub_id))
    ).scalar_one_or_none()
    if row and row.event_created:
        terminal = str(payload.get("status")) in ("canceled", "incomplete_expired")
        if event_created < row.event_created or (event_created == row.event_created and not terminal):
            return "stale"
    await _store_subscription(db, user, payload, source="event", event_created=event_created)
    return "subscription_updated"


async def _grant_entitlements(db: AsyncSession, user: User, inv: dict[str, Any]) -> str:
    s = get_settings()
    sub_id = _invoice_subscription(inv)
    if not sub_id:
        return "ignored"
    if int(inv.get("amount_paid") or 0) <= 0:
        return "zero_value"  # trials and fully-discounted invoices grant nothing
    lines = list(((inv.get("lines") or {}).get("data")) or [])
    if (inv.get("lines") or {}).get("has_more") and enabled():
        try:
            page = await _client().v1.invoices.line_items.list_async(inv["id"], params={"limit": 100})
            lines = (
                [x.to_dict() for x in page.auto_paging_iter()] if hasattr(page, "auto_paging_iter") else lines
            )
        except stripe.StripeError as exc:
            raise AppError(ErrorCode.PROVIDER_ERROR, "The payment service is unavailable.") from exc
    plans = _price_plans(s)
    outcome = "ignored"
    for ln in sorted(
        lines,
        key=lambda x: bool(((x.get("parent") or {}).get("subscription_item_details") or {}).get("proration")),
    ):
        details = (ln.get("parent") or {}).get("subscription_item_details") or {}
        plan = plans.get(_price_of_line(ln) or "")
        period = ln.get("period") or {}
        start, end = _ts(period.get("start")), _ts(period.get("end"))
        if not plan or start is None or end is None or int(ln.get("amount") or 0) <= 0:
            continue  # unknown price, credit proration, or free line
        allowance = s.plan_monthly_credits.get(plan, 0)
        existing = (await db.execute(select(SubscriptionEntitlement).where(
            SubscriptionEntitlement.subscription_id == sub_id, SubscriptionEntitlement.period_start <= start,
            SubscriptionEntitlement.period_end > start).with_for_update())).scalar_one_or_none()  # fmt: skip
        if existing is None:
            if details.get("proration"):
                continue  # a proration alone never opens a period; the period's own line does
            ent = SubscriptionEntitlement(subscription_id=sub_id, user_id=user.id, period_start=start,
                                          period_end=end, plan=plan, credits_granted=0, invoices=[])  # fmt: skip
            db.add(ent)
            await db.flush()
        else:
            ent = existing
        top_up = allowance - ent.credits_granted
        if inv["id"] not in (ent.invoices or []):
            ent.invoices = [*(ent.invoices or []), inv["id"]]
        if top_up <= 0:
            outcome = "already_granted" if outcome == "ignored" else outcome
            continue  # downgrade or repeat: no clawback mid-period, no second grant
        key = f"entitlement:{sub_id}:{int(ent.period_start.timestamp())}:{plan}"
        if await credits.grant(db, user.id, top_up, kind="grant",
                               reason=f"{plan.title()} plan credits" + (" (upgrade)" if ent.credits_granted else ""),
                               dedupe_key=key):  # fmt: skip
            ent.credits_granted, ent.plan = allowance, plan
            outcome = "credits_granted"
    return outcome


async def handle_event(db: AsyncSession, event: dict[str, Any]) -> str:
    """Process one verified event inside the caller's transaction. Returns the outcome."""
    s = get_settings()
    event_id, type_ = str(event["id"]), str(event["type"])
    if event.get("api_version") and event["api_version"] != s.stripe_api_version:
        metrics.webhook_events.labels(type_, "api_version_mismatch").inc()
        log.error("stripe_api_version_mismatch", received=event["api_version"], expected=s.stripe_api_version)
        raise AppError(ErrorCode.INTERNAL, "Webhook API version does not match the configured version.",
                       retryable=True)  # fmt: skip
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
            pi = obj.get("payment_intent")
            await db.execute(insert(CreditPurchase).values(
                checkout_session_id=obj["id"], user_id=user.id, payment_intent_id=pi if isinstance(pi, str) else None,
                credits=s.credit_pack_size, amount_total=obj.get("amount_total"), currency=obj.get("currency"))
                .on_conflict_do_nothing())  # fmt: skip
            await credits.grant(db, user.id, s.credit_pack_size, kind="purchase",
                                reason="Credit pack", dedupe_key=f"purchase:{obj['id']}")  # fmt: skip
            outcome = "credits_granted"
        elif obj.get("mode") == "subscription" and isinstance(obj.get("subscription"), str):
            outcome = (
                await sync_subscription(db, user, obj["subscription"], {}, created)
                if enabled()
                else "ignored"
            )
    elif type_ == "invoice.paid":
        outcome = await _grant_entitlements(db, user, obj)
    elif type_ == "invoice.payment_failed":
        metrics.billing_anomalies.labels("payment_failed").inc()
        outcome = "payment_failed"  # nothing granted; Stripe's retries / dunning follow
    elif type_ in ("customer.subscription.created", "customer.subscription.updated",
                   "customer.subscription.deleted", "customer.subscription.paused",
                   "customer.subscription.resumed"):  # fmt: skip
        outcome = await sync_subscription(db, user, str(obj["id"]), obj, created)
    elif type_ == "charge.refunded":
        amount, refunded = int(obj.get("amount", 0)), int(obj.get("amount_refunded", 0))
        pi = obj.get("payment_intent")
        purchase = (await db.execute(select(CreditPurchase).where(
            CreditPurchase.payment_intent_id == pi))).scalar_one_or_none() if isinstance(pi, str) else None  # fmt: skip
        if purchase is None:
            # Not a known credit-pack payment (e.g. a subscription invoice): no automatic
            # clawback. Support decides; the event is recorded and alerted on.
            metrics.billing_anomalies.labels("refund_needs_review").inc()
            log.warning("refund_needs_review", charge=obj.get("id"))
            outcome = "needs_review"
        elif amount > 0 and refunded > 0:
            # Claw back the refunded share of that purchase. The key includes the cumulative
            # refunded amount, so partial refunds each apply once.
            target = round(purchase.credits * refunded / amount)
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


async def reconcile_subscriptions(limit: int = 200) -> int:
    """Re-read every live subscription from Stripe (catches missed or failed webhooks)."""
    if not enabled():
        return 0
    from storypanel.db.session import transaction

    async with transaction() as db:
        rows = (await db.execute(select(BillingSubscription.id, BillingSubscription.user_id).where(
            BillingSubscription.status.in_(LIVE_STATUSES)).limit(limit))).all()  # fmt: skip
    n = 0
    for sub_id, uid in rows:
        try:
            async with transaction() as db:
                user = (await db.execute(select(User).where(User.id == uid).with_for_update())).scalar_one()
                await sync_subscription(db, user, sub_id, {}, datetime.now(UTC))
                n += 1
        except AppError:
            log.warning("subscription_reconcile_failed", subscription=sub_id)
    return n


_LIVE_SUBSCRIPTION = ("active", "trialing", "past_due", "unpaid", "incomplete", "paused")


async def cancel_all_subscriptions(*, customer_id: str | None, subscription_ids: list[str],
                                   idempotency_prefix: str) -> int:  # fmt: skip
    """Account deletion: cancel every live subscription of the customer immediately, with
    no proration credit and no final invoice. Raises on any Stripe failure so the caller
    keeps the identifiers and retries; cancelling an already-cancelled one is a no-op."""
    client = _client()
    ids = set(subscription_ids)
    try:
        if customer_id:
            page = await client.v1.subscriptions.list_async(params={"customer": customer_id, "status": "all",
                                                                    "limit": 100})  # fmt: skip
            ids |= {sub.id for sub in page.data if sub.status in _LIVE_SUBSCRIPTION}
        n = 0
        for sub_id in sorted(ids):
            try:
                await client.v1.subscriptions.cancel_async(
                    sub_id, params={"prorate": False, "invoice_now": False},
                    options={"idempotency_key": f"{idempotency_prefix}:cancel:{sub_id}"})  # fmt: skip
                n += 1
            except stripe.InvalidRequestError as exc:
                if "canceled" in str(exc).lower() or getattr(exc, "http_status", None) == 404:
                    continue  # already cancelled or gone: the desired end state
                raise
    except stripe.StripeError as exc:
        raise AppError(ErrorCode.PROVIDER_ERROR, "The payment service is unavailable.") from exc
    return n
