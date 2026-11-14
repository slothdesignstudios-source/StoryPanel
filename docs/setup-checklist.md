# Setup checklist for live integrations

One list, in the order that unlocks the most with the least. Groups are independent: **A needs no
hosting account**, B and C can each be tested locally, and D is only needed for staging.

**Secret** values go in an untracked `.env` (local) or a secret store / the hosting platform's secret
variables, never in chat or the repository. In this cloud environment, provider keys can be saved per host
in the environment's network-secrets form, and hosts are allowed in its network settings. **Public**
values are ordinary configuration and may be committed. Variable names below are exact.

Sources and verification dates for every model, price and API version: [`provider-verification.md`](provider-verification.md).

## A. First live comic (local stack, no hosting accounts)

**See [`first-live-comic.md`](first-live-comic.md).** It needs three secrets: `SP_ANTHROPIC_API_KEY`,
`SP_OPENAI_API_KEY` and `SP_GEMINI_API_KEY`. **No fal or Voyage account is needed.** The file also
lists three network hosts, the committed public config (`config/live-smoke.env`), which prices are
verified and which are not, the free preflight command, and the paid command with its scope.

The cost model behind the budget request is [`smoke-test-cost.md`](smoke-test-cost.md):

| Images from | Estimated cost (unverified prices) | Smallest cap for a clean run |
|---|---|---|
| OpenAI | about USD 3.32 | USD 4.52 |
| Gemini | about USD 1.19 | USD 1.72 |

The approved cap is an application limit, not a guaranteed ceiling on provider charges.

| # | Item | Kind | Where |
|---|---|---|---|
| A1 | `SP_ANTHROPIC_API_KEY` | secret | console.anthropic.com → API keys → untracked `.env` |
| A2 | `SP_OPENAI_API_KEY` (a project key; image models may need organisation verification) | secret | platform.openai.com → API keys → `.env` |
| A3 | `SP_GEMINI_API_KEY` (billing enabled; the free tier does not cover image output) | secret | aistudio.google.com → Get API key → `.env` |
| A4 | Optional: `SP_IMAGE_PROVIDER_DEFAULT=gemini` to make Gemini the default | public | `.env` |
| A5 | Hosts `api.anthropic.com`, `api.openai.com`, `generativelanguage.googleapis.com` | network | local machines: nothing to do |

*The organisation-verification note for OpenAI image models and the free-tier note for Gemini come
from search summaries and are unverified. Preflight reports whether each key can see its model.*

## B. Clerk authentication and identity deletion (development instance; local is fine)

| # | Item | Kind | Where | Unlocks |
|---|---|---|---|---|
| B1 | Clerk **development** instance with email + password sign-in enabled | account | dashboard.clerk.com | everything below |
| B2 | `SP_AUTH_MODE=clerk`, `NEXT_PUBLIC_AUTH_MODE=clerk` | public | `.env` / web build args | Clerk mode on API and web |
| B3 | `SP_CLERK_ISSUER` (`https://<instance>.clerk.accounts.dev`), `SP_CLERK_JWKS_URL` (`<issuer>/.well-known/jwks.json`), `SP_CLERK_AUTHORIZED_PARTIES` (`["http://localhost:3000"]`) | public | Clerk dashboard → API keys / instance URL | token validation (issuer, keys, `azp`) |
| B4 | `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` (`pk_test_…`) | public | Clerk dashboard → API keys | hosted sign-in in the web app |
| B5 | `NEXT_PUBLIC_CLERK_FRONTEND_API` | public | only for a **production** instance on your domain (e.g. `https://clerk.<domain>`); dev default is `*.clerk.accounts.dev` | CSP allows Clerk scripts/requests |
| B6 | `SP_CLERK_SECRET_KEY` (`sk_test_…`) | secret | Clerk dashboard → API keys; `.env` / secret store | identity deletion on account deletion |
| B7 | `E2E_CLERK_SECRET_KEY` (same value as B6), set only in the shell running the test | secret | as B6; never a `NEXT_PUBLIC_` variable | the live test creates/inspects test users |
| B8 | `SP_CLERK_WEBHOOK_SECRET` (`whsec_…`) and a Clerk webhook to `https://<public-api>/webhooks/clerk` for `user.deleted` | secret | Clerk dashboard → Webhooks; needs a publicly reachable API (group D, or a tunnel) | deletion started from Clerk's side |
| B9 | Hosts `api.clerk.com`, `<instance>.clerk.accounts.dev` reachable | network | cloud env network settings (local machines: nothing) | all of B |

Test: `E2E_CLERK=1 E2E_CLERK_SECRET_KEY=… pnpm exec playwright test e2e/live/clerk.spec.ts --project=desktop`
(sign-in, refresh after the 60 s token lifetime, account isolation, no authenticated request after logout,
account deletion removes the Clerk user).

## C. Stripe test-mode billing (local is fine)

