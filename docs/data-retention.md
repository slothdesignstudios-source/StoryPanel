# Data deletion and retention

Implemented in `services/backend/src/storypanel/services/deletion.py` (account deletion),
`storage.delete_keys` and `services/maintenance.py` (verified object deletion).

## Account deletion

Triggered by `DELETE /v1/me` (the user) or Clerk's `user.deleted` webhook. One durable job
per account (`account_deletions`), advanced inline and then by the maintenance loop until it
completes. Every step is idempotent; a failing step is retried with backoff and later steps
wait for it.

| Step | What happens | Completes when |
|---|---|---|
| request | `users.deleted_at` set in the same transaction as the job row. Every API call with any token for this account now fails (401); a DB trigger refuses new projects, assets and exports for the owner. | immediately |
| cancelling | Pending workflow starts/signals for the owner are withdrawn; every project, panel and export workflow is cancelled through the outbox; queued/running exports are marked cancelled. | cancels are queued |
| draining | Waits for paid provider calls in flight (up to `SP_DELETION_DRAIN_SECONDS`, default 600 s), then cancels them at the provider through each attempt's own endpoint. Their cost stays counted as an unresolved potential charge for reconciliation. | no open provider attempts |
| purging | Projects and account-level assets deleted; for versioned buckets every version is deleted. Keys S3 did not confirm are recorded in `pending_object_deletions` and retried. | zero asset rows **and** zero unconfirmed object deletions for the owner |
| billing | Every live Stripe subscription of the customer is cancelled immediately (`prorate=false`, `invoice_now=false`): no refund of the current period, no final invoice. Stripe identifiers stay on the job until this succeeds. | Stripe confirms (or the subscription is already cancelled) |
| identity | The Clerk user is deleted (`DELETE /v1/users/{id}` with `SP_CLERK_SECRET_KEY`); skipped when Clerk told us it was deleted, and in local dev auth. | Clerk returns 200/404 |
| anonymising | User row: email, auth subject and Stripe ids removed; idempotency keys removed; the job keeps only hashes and timestamps. | always |

Re-authentication: tokens issued before the deletion request are rejected for good (the job
keeps an HMAC of the auth subject). A sign-in issued after deletion completed creates a new,
empty account. The welcome grant is recorded per HMAC'd subject and email
(`signup_grants`), so deleting and re-creating an account never grants it twice.

## What is retained, and why

| Record | Contents after deletion | Reason | Retention |
|---|---|---|---|
| `credit_ledger` | amounts, kinds, internal user id | financial record, audit of purchases and refunds | 7 years (operator to confirm with legal review, B8) |
| `provider_attempts` / `provider_usage` | costs, models, provider request ids, internal ids | cost accounting and provider reconciliation | 7 years (as above) |
| `audit_log` | action, pseudonymous user ref, request id | security audit | 1 year |
| `account_deletions` | HMAC of auth subject, status, timestamps | prove deletion happened; block pre-deletion tokens | indefinite (no personal data) |
| `signup_grants` | HMACs of subject / normalised email | prevent repeat welcome grants | indefinite (no recoverable personal data) |
| `webhook_events` | Stripe/Clerk event ids and types | webhook idempotency | 90 days |
| Stripe customer | held by Stripe | Stripe's own tax/accounting obligations | per Stripe |

HMACs use `SP_PSEUDONYM_SALT`; rotating it breaks the welcome-grant and token-block checks
for earlier deletions, so rotate only with a re-keying plan.

Not yet automated: the 7-year/1-year/90-day expiries above are policy statements; no job
enforces them yet. The retention periods need legal sign-off (blockers, B8).
