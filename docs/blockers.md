# External requirements

**The actionable list is [`setup-checklist.md`](setup-checklist.md)** (groups A–D: exact variable names,
secret or public, where to configure, what each unlocks). This page keeps the blocker ids other documents
refer to and maps them to it. Secrets are never pasted into chat or committed.

| # | Missing | Checklist | Unlocks |
|---|---|---|---|
| B1 | Provider keys (`SP_ANTHROPIC_API_KEY`, `SP_OPENAI_API_KEY`, `SP_GEMINI_API_KEY`) in the untracked `.env` on the machine that runs the test; no fal or Voyage key | A ([`first-live-comic.md`](first-live-comic.md)) | `scripts/live-smoke.py preflight`; first live calls (gate G2); live behaviour of OpenAI/Gemini errors, refusals, timeouts and reported usage. All PR tests of it are simulated or use mocked HTTP transports |
| B2 | Your written approval of a bounded budget. **First the smoke test only** (one 3-panel strip: with OpenAI images estimated ≈ USD 3.32, cap ≥ USD 4.53, suggested 8.00; with Gemini images ≈ USD 1.20, cap ≥ USD 1.73, suggested 4.00; prices unverified; the cap is an application admission limit, not a guaranteed ceiling — see `smoke-test-cost.md`). The size of any larger evaluation will be proposed from the smoke test's *measured* cost; no amount for the 63-prompt suite is proposed or authorised yet | A | paid smoke run; later §8.3 quality, cost and latency (gate G3) |
| B3 | Stripe test-mode account, prices, CLI | C1–C7 | `live_tests/test_stripe_live.py` (gate G4) |
| B4 | Clerk development instance | B1–B9 | `e2e/live/clerk.spec.ts` (gate G4) |
| B5 | OpenAI and Gemini prices, and the `gemini-3.1-flash-image` model id, are **unverified** (search summaries only; developers.openai.com and ai.google.dev are blocked from this sandbox). Anthropic prices were verified on the official page on 2026-11-12. See [`provider-verification.md`](provider-verification.md) | A | trustworthy reservations and estimate |
| B6 | Network access for Terraform providers (`registry.terraform.io`, `releases.hashicorp.com`) — refused by this environment's policy, not changed | any developer machine or CI (D1) | `terraform init && validate`, then `plan` |
| B7 | Git remote, AWS, Temporal Cloud, Render | D1–D7, D9 | CI; `live_tests/test_storage_live.py`; staging deploy per [`staging-plan.md`](staging-plan.md) (gate G5) |
| B8 | Human decisions and reviews: legal (terms, IP/likeness, data protection, C2PA, retention periods in [`data-retention.md`](data-retention.md)); finance/product sign-off of [`billing-policy.md`](billing-policy.md); manual accessibility audit; security review | — | gates G7, G8 |
| B9 | Real alert receivers (PagerDuty routing key, Slack webhook) as files in `ALERTMANAGER_SECRETS_DIR` | D8 | `scripts/verify-alerting.sh` against real receivers (gate G6) |

## Environment note
In the first session one host, `temporal.download`, was added to the personal Default cloud environment so the
Temporal SDK could fetch its test server. No environment setting or allowlist has been changed since. Blocked
hosts (`ghcr.io` blobs, `quay.io`, `registry.terraform.io`, provider documentation sites) were worked around or
recorded here, not added.
