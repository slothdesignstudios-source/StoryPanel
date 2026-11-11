# Remediation tracker — independent review of 46e33b6

Source review: "StoryPanel implementation and production readiness review", 12 Nov 2026.
Each finding was treated as a hypothesis, reproduced with a failing test against the
unchanged code where possible, then fixed. Evidence files are in `docs/evidence/remediation/`.

Status vocabulary: **Fixed, locally verified** (regression test passes against real local
Postgres/Redis/S3-simulation/Temporal), **Fixed, unit-verified** (isolated doubles only),
**Partially fixed**, **Open — external** (needs credentials, accounts or approvals),
**Open**.

## P0 — Paid request recovery and cost accounting

### F1. Resume could fetch/cancel through the wrong endpoint
- **Reproduction:** `tests/test_paid_recovery.py::test_primary_request_resumes_on_primary_after_breaker_opens`,
  `..._fallback_request_resumes_on_fallback_after_breaker_closes`, `..._cancellation_goes_to_the_endpoint_that_accepted_the_request`.
  Against 46e33b6 (with the fake made endpoint-aware, as a real queue is) all three failed: the
  resume fetched `fake-fallback` for a request accepted by `fake-image`, got "unknown request",
  released the accounting and allowed a second paid submission
  (`docs/evidence/remediation/F1-before.txt`).
- **Root cause:** `generate_image` chose the model (breaker) before loading the persisted
  operation and captured it in the fetch/cancel closures; `_paid_request`'s `model = row.model`
  rebound only a local.
- **Fix:** `metering._paid_request` loads the latest persisted attempt first. A resumable attempt
  (`started`, `cancelled`, `succeeded` with a request id) is fetched, cancelled and settled with
  its own persisted provider, model and `price_ref` (`pricing.Price.from_ref`). `choose()` (breaker
  routing + price + estimate) runs only when a new attempt is needed. Fetch/cancel callbacks take
  the model explicitly. Upscaler protocol now also takes the model.
- **Regression tests:** the three above plus `test_upscale_resume_uses_the_persisted_model`;
  assert provider submissions (`fake.control.models_used`), the endpoint of every fetch/cancel,
  and the price used (primary $0.04 vs fallback $0.035).
- **Status:** Fixed, locally verified.

### F2. Transient retrieval failures could trigger another paid generation
- **Reproduction:** `test_status_503_after_acceptance_retries_retrieval_not_generation_v0` (pre-fix
  shape, `F2-F3-before.txt`): a 5xx while polling became `PROVIDER_ERROR`, the attempt was marked
  failed, the retry submitted a second paid request (`assert 2 == 1`).
- **Root cause:** the fal adapter mapped status/result/download transport failures to the same
  `PROVIDER_ERROR` as a provider-reported job failure, and metering treated any fetch error as
  definitive.
- **Fix:** explicit outcome taxonomy in `providers/base.py` and `fal_image.py`:
  `RequestPending` (running), `JobFailed` (only `Completed(error=…)`; moderation flagged),
  `RetrievalFailed` (5xx/429/reset/timeout on status, result or download — id kept, retrieval
  retried), `RequestUnknown` (404 on status or expired result file — *not* proof of no charge:
  the attempt becomes `lost`, counted as `unresolved`), unusable output (`AppError`, charged).
  Unclassified errors during fetch keep the id. Submission timeouts remain `ambiguous` and are
  never resubmitted.
- **Regression tests:** `test_queue_outcomes_are_classified_from_sdk_status` (SDK statuses, HTTP
  404/429/502/503, connect reset, read timeout, download 503/reset/403/404/410 via
  `httpx.MockTransport`); `test_transient_retrieval_failure_retries_retrieval_not_generation`
  (6 fault types × 2 consecutive failures → exactly 1 submission, 3 fetches of 1 id);
  `test_polling_timeout_keeps_the_request_and_resumes`; `test_expired_result_stays_counted_…`;
  `test_worker_crash_during_fetch_resumes_the_same_request`;
  `test_crash_between_acceptance_and_recording_the_id_is_never_resubmitted`;
  `test_cancel_after_completion_is_resolved_by_reconciliation`;
  `test_retry_after_cancellation_collects_the_finished_result_without_paying_again`.
- **Status:** Fixed, locally verified (simulated provider). Live fal behaviour of 404 vs expiry
  is unverified (B1/B2).

### F3. Failed-attempt costs were overwritten on retry
- **Reproduction:** `test_billed_failure_then_success_keeps_both_attempts_counted` (pre-fix):
  counted `0.04` instead of `0.08` after a billed failure and a success.
