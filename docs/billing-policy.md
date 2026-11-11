# Credits and billing policy (as implemented)

Credits are the customer-facing unit. Provider cost in USD is tracked separately (`provider_usage`) and never shown to users.
Prices are configuration (`SP_CREDITS_*`); the numbers below are the defaults.

| Work | Credits | Held when | Charged when |
|---|---|---|---|
| Cast and reference sheets | 6 | comic is created | cast is ready for review |
| Story and script | 2 | comic is created | script is ready for review |
| Each panel | 1 | script is approved (all panels) | that panel is delivered with a visible image |
| Regenerate a panel | 1 | on request | only if it produces a visible image |
| Fix by instruction | 1 | on request | only if it produces a visible image |
| Character revision | 1 | on request | only if the new sheet is accepted |
| Export | 0 | – | – |

**Never charged:** automatic QA retries; panels that fail or are safety-blocked; rejected revisions and edits;
anything after a cancellation; work a provider outage prevented.

**Cancellation and failure.** Delivered steps stay charged; every open hold on the comic is released.
**Resume.** Holds are taken again for what remains. Charges carry dedupe keys, so finished work is never charged twice.
**Insufficient credits.** The request is refused with `insufficient_credits` (HTTP 402) before anything starts.
**Late results.** If you restore another version while a redraw is running, the redraw lands in history without replacing your choice. It is still charged, because it was delivered and can be restored.
**Refunds.** Refunding a credit pack removes the refunded share of its credits, but never below what running comics hold. Packs are matched to refunds by payment intent (`credit_purchases`). A refund that matches no pack (for example a subscription invoice) is **not** clawed back automatically: it is logged as `refund_needs_review` and counted in `sp_billing_anomalies_total{kind="refund_needs_review"}` for support.
**Plans.** Free accounts get a monthly grant on first sign-in of the month (after the first 28 days). Paid plans follow the entitlement rules below.

## Subscription entitlements (as implemented, `services/billing.py`)

Plan credits are an entitlement **per subscription billing period**, recorded in `subscription_entitlements`
with a unique `(subscription, period start)`. Grants are keyed by that period, never by invoice.

| Situation | What happens |
|---|---|
| First purchase / renewal | The first *paid* invoice whose non-proration line for a plan price covers a period grants that plan's monthly allowance for the period. Any further invoice for the same period grants nothing. |
| Upgrade mid-period | When a paid invoice carries a proration debit line for a higher plan, the current period is topped up to the higher allowance (creator→pro: +160). If Stripe defers the prorations to the next invoice, the top-up happens when that invoice is paid. |
| Downgrade mid-period | No clawback. The next period is granted at the lower allowance. |
| Trials, 100 % discounts, zero-value invoices | Nothing granted (`amount_paid` must be > 0). The plan's features apply while the subscription is `trialing`. |
| Payment failed / delayed | Nothing granted until `invoice.paid`; then granted once for that period. While `past_due` the plan is kept; `unpaid`/`canceled` drop to free. |
| Cancellation | At period end (Stripe's default) the plan stays until `customer.subscription.deleted`; credits already granted are kept, because they were paid for. |
| Refund of a subscription invoice | Handled by support (see Refunds). |
| Multiple subscriptions | Checkout refuses a second subscription while one is live (plan changes go through the billing portal). If two exist anyway (e.g. created in the dashboard), each grants for its own periods, the user gets the best plan, and `sp_billing_anomalies_total{kind="multiple_live_subscriptions"}` alerts support. |
| Duplicate / out-of-order / equal-timestamp events | Event ids are processed once. Every subscription event triggers a fetch of the subscription's *current* state from Stripe, so delivery order does not matter; subscriptions are also re-read hourly. (Without API access — local development only — events are ordered by timestamp and, at equal timestamps, a terminal status wins.) |
| Checkout retried | The client's `Idempotency-Key` is forwarded, so the same click yields the same Checkout Session. |

All payloads are read with the pinned API version `SP_STRIPE_API_VERSION` (default `2026-10-28.endive`, the
version of the installed SDK). The webhook endpoint in the Stripe dashboard must use the same version; events
in any other version are refused with HTTP 500 so Stripe retries them after the configuration is fixed.
Field paths are checked against the SDK's generated types (`tests/test_billing.py`); they have **not** yet been
checked against payloads captured from a real Stripe test-mode account (blocker B3).

## Guarantees and their limits
- A balance cannot be overspent by concurrent requests: each hold is one guarded `UPDATE`.
- The ledger is append-only at the database level and reconciled against balances every minute.
- Payment webhooks are signature-checked, deduplicated by event id, and order-safe for subscriptions.
- We do **not** guarantee that a provider bills us exactly once per image. See decisions.md C5 and D-COST.
- Provider spend (USD) is reserved before each call and capped per comic, per user per day and globally per day.
  This is separate from credits: hitting a spend ceiling stops generation without charging credits for undelivered work.

Indicative plan sizes and prices in the product document are unvalidated. Review the terms a "commercial-use
licence" would need with counsel before offering one; the product does not currently grant or advertise one.