| # | Item | Kind | Where | Unlocks |
|---|---|---|---|---|
| C1 | Stripe account in **test mode**; products "Creator" and "Pro" with monthly recurring prices, and a one-off "Credit pack" price | account | Stripe dashboard (test mode) → Product catalog | all of C |
| C2 | `SP_STRIPE_PRICE_CREATOR`, `SP_STRIPE_PRICE_PRO`, `SP_STRIPE_PRICE_CREDIT_PACK` (`price_…`) | public | the price ids from C1 | plan mapping, Checkout |
| C3 | `SP_STRIPE_SECRET_KEY` (`sk_test_…`) | secret | Developers → API keys; `.env` / secret store | API calls (the test refuses non-`sk_test_` keys) |
| C4 | Stripe CLI, `stripe login` to the same test account, then `stripe listen --forward-to http://127.0.0.1:8000/webhooks/stripe` | tool | stripe.com/docs/stripe-cli | real signed webhook delivery, duplicate re-delivery |
| C5 | `SP_STRIPE_WEBHOOK_SECRET` (`whsec_…` printed by `stripe listen`; for staging, the endpoint's signing secret) | secret | as above | signature verification |
| C6 | `SP_STRIPE_API_VERSION=2026-10-28.endive` (preset) and the same version on the webhook endpoint | public | Developers → Webhooks → endpoint API version | the app refuses events in other versions |
| C7 | Host `api.stripe.com` reachable | network | cloud env network settings | all of C |

Test: `SP_LIVE=stripe uv run pytest live_tests/test_stripe_live.py -v` (Checkout session idempotency;
initial grant; duplicate delivery; immediate-proration upgrade top-up; renewal via a test clock; downgrade
without clawback; subscription refund routed to review; cancellation → free). Hosted Checkout completion
itself needs a browser and is done once by hand with test card 4242 4242 4242 4242.

## D. Staging deployment and alert delivery

Nothing here is applied or deployed without your explicit go-ahead.

| # | Item | Kind | Where | Unlocks |
|---|---|---|---|---|
| D1 | A Git remote (GitHub repository) | account | GitHub | CI run of every check in `.github/workflows/ci.yml` |
| D2 | AWS account; short-lived credentials to run `terraform plan`, then your approval to `apply` `infra/terraform` | account / decision | AWS IAM | the bucket, versioning, IAM user |
| D3 | `SP_S3_BUCKET`, `SP_S3_REGION` (public); `SP_S3_ACCESS_KEY_ID`, `SP_S3_SECRET_ACCESS_KEY` (secret, from the Terraform outputs) | mixed | Terraform outputs → secret store | `SP_LIVE=storage uv run pytest live_tests/test_storage_live.py` (signed URLs, tampering rejected, all versions deleted, least privilege) — runnable before Render |
| D4 | Temporal Cloud namespace: `SP_TEMPORAL_ADDRESS`, `SP_TEMPORAL_NAMESPACE` (public), `SP_TEMPORAL_API_KEY` (secret) | account | Temporal Cloud | workflows in staging |
| D5 | Render workspace with permission to create a Blueprint from `infra/render/render.yaml`; fill the env group (`sync: false` keys) | account | Render dashboard | `plan_blueprint`, staging deploy (after approval) |
| D6 | `SP_PRICING_JSON` (contents of the confirmed pricing file), `SP_CORS_ORIGINS`, `SP_WEB_BASE_URL`, `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_ASSET_ORIGINS` | public | Render env group / web env | complete deployed config (validated at startup) |
| D7 | `SP_SENTRY_DSN`, `NEXT_PUBLIC_SENTRY_DSN`, `SP_LANGFUSE_PUBLIC_KEY`, `SP_LANGFUSE_SECRET_KEY`, `SP_OTLP_ENDPOINT` | secret (DSNs public by nature) | Sentry, Langfuse, your OTLP backend | error, LLM and trace telemetry in staging |
| D8 | Alert receivers: PagerDuty Events v2 integration key → file `pagerduty_routing_key`; Slack incoming-webhook URL → file `slack_webhook_url` | secret | PagerDuty service → Integrations; Slack app → Incoming webhooks; mount the directory as `ALERTMANAGER_SECRETS_DIR` | `ALERTMANAGER_CONFIG=alertmanager.production.yml ALERTMANAGER_SECRETS_DIR=… scripts/verify-alerting.sh` (fires, delivers — checked by Alertmanager's counters and your acknowledgement — and resolves) |
| D9 | Approvals: `terraform apply`, Render Blueprint apply, public URL | decision | you | staging smoke test per [`staging-plan.md`](staging-plan.md) |

## What each group does **not** need

A needs no Clerk, Stripe, AWS, Render or Temporal Cloud. B and C need no provider keys and no deployment
(only B8 needs a public URL). D's storage test (D3) does not need Render.
