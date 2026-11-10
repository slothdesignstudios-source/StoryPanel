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
**Refunds.** Refunding a credit pack removes the refunded share of its credits, but never below what running comics hold. Subscription refunds are handled manually.
**Plans.** Monthly credits are granted when Stripe reports the invoice paid. Free accounts get a monthly grant on first sign-in of the month (after the first 28 days).

## Guarantees and their limits
- A balance cannot be overspent by concurrent requests: each hold is one guarded `UPDATE`.
- The ledger is append-only at the database level and reconciled against balances every minute.
- Payment webhooks are signature-checked, deduplicated by event id, and order-safe for subscriptions.
- We do **not** guarantee that a provider bills us exactly once per image. See decisions.md C5.

Indicative plan sizes and prices in the product document are unvalidated. Review the terms a "commercial-use
licence" would need with counsel before offering one; the product does not currently grant or advertise one.
