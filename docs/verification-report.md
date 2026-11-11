# Verification report (2026-11-11)

Everything here ran in one sandbox: 4 vCPU Linux container, PostgreSQL 16 + pgvector, Redis 7, Temporal dev server
1.32 (CLI 1.10), S3 emulated by moto, Chromium via Playwright 1.63. **All provider calls were simulated.**
There is no Docker daemon, Terraform, Prometheus or outbound access to provider hosts in this environment.

## A. Simulated-provider tests (fake text, image, vision, embedding and upscale providers)
| Command (from `services/backend` unless noted) | Result |
|---|---|
| `uv run ruff format --check src tests && uv run ruff check src tests` | clean |
| `uv run mypy` (strict) | no issues in 60 source files |
| `uv run alembic upgrade head && uv run alembic check` | head `0002`; no drift. `0002` applied to a populated database |
| `uv run pytest -q` | **209 passed** (~2 min) |
| `uv run python -m storypanel.evals.harness --subset all --concurrency 6` | 63 prompts: 60 completed, 3 correctly blocked, 0 errors. Labelled SIMULATED |
| `uv run python ../../scripts/verify-release.py` | 3/3 pass (below) |
| `uv run python ../../scripts/verify-observability.py` | 7/7 pass (`docs/evidence/observability.json`) |
| `uv run python ../../scripts/validate-infra.py` | no problems (syntax and cross-references only) |
| `scripts/check-bundle-secrets.sh` (repo root) | PASS: 10 canary secrets absent from 41 client files |
| `apps/web`: `pnpm typecheck`, `pnpm lint`, `pnpm test`, `pnpm build` | pass (1 lint warning; 4 unit tests) |
| `apps/web`: `pnpm e2e -- --trace off` against `scripts/native-stack.sh` | **7 passed**: journey, lettering visual regression, axe ×2, keyboard-only, responsive ×2 |
| `uv run pip-audit …`, `pnpm audit --prod --audit-level high` | no known vulnerabilities (after upgrading `@clerk/clerk-react` to 5.61.11 for GHSA-w24r-5266-9c3c) |

### What the 209 backend tests cover
Full comic creation, approval, editing, restoration and export · worker restart at a gate · provider outage, rate
limiting, malformed output, ambiguous timeouts, queued failures, unknown request ids, cancellation, fallback ·
concurrent credit reservations and concurrent spend reservations · SSE disconnect, reconnect, replay and retention
reset over real HTTP · stale approvals and concurrent edits · safety rejection, exhausted QA retries, targeted repair ·
cross-account access on 20 routes, with dev and Clerk-mode tokens · payment webhook signature, replay, concurrency and
ordering · pricing policy · provider request contracts against fal's published schema.

### Release runs (`docs/evidence/release-verification.json`)
**Worker SIGKILL during an in-flight image request.** The only worker was killed with signal 9 while
`portrait` was at the provider. A new worker took over after the 60 s
heartbeat timeout; the comic reached the cast gate 73.3 s after the kill, with no duplicate usage rows and complete
character sheets. This run found and fixed a real defect (a finished-but-unstored paid result failed the comic on retry).

**Load.** 20 concurrent users, each creating a 4-panel strip through both gates to completion, then 10 reads.
One API process, one worker. 1362 API requests, status codes {'201': 20, '200': 1342}; latency p50 0.051 s,
p95 0.18 s, p99 0.707 s. 20/20 comics completed, 80 panels, 830 provider calls in
33.6 s. Zero ledger or hold drift, zero provider rows left open.
*Limitation:* fake providers answer in milliseconds, so this measures the application plane only. It says nothing
about real generation latency, provider rate limits or cost.

**Backup and restore.** `pg_dump` of the loaded database (3319 rows, 25 tables) restored into a new
database: identical row counts in every table, identical ledger digest, migration head `0002`, schema matches the
models, the append-only trigger is present, and an API pointed at the restored database serves a finished comic with
its images. *Limitation:* logical dump on one machine with a small dataset; managed point-in-time recovery and object
version restore were not exercised.

### Telemetry (`docs/evidence/observability.json`)
One browser-chosen trace id carried 35 spans across API and worker (HTTP request → workflow start → workflow run →
activities → provider calls) over real OTLP/HTTP. A deliberately injected worker crash arrived at a Sentry envelope
receiver with release and environment and without user data or local variables. Thirteen Langfuse observations
carried model, usage and prompt version and no prompt text. With all three sinks refusing connections a comic still
reached the cast gate. `sp_dependency_up{dependency="storage"}` went 1 → 0 → 1 as storage was stopped and restarted.
*Limitation:* Prometheus, Alertmanager, Grafana, Tempo and the Collector were not run.

### Accessibility
axe-core (WCAG 2.1 A and AA rule sets) reports no violations on sign-in, dashboard, wizard, cast review, script
review, editor, export dialog, reader, billing and settings. It found three colour-contrast failures, which were
fixed. Sign-in, comic creation and balloon editing were completed with the keyboard alone.
*Limitation:* automated rules only; no screen-reader or switch-device testing.

## B. Live-provider tests
**None.** No provider, Clerk or Stripe credential was available, and their hosts are not reachable from this
environment. The specification's quality, cost and latency targets are therefore unmeasured. See `blockers.md`.

## C. Could not be executed here
| Check | Why | Where it will run |
|---|---|---|
| `docker build`, `docker compose up`, image scan | no container runtime | CI jobs `images`, `e2e` |
| `terraform fmt/validate/plan` | binary not installable (host not allowed); plan also needs AWS credentials | CI job `infra`; plan per staging-plan.md |
| `promtool check rules`, alert firing in Prometheus | binary not available | CI job `infra`; staging |
| Render Blueprint plan | needs a Git remote and a Blueprint in a workspace; creating one is a deployment action | staging-plan.md step 4 |
| GitHub Actions run | no remote | first push |