- **Root cause:** one mutable `provider_usage` row per operation; `_reserve` reopened failed rows
  and overwrote cost and request id.
- **Fix:** `provider_attempts` table (migration `0003`, backfilled from existing rows): one
  immutable row per submission beneath the logical operation. A trigger forbids deletes,
  identity/request-id changes and edits to settled costs. Ceilings sum attempts. Cost bases:
  `reserved` (estimate held), `unresolved` (unknown outcome, estimate kept), `calculated`
  (usage × operator price — never presented as a provider charge), `provider_confirmed` (only via
  `metering.record_provider_charges`, e.g. a billing export), `released`, `assumed_unbilled`
  (operator policy `SP_COUNT_FAILED_REQUESTS_AS_BILLED=false`). Reconciliation
  (`reconcile_open_requests`) resolves reserved/submitting/started/cancelled attempts through
  their own endpoint and never releases a lost id. Estimate wording corrected: estimates are
  conservative heuristics, not guaranteed bounds; an overrun is logged (`provider_estimate_exceeded`).
- **Regression tests:** `test_billed_failures_then_success_keep_every_attempt_counted` (2 failures
  + success → 3 attempts, $0.12, 3 submissions), `test_operator_policy_can_treat_failures_as_unbilled`,
  `test_failed_attempts_count_toward_the_ceiling`, `test_concurrent_retries_near_the_ceiling_never_exceed_it`,
  `test_attempt_records_are_immutable_in_the_database`, `test_only_provider_reported_charges_are_labelled_confirmed`,
  `test_reconciliation_resolves_abandoned_requests`.
- **Status:** Fixed, locally verified.

### Crash-drill evidence correction
The archived SIGKILL drill recorded `succeeded|2|actual` because the fake provider kept jobs in
process memory. A Redis-backed durable fake queue (`SP_FAKE_DURABLE_QUEUE=1`) now outlives the
worker like a provider queue and counts provider-side submissions; `scripts/verify-release.py`
asserts one attempt, one submission, and provider submissions = recorded attempts.

## P0 — Account deletion and privacy

### F4. Deletion allowed repeat welcome grants and left subscriptions unmanaged
- **Reproduction:** the former `test_account_deletion_removes_content` (test_resilience.py) asserted
  that the *same retained token* re-created a fresh account after deletion — and that account
  received the welcome grant again (`_load_user` grants on every new row). Deletion cleared
  `stripe_customer_id`/`subscription_id` locally without calling Stripe, cancelled only
  ACTIVE project workflows (not panel or export workflows), and purged synchronously while
  activities could still write.
- **Root cause:** deletion was a single synchronous purge that rewrote `auth_subject`; no
  tombstone, no grant dedupe, no external cleanup, no drain.
- **Fix:** `services/deletion.py` — a durable, idempotent job (`account_deletions`, migration 0004):
  block (deleted_at + DB trigger `forbid_writes_for_deleted_owner` on projects/assets/exports, which
  takes FOR SHARE on the owner so in-flight inserts finish first) → cancel all owned workflows
  (project, jobs, panel and export starts; pending starts withdrawn) → drain paid calls
  (`SP_DELETION_DRAIN_SECONDS`, then provider cancel via the attempt's endpoint, cost kept as
  unresolved) → purge with verification → cancel every live Stripe subscription
  (`prorate=false`, `invoice_now=false`; ids retained until success) → delete the Clerk identity
  (`SP_CLERK_SECRET_KEY`) → anonymise. Pre-deletion tokens are refused permanently (HMAC
  tombstone); a post-deletion sign-in creates an empty account with no welcome grant
  (`signup_grants`, HMAC of subject and email; existing grants backfilled by the migration).
  Clerk `user.deleted` webhook uses the same job (identity step skipped). Activities whose owner
  is deleted fail non-retryably. Policy and retention: `docs/data-retention.md`.
