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
asserts one attempt, one submission, and provider submissions = recorded attempts. Result 2026-11-12: PASS —
`succeeded|1|calculated`, 1 attempt for the in-flight operation, 8 provider-side submissions = 8 recorded attempts.
(The first run failed: the durable fake queue stored raw pickle bytes that the shared text-decoding Redis client could
not read back; fixed and covered by `test_durable_fake_queue_survives_a_new_provider_instance`.)

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

## P1 — Subscription entitlements

### F8. Plan credits keyed by invoice, not entitlement period (plus two schema defects found while verifying)
- **Reproduction (`F8-before.txt`):**
  1. Review claim, in the payload shape the old code read: two distinct paid invoices in one period
     granted 480 instead of 240.
  2. *New:* in the pinned API version (`2026-10-28.endive`, the installed SDK's version) invoice lines have
     no `price` (it is `pricing.price_details.price`): a real `invoice.paid` granted **0** credits.
  3. *New:* `Charge` has no `invoice` field in that version, so the old "not a subscription" test
     (`not obj.get("invoice")`) treated every refund — including subscription refunds — as a credit-pack
     refund and clawed back 40 credits.
  Also: Checkout had no idempotency and no existing-subscription guard; ordering accepted equal timestamps;
  no reconciliation with Stripe.
- **Fix:** `services/billing.py` + migration 0006 (`billing_subscriptions`, `subscription_entitlements`,
  `credit_purchases`). Entitlement per (subscription, period start), with upgrade top-ups, no mid-period
  clawback on downgrade, nothing for zero-value/trial/failed invoices or prorations alone. Subscription
  events fetch authoritative state from Stripe (order-independent) + hourly reconciliation; payload-ordering
  fallback only without API access (local). Pack purchases recorded with payment intent; refunds claw back
  only matched packs, others go to `needs_review`. Checkout forwards the client Idempotency-Key, refuses a
  second live subscription (409), and tags subscriptions with the user id. Client pinned to
  `SP_STRIPE_API_VERSION`; webhooks in another version are refused (500, retried). Account deletion now cancels
  every mirrored subscription. Policy: `docs/billing-policy.md`.
- **Regression tests:** `tests/test_billing.py` (9): one allowance per period across invoices/redeliveries;
  purchase → renewal → immediate-proration upgrade (+160) → downgrade (no clawback) → lower renewal; deferred
  prorations on the next invoice; trial/zero-value/failed then paid later; ordering and equal timestamps without
  API; pack refunds partial/redelivered/full vs. unknown refund → review; duplicate events and API-version
  mismatch (refused, then processed); `test_payload_paths_exist_in_pinned_sdk_types` (every field path read
  exists in the SDK's generated types; the old paths do not). `tests/test_auth.py`: authoritative sync against the
  local Stripe stub (stale "active" event after Stripe says canceled → canceled; `Stripe-Version` header sent);
  Checkout idempotency key forwarded, second subscription refused, packs still allowed.
- **Status:** Fixed, locally verified against generated SDK types and a local stub. **Not** verified against real
  Stripe test-mode payloads, Checkout, the portal, proration or dunning (B3). The entitlement policy itself needs
  product/finance sign-off (B8).

## P1 — Reproducible deployment

| Item | Reproduction / finding | Fix | Evidence | Status |
|---|---|---|---|---|
| Compose signed browser URLs for `http://minio:9000` | SigV4 signs the Host; a URL signed for `minio:9000` is invalid at `localhost:9000` | `SP_S3_PUBLIC_ENDPOINT_URL` + separate presigning client (`storage._signing_client`) | `test_signed_urls_are_valid_for_the_host_the_browser_requests` (botocore reference signer: valid for `localhost:9000`, invalid for `minio:9000`); Versity gateway: presigned GET 200, tampered signature 403; Compose browser journey loads images into the canvas | Fixed, locally verified |
| Render blueprint lacked pricing inputs | — | `SP_PRICING_JSON`, optional fallback/inpaint models, `SP_CLERK_SECRET_KEY`, pinned `SP_STRIPE_API_VERSION` | `test_render_blueprint_supplies_a_complete_deployed_configuration` (all declared vars → valid staging config; dropping any critical one is refused) | Fixed, statically verified; not applied to Render (B7) |
| Embedding dimension vs `vector(1024)` | config allowed 256/512/2048 | must equal `EMBEDDING_DIM` | same test (`2048` refused) | Fixed |
| Production images | never built | both built: backend 847 MB, web 413 MB. Dockerfile changes: uv from PyPI (ghcr.io blob host not reachable here), pnpm pinned via npm, optional `build_ca` BuildKit secret for TLS-intercepting proxies (never stored in a layer: `docker history` and `/run/secrets` checked) | `docker build` logs | Built locally |
| Compose stack | never run. Found: `minio/minio` no longer on Docker Hub; Temporal dev server could not write its SQLite file (non-root image, root-owned volume) | Versity S3 gateway v1.11.0 (SeaweedFS 4.53 was tried and failed SSE puts / raft after restart), `SP_S3_SERVER_SIDE_ENCRYPTION=none` locally (must be AES256/aws:kms when deployed), Temporal 1.10.0 pinned with its DB in the image's home dir | all 7 services healthy, `/readyz` ok; **9/9 browser tests pass against the containers** (`docs/evidence/remediation/compose-stack-e2e.txt`): journey incl. canvas, export download, a11y, isolation, lettering | Locally verified (Compose) |
| Clerk hostnames/CSP | — | unchanged; needs a Clerk instance | — | Open — external (B4) |

## P1 — Safety and comic output

| Item | Finding | Fix | Regression test | Status |
|---|---|---|---|---|
| Edited dialogue only seed-word checked | an edit passing the rules (`"[[unsafe]]"` for the fake classifier) exported | `balloons.moderated_sha` (migration 0007); export runs the full classifier over every balloon whose text changed since it was moderated, under the project lock | `test_edited_dialogue_is_fully_moderated_before_export` (blocked → 422, no export, 1 classifier call; fixed → export; unchanged balloons not re-checked) | Fixed, locally verified |
| Quarantine across previews/restore/upscale/export | re-inspected: export checks the *original* asset's flag even when an upscaled copy exists; restore refuses quarantined versions; all URL surfaces audited | — | existing quarantine URL audit test | Verified (no change needed) |
| Identity QA passed without evidence | a present character without a box was silently skipped | `unlocalised:` / `no_reference:` problems fail QA | `test_missing_localisation_is_insufficient_identity_evidence` | Fixed, locally verified |
| Sheets cropped on an assumed grid | the fake's own expression sheet was 4×1 while cropped 2×2 — silently wrong crops | `pipeline/sheets.py`: grid only if every cell has a figure and no crop line cuts one; else projection segmentation; else crops not used as identity references | `test_sheet_layout_validation`, `test_fake_sheets_have_the_layout_the_pipeline_crops` | Fixed; live sheet layouts unmeasured (B2) |
| Inpainting without references | the fill endpoint takes no reference images | `SP_REPAIR_MODE=reference_edit` (default): multi-reference edit model with panel + approved portrait, composited through the mask; `inpaint` kept | repair test parametrised over both modes (pixels outside the mask identical) | Implemented; identity benefit unmeasured until live eval (B2) |
| PDF lettering rasterised; bleed = blank margin | — | vector balloons/text with embedded font subsets from stored line breaks; art extended into a 0.125 in bleed; TrimBox/BleedBox | selectable text, embedded `ComicNeue`/`Bangers`, boxes, rasterised lettering IoU vs PNG renderer **0.866**; bleed pixels continue the edge art | Fixed, locally verified |

## P1 — Evaluation and observability

| Item | Fix | Evidence | Status |
|---|---|---|---|
| Eval stopped before export; cost from `cost_usd`; defects collapsed; no denominators; budget = daily ceilings | export + upscaling inside each case; cost from immutable attempts split product / eval overhead / unresolved; first-attempt, repaired and final defects; `user_regenerations` explicitly not measured; counts include errors and an `incomplete` list; per-run budget = lifetime `SpendCap` on the run's own user (migration 0008) | `test_run_budget_is_a_lifetime_cap_unaffected_by_day_boundaries`; simulated 63-case run: 60 completed **and exported**, 3 blocked, 0 errors (`eval-simulated-all.json`, labelled SIMULATED) | Fixed; live run Open — external (B1/B2) |
| Browser `sampled=1` overrode the ratio | sampled remote parents re-decided by trace-id ratio | `test_sampling_ratio_applies_to_browser_traces_and_is_consistent_across_hops` (≈20 % at 0.2; API and worker agree) | Fixed |
| No alert delivery | Alertmanager (page/ticket routes, file-based receiver URLs, inhibition), DNS-SD per-instance scraping, `WorkerInstanceDown` | `scripts/verify-alerting.sh` → `docs/evidence/alerting-drill.txt` | see verification report |
| Browser telemetry | Sentry `beforeSend`/`beforeBreadcrumb` scrub signed-URL credentials, tokens, emails, headers, console | `src/lib/telemetry.test.ts` | Fixed, unit-verified |

## Regressions found by the new checks (and fixed)

- **Owner-guard deadlock (introduced by F4, found by the 63-case eval):** the trigger took `FOR SHARE`,
  conflicting with ordinary `UPDATE users` (credit holds) → concurrent work for one user deadlocked. Migration
  0009 uses `FOR KEY SHARE` (still blocked by deletion's `FOR UPDATE`). `test_concurrent_work_for_one_user_does_not_deadlock_on_the_owner_guard`
  fails on 0008 (`owner-guard-deadlock-before.txt`), passes on 0009.
- **Stop/reconcile race (found by the F3 immutability trigger):** an attempt settling while being stopped —
  transitions are now conditional (`only_if`).

## Audit 2026-11-12 — regression audit of the remediation (from dd0775a)

All results below are from **simulated providers and local stubs**; none proves live provider, billing,
auth or infrastructure behaviour (see [`setup-checklist.md`](setup-checklist.md)).

| Edge case | Finding | Fix / evidence | Status |
|---|---|---|---|
| Lost provider result | held: cost kept on the lost attempt; replacement is a new attempt with its own reservation | `test_lost_result_keeps_its_cost_and_the_replacement_needs_its_own_reservation` | Verified (simulated) |
| Cancellation acknowledgement | **defect**: a provider "cancelled" reply was released to $0 as if unbilled | new status `cancel_confirmed`, basis `unresolved`; same in reconcile (`5c9c2ea`); `test_cancellation_acknowledgement_is_not_proof_of_no_charge` | Fixed |
| Deletion recovery | **defects**: a failing step was retried in the same pass; in-flight workflow cancels were fire-and-forget | per-pass `failed_steps`; cancels via outbox + `draining` step that revives dead letters; identifiers retained until each step succeeds; `test_deletion_recovers_when_stripe_clerk_and_storage_all_fail`, `test_deletion_waits_for_workflow_cancellation_and_retries_dead_letters` | Fixed |
| Terraform version deletion | held: IAM grants exactly the actions storage uses (incl. `s3:DeleteObjectVersion`, `s3:ListBucketVersions`) | `test_terraform_iam_grants_exactly_what_storage_uses` | Verified (static) |
| Account switch | held for late responses; autosave timers now cleared on unmount and `session_changed` save errors no longer toast | `e2e/isolation.spec.ts` "autosave pending at sign-out" — a guard: the old code did not visibly fail it (`audit-autosave-before.txt`) | Hardened |
| Subscription periods | **defects**: anchor-reset upgrade and proration-before-base-invoice collided on `period_start` | entitlements keyed by `(subscription_id, period_end)` (migration 0010); `test_upgrade_with_billing_anchor_reset…`, `test_proration_arriving_before_the_base_invoice…`, `test_two_live_subscriptions…` | Fixed |
| Eval lifetime budget | **defect**: a resumed run could be given a new budget | `ensure_run` refuses a changed budget; `--run-id`; `test_eval_run_budget_survives_restart_midnight_concurrency_and_unresolved_charges` | Fixed |
| Compute-second billing (found while verifying prices) | **defect**: fal ESRGAN is billed per compute second; the table had a per-image placeholder | unit `per_compute_second`, reserved at rate × wait budget, unresolved until a provider charge is imported (`0f4b7c7`); `test_compute_second_billing…` | Fixed |
| PDF vector renderer | **defects**: outlines half a stroke too large, tail tips overshooting (18 px at 1600 px), SFX stroke over / black fill | `61840a8`; `test_pdf_balloon_geometry_and_sfx_fill_match_the_png_renderer` (per-balloon ink boxes ≤ 3 px, SFX yellow ratio 0.85–1.15); visual comparison in `evidence/pdf-audit/` | Fixed; remaining differences explained there |
| CSP for a production Clerk instance | **defect**: only `*.clerk.accounts.dev` allowed | `NEXT_PUBLIC_CLERK_FRONTEND_API` in CSP, Docker build arg, Render; `src/lib/csp.test.ts` | Fixed |
| Real alert receivers | webhook-only config could not drive PagerDuty/Slack | `alertmanager.production.yml` (`amtool check-config` SUCCESS); compose overrides; drill reads Alertmanager counters for real receivers | Prepared, not executed live |
| PDF fonts (found in this audit's smoke rehearsal) | **defect**: reportlab's default initial font left an unused, **unembedded** Helvetica resource in every PDF (print preflight rejects it) | canvas starts on the embedded face; `test_pdf_lettering_is_vector_text…` now asserts every font is an embedded subset (failed before: `evidence/remediation/audit-pdf-font-before.txt`); smoke validator checks it too | Fixed |
| Smoke-test handoff (found in this audit) | **defects**: `native-stack.sh` hard-coded fake providers, so the documented live run had no way to start; preflight needed storage settings by hand; preflight could echo raw settings input (may contain key values) on a validation error; `estimate` failed once live mode was set | `scripts/stack-env.sh` (shared, `SP_STACK_LIVE=1` adds `config/live-smoke.env` and parses **only** the three provider keys from `.env`), `scripts/live-env.sh`; validation messages printed without input; estimate needs no keys; live run fails if the strip does not have the 3 requested panels | Fixed; verified with no keys present (preflight fails closed listing the three keys; storage and API checks pass) |

## Verification round 2026-11-12 (from 259342e): pre-spend checks

Simulated providers and local stubs only. No paid call was made.

| Item | Finding | Fix / evidence | Status |
|---|---|---|---|
| Smoke estimate | **Defect:** the USD 4.84 "upper bound" was a call profile × multipliers. It ignored Temporal activity retries (text ×4, image ×5), exports re-submitting upscales, and the fact that a client wait is not a compute-time limit. It was also described as a guaranteed bound | Itemised model built from the code's own limits (`live-smoke.py estimate`, `docs/smoke-test-cost.md`): estimated ≈ USD 1.14; minimum cap for a clean run USD 2.21; USD 20.80 held if every retry ran (the cap stops the run first); a strict ceiling is **not** guaranteed, and the report lists why | Fixed |
| Text reservations | **Defect:** `chars/3 + 500` can under-reserve non-ASCII text, small images (px/750 vs ⌈w/28⌉·⌈h/28⌉) and the output schema | UTF-8 bytes + schema + 4,000-token allowance; the official image rule capped at 4,784; full `max_tokens` (official hard cap). `test_cost_bounds.py` (fails before: `cost-bounds-before.txt`) | Fixed |
| Compute-second upscaling | **Defect:** ESRGAN reserved 150 s, but activity retries resumed the same job for up to 40 min | Cancelled at the reserved time and never resubmitted; reconciliation cancels abandoned jobs; counted as unresolved. Billing for a running job after cancel is undocumented, so it stays listed as exposure | Fixed (app side) |
| fal add-ons | Web search and thinking add-ons were left at their defaults | `enable_web_search=false`, `thinking_level="minimal"`, sent explicitly | Fixed |
| Refusal fallback | A refused Sonnet call can be billed **and** re-run (official page) | Off for the smoke test (`config/live-smoke.env`); stated, not silent | Constrained |
| 3-panel strip | **Defect:** the requested panel count never reached the script rule ("3 to 4"), and the fixture always wrote 4 | `ComicBrief.panels` / `FormatSpec.panels`, exact script rule and validation (editor too); fixture honours it; smoke checks at the script gate *and* at the end in both modes. `test_strip_panels.py` (fails before); simulated run `evidence/smoke/sim4`: 3 panels, edit, PDF/PNG/CBZ valid | Fixed |
| Entitlements | Order and duplication invariance had not been proven | `test_billing_properties.py`: 5 scenarios × 8 shuffled deliveries with duplicate events. Catches two deliberate mutations (`billing-properties-mutations.txt`) | Verified (simulated) |
| Deletion: new work | **Defect:** an activity running at deletion time could still reserve and submit paid calls | Reservation refused for a deleted account (non-retryable) | Fixed |
| Deletion: unresolved charges | Held: deletion waits at most `deletion_drain_seconds`, cancels and continues; reconciliation settles later | `test_deletion_completes_while_a_charge_is_still_unresolved…` | Verified |
| Deletion: minimal retention | **Defect:** the ledger kept "Character revision: {name}" (user text) in an append-only table; `provider_usage.target_id` kept character slugs | User text never written to ledger reasons (static test); targets cleared at anonymisation; amounts and request ids kept | Fixed |
| PDF safe area | **Defect:** the editor let lettering reach the trim (clamp to 0); automatic placement allowed 2 units; one representative page had 95 px of lettering outside the safe area | All geometry writers clamp to a 0.125 in safe area (rotated SFX and outline included); rendered-PDF check finds 0 px outside (`evidence/pdf-safe-area/`). Earlier README wrongly called lettering in the bleed harmless; corrected | Fixed |
| Lettering collisions | **Defect:** captions and SFX were placed over other balloons' text | Free-corner captions, SFX search and shrink, `overlaps_lettering` / `tail_crosses_lettering` flags shown in the editor; 0 unflagged overlaps over 518 maximum-load pairs | Fixed |
| Unprintable characters | **Defect:** characters outside the fonts (Ł, Cyrillic, CJK, emoji) would print as boxes | Script-validation error (no panel paid for), editor refusal, `typeset` flag | Fixed |

## Live smoke preparation (from 39e18c2)

| Item | Change | Status |
|---|---|---|
| Cost breakdown | Itemised table and cap scenarios generated into `smoke-test-cost.md` (`estimate --markdown`); a USD 5.00 cap fits even if every paid call fails once (USD 3.94 with exports) | Done |
| Pricing | fal/Voyage pages blocked (no browser tool; fetch refused). Preflight now checks fal prices with fal's official pricing API once a key exists; Voyage remains unverified | Prepared |
| Character coverage | Documented in the UI (prompt and balloon fields), `release-notes.md`; one actionable message everywhere; prompts with unsupported characters refused before any paid call; editor keeps such text as an unsaved draft; web list generated from the fonts (parity test) | Done |
| Setup | `first-live-comic.md` §1: cloud network secrets per host (fal key on three hosts; `v3.fal.media` allowed without a key because uploads use a CDN token) | Done |
| Preflight | Credential-free checks always run (host reachability, price table, API); errors sanitised; fal pricing API check | Done; result without credentials in `evidence/preflight-no-credentials.txt` |

## Provider change 2026-11-14 (from de239a8): Anthropic + OpenAI + Gemini; fal and Voyage disabled

The owner has no fal or Voyage account. Simulated providers and mocked HTTP transports only: **no paid call was made.**

| Item | Change | Evidence | Status |
|---|---|---|---|
| Provider enablement | `SP_FAL_ENABLED` / `SP_VOYAGE_ENABLED` (default false). Only the keys enabled capabilities need are required. Settings that would reach a disabled provider are refused at startup | IP default-settings test; U production config; P live config | Done |
| OpenAI images | `openai_image.py`: generate and multi-reference edit, explicit sizes, no mask, hashed safety id, SDK retries off | IP request contracts (MockTransport behind the real SDK) | Done |
| Gemini images | `gemini.py: GeminiImageProvider`, called directly (not through fal): aspect and size config, refusal detection, usage by modality | IP | Done |
| Provider per comic | Selector on New comic (default OpenAI; unavailable options disabled with the reason). Stored on the comic and every attempt, never switched; refusals not rerouted | PP persistence across restart and config change; web vitest/tsc | Done |
| Synchronous paid-request safety | `image_sync_*` ledger path: reserve, store before success, ambiguous never re-sent, worker-death recovery, 5xx/429/4xx policy, usage-based settlement, unresolved when usage is missing | IP (13 safeguard tests) | Done |
| Unresolved-charge recovery | Charges importable by `attempt:<id>` when there is no request id; negative and malformed values refused | IP | Done |
| Identity QA | Gemini visual judgment replaces Voyage embeddings. Structured per-character findings; the stricter of model and app outcome wins; mismatch → bounded repair on the same provider; uncertain/invalid/error → manual review | PP; IP structured validation; EVAL identity subset (swap and look-alike probes, simulated) | Done |
| Exports | Local Lanczos resampling, effective-DPI report (300 good / 150 low) stored on the export and shown in the editor; no external call | PP all formats, both providers, fal methods booby-trapped | Done |
| Reconciliation / deletion | Synchronous attempts are never queried. Disabled-fal attempts are rotated, not retried. Deletion purges stored provider results and keeps costs | IP; PP | Done |
| Observability | `provider_unresolved_usd{provider}`, `identity_visual{outcome}`, `provider_print_dpi`. Log redaction covers OpenAI and Google key shapes. Bundle scan has OpenAI/Gemini canaries | U redaction; bundle scan | Done |
| Configuration | `.env.example`, `config/live-smoke.env`, `render.yaml`, nightly eval workflow, `stack-env.sh`, `native-stack.sh` (web started without any provider key), PostgreSQL version auto-detected | blueprint test; shell syntax | Done |
| Smoke tooling | Preflight checks the three hosts and uses free model lookups for each key. `--image-provider`. The cost model is rebuilt for token-billed images and Gemini QA | Simulated runs for both providers (`evidence/smoke/sim-*-20261114`) | Done |
| Prices | OpenAI and Gemini rows **unverified** (search summaries; official pages blocked). Higher rate used where sources conflicted | `provider-verification.md` | Open (B5) |

## Approved scope 2026-11-14: credits, approvals, story fidelity, QA wording, layout (from c6a45b1)

Approved by the owner after the Phase 1 diagnosis and Phase 2 plan. **No paid calls, no deployment.**
Results are labelled code-confirmed, simulated-test-confirmed, visually verified (screenshots) or
live-provider-verified; nothing in this round is live-provider-verified.

**Product decisions (owner):**
- Prices and chargeable outcomes unchanged, including needs-attention panels and superseded
  regenerates/edits. Those policies are stated explicitly in the UI.
- Editable structured continuity rules, backwards compatible. Existing world rules are offered for
  review and classification. Their original text is kept, and no details are invented.
- Character-sheet QA flags problems for review. No automatic paid redraws.
- Story-consistency uncertainty or an unavailable check can be overridden explicitly. The approved
  version and the exception are recorded. Safety and moderation failures stay blocking.
- Strip layouts are preserved. The UI explains that the wide slot is last; panels are not reordered
  and old comics are not changed.
- Unsupported lettering characters are allowed in planning-only input, with a warning. Font coverage
  is still enforced on rendered text. Nothing is transliterated silently.
- No paid A/B tests or live red-team evaluations yet.
- Lettering placement is improved deterministically, using fixtures. Real-image visual validation is
  marked pending where there is no evidence.

**Corrections to the plan (owner):**
- Credit history comes from immutable events. An append-only reservation-event log is added for future
  operations; older detail is marked unavailable or approximate. Settlements are never counted twice.
- The 12-credit drop stays unresolved until the account evidence arrives (`scripts/sql/reconcile-comic.sql`).
- Approvals are bound to the exact persisted versions on the server, and stale approvals are rejected
  transactionally. Tests cover concurrent tabs and a save racing an approval.
- Beat tags are validated deterministically, and the consistency check also judges whether each panel
  expresses its beat.
- QA concurrency is deferred until the identity-input contract, regression outcomes, cost and
  cancellation behaviour are demonstrated. A measurement report comes first.
- Lettering insets of 66% and 58% are starting values. They are tested against rendered shapes, and
  geometry tests are not counted as visual verification.

| Group | Scope | Status (2026-11-14) |
|---|---|---|
| A | Stale/rejected panel-hold leak (reproduced, then fixed); credit-path regressions; append-only hold events (0012); holds and activity view; header and Credits page refresh from live events; charging policy stated at each step; `scripts/sql/reconcile-comic.sql` | Done: simulated-test-confirmed |
| B | Approval re-checked under lock at stage transitions; UI blocks approval with unsaved or saving edits; Themes and World rules editable; server length and nonblank limits; planning input may use unprintable characters (warning); printed text still enforced | Done: simulated-test-confirmed |
| C | Continuity rules plus verbatim world-rule classification; prompt v2 propagation; beat tags; script consistency check with recorded overrides; sheet review flags; required elements in panel QA; patch markers for in-flight workflows; 0013 | Done: simulated-test-confirmed. Judgment quality not measured (live models) |
| D | QA reason categories; "not checked" kept apart from defects; "Drawn · N panels to review" | Done: simulated-test-confirmed; screenshots |
| E | Top-aligned preview, unclipped selects, multi-line lettering inputs, whole sheets on cast cards, text inside drawn balloons (rendered-pixel test), body and SFX avoidance (fixtures) | Done: browser-measured and pixel-tested; real-art visual validation pending |
| F | `scripts/stage-report.py` and `docs/performance.md` | Done (structure only; live timing pending). QA concurrency and paid experiments deferred |

Also found and fixed: local Postgres was created as SQL_ASCII under a C locale, which rejected non-ASCII text in JSON columns. `native-services.sh` now creates UTF-8 databases and warns otherwise.

Still open:
- The 12-credit drop: needs the owner's `reconcile-comic.sql` output.
- "Wide last" showing equal panels: needs the layout id from that output.
- Live verification of every check's judgment quality and of lettering on real art.

## Verification and visible fixes 2026-11-15 (from 7040061)

No paid calls, no deployment. Levels as above. Nothing is live-provider-verified.

| # | Item | Commit | Evidence | Level |
|---|---|---|---|---|
| 1 | Confirmations: labelled per page, panel, rule and issue; start unchecked; clear on reopen and on any change; keys bound on the server to version, check result and message (stale or partial confirmations refused, also from another tab) | 5258b00 | `test_story_fidelity` (2 new), `e2e/review-dialog.spec.ts`, screenshots 05c, 08, 08b | sim, browser |
| 2 | Script fields and validation messages never under the sticky bar or header (reproduced at 1280×720 and on a Pixel 7) | a157480 | `e2e/sticky.spec.ts` (desktop and mobile), screenshot 16 | browser |
| 3 | Readable lettering floor of 15 units (6.48 pt on the 6×9 in page; 30 px in the PNG); overflow flagged instead of shrinking (before: down to 11 units = 4.75 pt); editor, PNG and PDF agree; exports report lettering size; strip PDFs warn | bddf5fa | `test_lettering_legibility`, `text.test.ts`, `e2e/legibility.spec.ts`, `e2e/lettering.spec.ts`, screenshots 13, 15 | sim, pixel, browser. No real art |
| 4 | Rule changes mark older sheet and panel checks as stale (fingerprints). A stale sheet needs confirmation; legacy results are reported as unknown. Phase 2 audit, plus 2 tests for points that had none | 55c8066, 5901b91 | `test_stale_checks`, `docs/phase2-audit.md`, screenshot 14 | sim |
| 5 | Cancellation: no new paid request once a cancel is recorded (also for ended, deleted, or owner-deleted comics); late results kept, never charged, in either order; deterministic race test | 4a9d759 | `test_cancellation_race` (fails without the guard) | sim |
| 6 | Upgrade from c6a45b1 (0011): replay, migration, legacy reporting, paused comics finish, ledger reconciles; SQL_ASCII → UTF-8 on copies. Found and fixed: the local Temporal kept workflows in memory (approvals after a restart did nothing); lost comics can be listed and resumed; signals keyed per workflow run | 6ab88a7, db8ae41, c5db1c0 | `docs/upgrade-verification.md`, `docs/evidence/upgrade/` (27 checks pass), `test_lost_workflows` | sim (real Postgres and Temporal) |
| 7 | Proposed live test (prop rule, distinct shots, script edit, all exports), simulated with both providers; cost model updated | 06da962 | `docs/live-test-proposal.md`, `docs/evidence/smoke/sim-fidelity-*` | sim |

Still open: see [`blockers.md`](blockers.md) → "Remaining blockers (2026-11-15)".
