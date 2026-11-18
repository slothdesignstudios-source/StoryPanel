# Blockers

> **Current status:** see [`release-blockers.md`](release-blockers.md) (2026-11-18). This page keeps the earlier infrastructure detail and blocker ids.

## Remaining blockers (updated 2026-11-15, later the same day)

StoryPanel is **not production-ready**: no live integration has been exercised, and output quality
has not been judged on real generated art. Launch requirements are listed separately in
[`launch-checklist.md`](launch-checklist.md).

1. **No live provider call has been made.** Needs your approval of the one proposed run
   ([`live-test-proposal.md`](live-test-proposal.md): OpenAI images, cap USD 8.00).
2. **Prices are verified** (official pages, 2026-11-15). **OpenAI does not publish token counts per
   image** for `gpt-image-2.5-sunburst`, so its expected cost is a range (USD 1.85–3.97) until the
   live run measures them.
3. **Your installation has not been inspected or upgraded.** Start with the read-only report in
   [`local-upgrade-procedure.md`](local-upgrade-procedure.md) step 1. Waiting comics on an in-memory
   Temporal lose their workflow at the restart; a dry run lists them before anything changes.
4. **Durable image storage is required before the paid test.** Older `scripts/native-stack.sh`
   versions ran an in-memory S3 stand-in (moto): every image was lost when the stack stopped.
   **Prepared for review, not yet run on your machine:**
   - Versity S3 Gateway with a POSIX backend (files under `$SP_DATA/s3`, `--fsync`);
   - guards against stopping in-memory storage or Temporal;
   - snapshot export, import and verify (`scripts/s3-snapshot.py`);
   - an isolated persistence check (`scripts/persistence-check.sh`).

   Verified here only with moto: the check correctly fails, losing 32 of 32 objects. The Versity
   pass is not yet verified, because running the compiled server was not permitted in this session.
5. **The 12-credit discrepancy is unresolved.** It needs the real `scripts/sql/reconcile-comic.sql`
   output.
6. **Strip print size:** the default stays 6 in. Each export states, per page, the width its own
   smallest text needs. A better print layout is a recorded follow-up.

# External requirements

**The actionable list is [`setup-checklist.md`](setup-checklist.md)** (groups A–D: exact variable names,
secret or public, where to configure, what each unlocks). This page keeps the blocker ids other documents
refer to and maps them to it. Secrets are never pasted into chat or committed.

| # | Missing | Checklist | Unlocks |
|---|---|---|---|
| B1 | Provider keys (`SP_ANTHROPIC_API_KEY`, `SP_OPENAI_API_KEY`, `SP_GEMINI_API_KEY`) in the untracked `.env` on the machine that runs the test; no fal or Voyage key | A ([`first-live-comic.md`](first-live-comic.md)) | `scripts/live-smoke.py preflight`; first live calls (gate G2); live behaviour of OpenAI/Gemini errors, refusals, timeouts and reported usage. All PR tests of it are simulated or use mocked HTTP transports |
| B2 | Your written approval of a bounded budget. **First the smoke test only** (one 3-panel strip: with OpenAI images estimated ≈ USD 1.85–3.97, reservation requirement USD 3.41–5.52, proposed cap 8.00; with Gemini images ≈ USD 1.20, requirement USD 1.74, cap 4.00; prices verified 2026-11-15, OpenAI tokens per image not published; the cap is an application admission limit, not a guaranteed ceiling — see `smoke-test-cost.md`). The size of any larger evaluation will be proposed from the smoke test's *measured* cost; no amount for the 63-prompt suite is proposed or authorised yet | A | paid smoke run; later §8.3 quality, cost and latency (gate G3) |
| B3 | Stripe test-mode account, prices, CLI | C1–C7 | `live_tests/test_stripe_live.py` (gate G4) |
| B4 | Clerk development instance | B1–B9 | `e2e/live/clerk.spec.ts` (gate G4) |
| B5 | **Resolved 2026-11-15:** OpenAI and Gemini prices and model ids verified on the official pages (`docs/evidence/pricing-2026-11-15/`). Still open: OpenAI publishes no token counts per image for `gpt-image-2.5-sunburst`; the live run measures them | A | trustworthy reservations and estimate |
| B6 | Network access for Terraform providers (`registry.terraform.io`, `releases.hashicorp.com`) — refused by this environment's policy, not changed | any developer machine or CI (D1) | `terraform init && validate`, then `plan` |
| B7 | Git remote, AWS, Temporal Cloud, Render | D1–D7, D9 | CI; `live_tests/test_storage_live.py`; staging deploy per [`staging-plan.md`](staging-plan.md) (gate G5) |
| B8 | Human decisions and reviews: legal (terms, IP/likeness, data protection, C2PA, retention periods in [`data-retention.md`](data-retention.md)); finance/product sign-off of [`billing-policy.md`](billing-policy.md); manual accessibility audit; security review | — | gates G7, G8 |
| B9 | Real alert receivers (PagerDuty routing key, Slack webhook) as files in `ALERTMANAGER_SECRETS_DIR` | D8 | `scripts/verify-alerting.sh` against real receivers (gate G6) |

## Environment note
In the first session one host, `temporal.download`, was added to the personal Default cloud environment so the
Temporal SDK could fetch its test server. No environment setting or allowlist has been changed since. Blocked
hosts (`ghcr.io` blobs, `quay.io`, `registry.terraform.io`, provider documentation sites) were worked around or
recorded here, not added.
