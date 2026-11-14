# Staging deployment plan (for review; nothing here has been executed)

Target: Render (API, worker, web, Postgres, Key Value) + Temporal Cloud + AWS S3, as in `infra/`.
Every step lists what proves it worked. Stop at the first failed check.

## 0. Prerequisites (blockers B1, B3–B7)
- GitHub repository with this code; Actions enabled; CI green on `main`.
- Accounts: Render workspace, AWS account, Temporal Cloud namespace `storypanel-staging`, Clerk development instance,
  Stripe test mode, Anthropic, OpenAI, Google Gemini (fal and Voyage are not needed).
- A private pricing file with verified prices (B5).

## 1. Push and let CI run
```bash
git remote add origin git@github.com:<org>/storypanel.git
```
```bash
git push -u origin main
```
Check: all five CI jobs pass (`backend`, `web`, `e2e`, `images`, `infra`). This is the first time images are built,
Compose is run, and `terraform validate` / `promtool` execute. Fix anything they find before continuing.

## 2. Storage (Terraform, plan first)
```bash
cd infra/terraform && terraform init && terraform plan -var-file=staging.tfvars -out staging.plan
```
Review the plan: one private bucket, encryption, versioning, lifecycle, TLS-only policy, CORS for the staging web
origin, one IAM user limited to that bucket. Apply only after review:
```bash
terraform apply staging.plan
```
Create an access key for `storypanel-staging-app` and store it only in the Render env group.
Check: `aws s3api get-public-access-block --bucket storypanel-assets-staging` shows all four blocks true.

## 3. Temporal Cloud
Create namespace `storypanel-staging` with an API key. Check: `temporal workflow list --namespace …` connects.

## 4. Render Blueprint (plan, review, then approve)
1. Create the Blueprint from `infra/render/render.yaml` on branch `main`.
2. Fill the `sync: false` values in env group `storypanel-shared` (see `.env.example` for each name):
   `SP_ENV=staging`, provider keys and model ids, `SP_PRICING_JSON` (contents of the verified file), Temporal address/
   namespace/API key, S3 bucket/region/keys, Clerk issuer/JWKS/authorized parties/webhook secret, Stripe test keys and
   price ids, `SP_CORS_ORIGINS`, `SP_WEB_BASE_URL`, and `SP_DATABASE_URL` using the `postgresql+asyncpg://` scheme with
   `?ssl=require`.
3. Set conservative spend ceilings for staging: `SP_SPEND_LIMIT_GLOBAL_DAILY_USD=20`, `SP_SPEND_LIMIT_USER_DAILY_USD=5`,
   `SP_SPEND_LIMIT_PROJECT_USD=4`.
4. Review the plan Render shows (3 services, 1 database, 1 Key Value). Approve.
Check: the API's pre-deploy step logs `alembic upgrade head` reaching revision `0002`; `/readyz` returns all four
dependencies true. If the API refuses to start, read its first log line: configuration validation names exactly which
setting is wrong (missing model, unverified endpoint, stale or missing price, dev auth, fake providers).

## 5. Wire webhooks
- Stripe (test mode): endpoint `https://<api>/webhooks/stripe`, events `checkout.session.completed`, `invoice.paid`,
  `customer.subscription.created|updated|deleted`, `charge.refunded`.
- Clerk: endpoint `https://<api>/webhooks/clerk`, event `user.deleted`.

## 6. Smoke test (read-only)
```bash
scripts/smoke.sh https://<api-host> https://<web-host>
```
Check: prints `smoke ok`. It asserts live providers, Clerk auth, dev login unreachable, unsigned webhook rejected,
CSP present.

## 7. First live comic, by hand, with a budget
Sign in through Clerk, create one 4-panel strip, approve both gates, edit a balloon, export a PDF.
Watch: `provider_usage` rows (model, request id, `cost_basis = actual`), the trace in the tracing backend, and that
the images are recognisably the same characters. Expected spend: well under USD 2. This is the first evidence for G2.

## 8. Bounded live evaluation (needs approval B2)
From a machine with the staging secrets:
```bash
cd services/backend && uv run python -m storypanel.evals.harness --subset fast --live --max-usd 5
```
Read the eight comics. Then, if approved, the full set with `--subset all --live --max-usd 40`, calibrate
`SP_IDENTITY_SIMILARITY_THRESHOLD`, and record results against §8.3.

## 9. Billing and auth checks (G4)
Stripe test card through Checkout for a plan and a credit pack; change plan; cancel; refund the pack. After each,
compare `/v1/credits` with the ledger and run the reconciliation queries in `runbooks/billing-mismatch.md`.
Delete a Clerk test user and confirm their projects and objects are gone.

## 10. Observability (G6)
Point `SP_OTLP_ENDPOINT`, `SP_SENTRY_DSN` and Langfuse keys at real backends. Load the dashboard JSON and alert rules.
Stop the worker for five minutes: `WorkerDown` and then `StuckProjects` should fire and resolve after restart.

## 11. Restore drill (G7)
Follow `runbooks/restore.md` on the staging database using Render's point-in-time recovery; record the measured time.

## Rollback
Render → Rollback to the previous deploy for API, worker and web. Migrations are expand-only, so the previous version
runs against the new schema. Storage and database are untouched by a rollback.

## Promotion to production
Only after G2–G8 have evidence. Same steps with `production.tfvars`, a separate Render env group, live-mode Stripe,
a production Clerk instance and its own Temporal namespace.
