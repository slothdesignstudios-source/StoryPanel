# External requirements

**The actionable list is [`setup-checklist.md`](setup-checklist.md)** (groups A–D: exact variable names,
secret or public, where to configure, what each unlocks). This page keeps the blocker ids other documents
refer to and maps them to it. Secrets are never pasted into chat or committed.

| # | Missing | Checklist | Unlocks |
|---|---|---|---|
| B1 | Provider keys (`SP_ANTHROPIC_API_KEY`, `SP_FAL_KEY`, `SP_VOYAGE_API_KEY`) and network access to the provider hosts | A1–A4 | `scripts/live-smoke.py preflight`; first live calls (gate G2); live behaviour of the paid-request taxonomy (404 vs expired results, cancellation after completion) — all PR tests of it are simulated |
| B2 | Your written approval of a bounded budget. **First the smoke test only** (current upper bound USD 4.84 for one 3-panel strip, printed by `live-smoke.py estimate`). The size of any larger evaluation will be proposed from the smoke test's *measured* cost; no amount for the 63-prompt suite is proposed or authorised yet | A6 | paid smoke run; later §8.3 quality, cost and latency (gate G3) |
| B3 | Stripe test-mode account, prices, CLI | C1–C7 | `live_tests/test_stripe_live.py` (gate G4) |
| B4 | Clerk development instance | B1–B9 | `e2e/live/clerk.spec.ts` (gate G4) |
| B5 | Confirmation of the six prices in `packages/pricing/pricing.2026-11-12.json` (read from official pages via search summaries on 2026-11-12, see [`provider-verification.md`](provider-verification.md)) | A5 | trustworthy estimate and ceilings |
| B6 | Network access for Terraform providers (`registry.terraform.io`, `releases.hashicorp.com`) — refused by this environment's policy, not changed | any developer machine or CI (D1) | `terraform init && validate`, then `plan` |
| B7 | Git remote, AWS, Temporal Cloud, Render | D1–D7, D9 | CI; `live_tests/test_storage_live.py`; staging deploy per [`staging-plan.md`](staging-plan.md) (gate G5) |
| B8 | Human decisions and reviews: legal (terms, IP/likeness, data protection, C2PA, retention periods in [`data-retention.md`](data-retention.md)); finance/product sign-off of [`billing-policy.md`](billing-policy.md); manual accessibility audit; security review | — | gates G7, G8 |
| B9 | Real alert receivers (PagerDuty routing key, Slack webhook) as files in `ALERTMANAGER_SECRETS_DIR` | D8 | `scripts/verify-alerting.sh` against real receivers (gate G6) |

## Environment note
In the first session one host, `temporal.download`, was added to the personal Default cloud environment so the
Temporal SDK could fetch its test server. No environment setting or allowlist has been changed since. Blocked
hosts (`ghcr.io` blobs, `quay.io`, `registry.terraform.io`, provider documentation sites) were worked around or
recorded here, not added.
