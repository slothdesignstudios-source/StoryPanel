# Runbook: billing mismatch

**Alerts:** `CreditLedgerDrift`, `WebhookSignatureFailures`.

The ledger (`credit_ledger`, append-only) is the record. `users.credit_balance` is a cache of its sum, and
`users.credit_reserved` must equal the open reservations. The maintenance loop checks both every minute.

## Find the drift
```sql
select u.id, u.credit_balance, coalesce(sum(l.delta),0) ledger_total
from users u left join credit_ledger l on l.user_id = u.id group by u.id
having u.credit_balance <> coalesce(sum(l.delta),0);

select u.id, u.credit_reserved, coalesce(sum(r.amount - r.settled),0) open_holds
from users u left join credit_reservations r on r.user_id = u.id and r.status='open' group by u.id
having u.credit_reserved <> coalesce(sum(r.amount - r.settled),0);
```
## Fix
- Never edit or delete ledger rows (the database refuses it). Correct with a new entry:
  `credits.grant(db, user_id, +/-n, kind="adjust", reason="Correction: <ticket>", dedupe_key="adjust:<ticket>")`.
- Balance cache wrong but ledger right → set `credit_balance` to the ledger sum in a transaction, and open an incident: that should be impossible.
- Holds that outlived their comic: `credits.close_for_project(db, project_id)`.

## "I paid but got no credits"
1. `select * from webhook_events where provider='stripe' order by id desc limit 20;` Was the event received?
2. Not received → check the Stripe dashboard's webhook delivery log and resend. Replays are safe: events are
   deduplicated by event id, and grants by invoice/session id.
3. Received with outcome `ignored` → the event had no user (`metadata.user_id` / `client_reference_id` / known customer). Grant manually with an `adjust` entry referencing the Stripe id.
4. `bad_signature` spikes → the webhook secret was rotated in Stripe but not in `SP_STRIPE_WEBHOOK_SECRET`.

## Refunds
`charge.refunded` on a credit pack claws back the refunded share, but never below what running comics hold.
Subscription refunds are not clawed back automatically: decide case by case and record an `adjust`.