- **Regression tests:** `tests/test_deletion.py` — `test_account_deletion_blocks_purges_and_resists_recreation`
  (retained token 401 on 5 endpoints; fresh token → new empty account, balance 0; alias subject with
  same email → no grant; other user untouched); `test_deletion_during_generation_drains_cancels_and_leaves_nothing`
  (job waits in `draining` while a paid call is in flight, then cancels it at the provider; no assets
  appear afterwards); `test_deletion_during_export_cancels_the_export`; `test_concurrent_requests_during_deletion`
  (DELETE ×3 + 6 concurrent creates; no project survives — this failed before the DB trigger was added);
  `test_billing_failure_keeps_identifiers_and_retries`; `test_clerk_identity_is_deleted_and_webhook_is_idempotent`
  (Clerk 503 → retried, then 200; repeated webhook/request is a no-op); `tests/test_auth.py::test_stripe_subscription_cancellation_requests`
  (request shapes against the local stub). The immutability trigger from F3 also caught a race in
  the drain step (an attempt settling while being stopped), fixed with conditional transitions.
- **Status:** Fixed, locally verified. Real Stripe test-mode and Clerk calls: Open — external (B3, B4).

### F5. Object deletion errors were silently ignored
- **Reproduction:** `tests/test_deletion.py::test_partial_access_denied_is_reported_not_swallowed`
  (`F5-before.txt`): `delete_objects` returned per-key `AccessDenied`; `delete_keys` returned `None`
  and the object remained.
- **Fix:** `storage.delete_keys` returns a `DeleteResult` (`deleted`, `failed{key: code}`), inspects
  per-key `Errors`, retries recoverable codes with backoff, and on versioned buckets deletes each
  version and delete marker (`list_object_versions`). Callers use `maintenance.delete_objects_durably`,
  which records unconfirmed keys in `pending_object_deletions` in the same transaction that removes
  the asset rows; `retry_pending_object_deletions` retries them (metric `sp_object_deletions_pending`).
  Account deletion does not complete while any remain. Terraform IAM gains `s3:DeleteObjectVersion`,
  `s3:ListBucketVersions`, `s3:GetBucketVersioning`.
- **Regression tests:** partial AccessDenied; transient `SlowDown` retried to success; duplicate
  execution; versioned bucket (moto) — a plain delete leaves 2 versions, `delete_keys` leaves 0 and no
  delete marker; durable reference kept through a denied purge, cleared once permission is restored.
- **Status:** Fixed, locally verified against moto. Real S3 versioned-bucket behaviour and the IAM
  change are unverified until `terraform apply` (B7).

### F6. Browser caches were not partitioned or cleared by account
- **Reproduction:** `apps/web/e2e/isolation.spec.ts` against the unchanged web code
  (`F6-before.txt`): after A → sign out → B in the same tab, A's comic title rendered on B's dashboard,
  both normally and when A's responses were delayed past the switch. The offline variant passed
  before and after (the signed-out shell shows only a spinner), so it is a guard, not a reproduction.
- **Root cause:** one QueryClient for the page lifetime, private keys without identity, no cancel/clear
  on sign-out, module-level editor draft store.
- **Fix:** `src/lib/session.ts` + `providers.tsx`: one QueryClient per identity (old one's queries
  cancelled and cache cleared; app subtree remounted with `key=identity`); private keys prefixed via
  `pk()`; `api.ts` drops responses to requests issued under a previous identity; sign-out/sign-in clear
  the token source and reset registered stores (editor drafts) before anything else.
- **Regression tests:** the three isolation specs (no A titles, project ids, signed asset paths or images
  for B; every API request after the switch carries B's identity or none; delayed A responses dropped).
- **Status:** Fixed, locally verified (dev auth). The Clerk session-switch path is implemented the same
  way but unverified without a Clerk instance (B4).

## P1 — Export retry correctness

### F7. The second failed-export retry could stay queued forever
- **Reproduction:** `tests/test_export_retry.py::test_failed_export_can_be_retried_more_than_once`
  against the unchanged code (`F7-before.txt`): export fails → retry 1 runs workflow `…-r1` and fails →
  retry 2 is marked queued but `stage_export_failed` had replaced `error.attempt`, so it reused
  `start:export:<id>:r1`, the outbox deduplicated it, and no workflow ever started ("timed out").
- **Root cause:** the attempt counter lived in the error-details JSON that failure handling overwrites.
- **Fix:** `exports.attempt` (migration 0005, backfilled from old error details), incremented under the
  row lock; workflow id and outbox key `export-<id>-a<attempt>` (new prefix, so no collision with old `r<n>` keys).
- **Regression tests:** initial failure → retry failure → second retry succeeds, asserting 3 distinct
  outbox start rows, all *delivered*, a ready export with a download URL, and `attempt == 3`;
  5 concurrent duplicate retries → exactly one 202, four 409, exactly one additional workflow start.
- **Status:** Fixed, locally verified (real Temporal dev server and outbox dispatcher).
