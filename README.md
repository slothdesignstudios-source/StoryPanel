# StoryPanel

Turn one sentence into a finished comic: StoryPanel proposes a cast and a script for you to approve, draws
every panel with the same characters, letters it with editable vector balloons, and exports PDF, PNG or CBZ.

**Status: MVP implemented and verified locally with simulated providers. Not production-ready.**
No real model provider, Clerk or Stripe has been exercised, no container image has been built, and nothing has been
deployed. Start with [`docs/requirements-matrix.md`](docs/requirements-matrix.md) (what is proven, with evidence),
[`docs/verification-report.md`](docs/verification-report.md) (what was run) and
[`docs/blockers.md`](docs/blockers.md) (what is needed from outside).

## Layout
```
apps/web/                 Next.js app: wizard, cast and script review, editor (react-konva), reader, billing
services/backend/         One Python package, two entrypoints: FastAPI API and Temporal worker
  src/storypanel/
    api/                  HTTP handlers, auth, middleware, request/response contracts
    services/             projects, editor, credits ledger, billing, events (SSE), outbox, maintenance
    pipeline/             story, art director, QA, lettering, rendering, stage implementations
    workflows/            Temporal workflow definitions, activities, worker
    providers/            adapters (Anthropic, fal, Voyage), deterministic fakes, metering
    domain/ db/ obs/      schemas and rules, models, logging/tracing/metrics
    evals/                evaluation harness
  migrations/ tests/
packages/                 prompts (versioned), layouts, styles, fonts, generated API types
evals/                    golden prompt set, rubrics, reports
infra/                    Dockerfiles, Render blueprint, Terraform, Prometheus/Grafana/OTel config
docs/                     requirements matrix, decisions, policies, runbooks
```

## Run it locally
**Step-by-step guide for Windows (WSL 2) / Ubuntu, including the first live comic: [`docs/RUN-LOCALLY.md`](docs/RUN-LOCALLY.md).**

Everything below uses fake providers and a dev sign-in: no accounts or keys needed.

### With Docker
```bash
docker compose up --build
```
Open http://localhost:3000 and sign in with any email. Add `--profile observability` for Prometheus (9090) and Grafana (3001).
The Compose stack was verified with the browser suite (`docs/evidence/remediation/compose-stack-e2e.txt`).

### Without Docker
Needs PostgreSQL 16 with pgvector, Redis, [uv](https://docs.astral.sh/uv/), Node 22 with pnpm, and the Temporal CLI.
```bash
scripts/native-services.sh start
```
```bash
(cd services/backend && uv sync && uv run alembic upgrade head)
```
```bash
(cd apps/web && pnpm install)
```
```bash
scripts/native-stack.sh start
```
Stop with `scripts/native-stack.sh stop`. Logs are in `/tmp/storypanel-run`.
Optional demo credits: `cd services/backend && uv run python ../../scripts/seed-demo.py you@example.com`.

## Checks
```bash
cd services/backend && uv run ruff format --check src tests && uv run ruff check src tests && uv run mypy
```
```bash
cd services/backend && uv run pytest -q
```
```bash
cd services/backend && uv run alembic check
```
```bash
cd services/backend && uv run python -m storypanel.evals.harness --subset fast
```
```bash
cd apps/web && pnpm typecheck && pnpm lint && pnpm test && pnpm build
```
```bash
cd apps/web && pnpm e2e -- --trace off
```
Backend tests need Postgres (database `storypanel_test`) and Redis; they start S3 emulation and a Temporal
dev server themselves. The e2e suite needs the stack running. The eval needs Postgres, Redis and an S3 endpoint.

Release verification (each writes a report under `docs/evidence/`):
```bash
cd services/backend && uv run python ../../scripts/verify-release.py
```
```bash
cd services/backend && uv run python ../../scripts/verify-observability.py
```
```bash
cd services/backend && uv run python ../../scripts/validate-infra.py
```
```bash
scripts/check-bundle-secrets.sh
```

After changing an API contract:
```bash
(cd services/backend && uv run python ../../scripts/export-openapi.py) && (cd apps/web && pnpm gen:api)
```
CI fails if the generated files are stale.

## Going live
Follow [`docs/blockers.md`](docs/blockers.md), [`docs/providers.md`](docs/providers.md) → "Before the first live run",
then [`docs/staging-plan.md`](docs/staging-plan.md). Every setting is listed in [`.env.example`](.env.example).
External accounts needed: Anthropic, fal, Voyage, Clerk, Stripe, Temporal Cloud, an S3-compatible bucket, and a host (Render blueprint provided).

## Documentation
- [Requirements matrix and release gates](docs/requirements-matrix.md)
- [Verification report](docs/verification-report.md) · [Blockers](docs/blockers.md) · [Staging deployment plan](docs/staging-plan.md)
- [Decisions and recorded conflicts in the spec](docs/decisions.md)
- [Credits and billing policy](docs/billing-policy.md)
- [Providers: verified vs unverified](docs/providers.md)
- [Security and observability](docs/security-and-observability.md)
- [Operations: deploy, rollback, migrations, retention](docs/operations.md)
- Runbooks: [provider outage](docs/runbooks/provider-outage.md), [stuck generation](docs/runbooks/stuck-generation.md),
  [billing mismatch](docs/runbooks/billing-mismatch.md), [failed export](docs/runbooks/failed-export.md),
  [backup and restore](docs/runbooks/restore.md), [elevated failures](docs/runbooks/elevated-failures.md),
  [budget overrun](docs/runbooks/budget-overrun.md)

Fonts: Comic Neue and Bangers, SIL Open Font License (see `packages/fonts`).
