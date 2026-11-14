# Verification report

## Provider change, 2026-11-14 (from de239a8): Anthropic + OpenAI + Gemini; fal and Voyage disabled

**Everything here is simulated or uses mocked HTTP transports behind the real SDK clients. No paid call was
made, and no live provider has been exercised.**

| Check | Result |
|---|---|
| Backend suite (`uv run pytest -q`, run alone) | **395 passed**, 0 failed (5 min 27 s). Includes `test_image_providers.py` (23 tests) and `test_provider_pipeline.py` (10) |
| `ruff format --check` / `ruff check` (src, tests; also `scripts/live-smoke.py`) | clean |
| `mypy src` | no issues in 64 files |
| `alembic check` | no pending operations (migration `0011_image_providers` applied) |
| Web: `tsc --noEmit` / `pnpm lint` / `vitest` | clean / 1 existing warning / 11 passed |
| Browser e2e (native stack, Playwright) | 11 passed, 1 skipped (live Clerk). The journey now selects Gemini and checks it is shown for the comic. axe passes on the new selector |
| Simulated 3-panel smoke, **OpenAI** (`evidence/smoke/sim-openai-final/`) | cast and script approval, 3 panels `ok`, 1 edit, PDF/PNG/CBZ `ok`, print-quality report. Paid-call models: Anthropic roles, `fake-openai-image`, `fake-gemini-qa`; **no upscale or embedding call** |
| Simulated 3-panel smoke, **Gemini** (`evidence/smoke/sim-gemini-final/`) | same, with `fake-gemini-image` |
| Preflight without credentials (`evidence/preflight-no-credentials-20261114.txt`) | Fails as expected. The only missing settings are `SP_ANTHROPIC_API_KEY`, `SP_OPENAI_API_KEY` and `SP_GEMINI_API_KEY`; no fal or Voyage key is asked for. `api.openai.com` is blocked by this sandbox's proxy, not by the app |
| Bundle secret scan (canaries for all keys, including OpenAI and Gemini) | PASS |
| Simulated identity eval subset (`evidence/remediation/eval-identity-simulated.json`) | 4 cases, 0 errors. Swap and look-alike probes are detected in fake mode only (a fake cannot measure judgment quality) |

Zero fal/Voyage calls are enforced in three ways:
- the registry does not construct either adapter (a fresh-interpreter test checks the modules are not
  even imported);
- the full-comic tests replace fal `generate_image`, `upscale_image` and `embed_images` with
  functions that raise;
- the smoke runs record no such call.

Not verified (needs keys and an approved budget): real OpenAI/Gemini behaviour, error shapes and
reported usage; image quality and reference fidelity; Gemini identity-judgment accuracy; whether the
unverified prices are correct.

## Pre-spend verification round, 2026-11-12 (from 259342e)

Findings and fixes: [`remediation-tracker.md`](remediation-tracker.md), section "Verification round". Cost
model: [`smoke-test-cost.md`](smoke-test-cost.md). Handoff: [`first-live-comic.md`](first-live-comic.md).
Simulated providers and local stubs only: **no paid call, no live integration.**

| Check | Result |
|---|---|
| Backend `uv run pytest -q` (run alone) | **342 passed** (was 281). New: `test_cost_bounds` 7, `test_strip_panels` 3, `test_billing_properties` 5 (40 shuffled deliveries), `test_deletion_guarantees` 4, `test_print_safe_area` 42. New tests fail on the previous code (`evidence/remediation/*-before.txt`), except the deletion test for unresolved charges, which pins behaviour that already held, and the billing property test, which is checked by mutation instead |
| `ruff format --check`, `ruff check`, `mypy` | clean |
| Web `tsc`, `eslint`, `vitest` | clean (1 pre-existing lint warning); 8 unit tests |
| Browser suite, rebuilt native stack | **11 passed, 1 skipped** (live Clerk spec, no credentials) |
| `live-smoke.py run --simulated` (`evidence/smoke/sim5`) | 2 characters; script **3 panels** (checked before approval); 3 final panels; edit 202; PDF (vector text, all fonts embedded), PNG 2000×700 and CBZ valid. Simulated prices, so not a cost measurement |
| `live-smoke.py estimate` | estimated ≈ USD 1.14; minimum cap for a clean run USD 2.21; USD 20.80 held if every retry ran. **Not a guaranteed upper bound** |
| `live-smoke.py preflight` (no keys) | fails closed, naming the three missing keys |
| Billing invariance mutation check | 2 of 2 deliberate entitlement bugs caught (`billing-properties-mutations.txt`) |
| PDF safe area (`evidence/pdf-safe-area/`) | 0 lettering pixels outside the 0.125 in safe area on four representative pages (95 before, on one page); no hidden text on them; 0 unflagged overlaps across 518 maximum-load pairs |
| Price verification | Anthropic prices verified on the official page (fetched; copies in `evidence/pricing-2026-11-12/`). fal and Voyage **unverified** (search summaries; their pages are blocked here) |

## Regression audit and live-integration preparation, 2026-11-12 (from dd0775a)

Findings and fixes: [`remediation-tracker.md`](remediation-tracker.md) § Audit 2026-11-12. Setup:
[`setup-checklist.md`](setup-checklist.md). **Every result here uses simulated providers or local stubs. No
live provider, Stripe, Clerk, storage or alert-receiver integration has been executed.**

| Check | Result |
|---|---|
| Backend `uv run pytest -q` (run alone) | **281 passed** (12 new audit tests + strengthened PDF/deletion assertions) |
| `ruff format --check`, `ruff check`, `mypy` (CI scope) | clean |
| Web `tsc`, `eslint`, `vitest` | clean (1 pre-existing lint warning); 8 unit tests incl. CSP |
| Browser suite on the rebuilt native stack | **11 passed, 1 skipped** (the skipped one is `e2e/live/clerk.spec.ts`, which needs Clerk credentials) |
| Live tests without credentials | `live_tests/` 2 skipped; Clerk spec skipped — by design |
| `live-smoke.py estimate` (no keys) | upper bound **USD 4.84** |
| `live-smoke.py preflight` (no keys) | **FAIL, as expected**: lists the three missing keys; storage round trip and API readiness pass when run through `scripts/live-env.sh` |
| `live-smoke.py run --simulated` | passes: 2 characters, 4 panels (fake script writer; live run requires 3), edit 202, PDF/PNG/CBZ valid, PDF fonts all embedded subsets; simulated cost 0.93 calculated / 1.43 reserved at fake prices (`evidence/smoke/sim3/`) — not a cost measurement |
| PDF visual acceptance | `evidence/pdf-audit/` (PNG vs PDF vs diff); material differences fixed; residual 0.8 % differing pixels explained (anti-aliasing, hinting, bleed) |
| `amtool check-config alertmanager.production.yml` | SUCCESS (real delivery not executed) |

## Remediation round, 2026-11-12 (independent review of 46e33b6)

Per-finding reproduction, fix and tests: [`remediation-tracker.md`](remediation-tracker.md). All runs below used
**simulated providers** unless stated; nothing here is live-provider, Stripe, Clerk or staging evidence.

| Check | Command | Result |
|---|---|---|
| Backend tests | `cd services/backend && uv run pytest -q` (Postgres 16 + pgvector, Redis, moto, Temporal dev server) | **267 passed** (was 209) |
| Lint / types / migrations | `uv run ruff format --check src tests && uv run ruff check src tests && uv run mypy && uv run alembic check` | clean; migrations 0003–0009 apply on populated DBs (local, test, and inside the Compose container) |
| Web | `pnpm typecheck && pnpm lint && pnpm test` | clean (1 pre-existing lint warning); 6 unit tests incl. telemetry scrubbing |
| Browser, native stack | `pnpm exec playwright test` against `scripts/native-stack.sh` | **10/10**, incl. 3 new isolation specs that failed on the old web code (`evidence/remediation/F6-before.txt`) |
| Production images | `docker build -f infra/docker/{backend,web}.Dockerfile --secret id=build_ca,…` | both built (847 MB / 413 MB); no CA material in layers or `/run/secrets` |
| Compose stack | `docker compose up -d --no-build` | 7 services healthy, `/readyz` ok |
| Browser, Compose containers | `pnpm exec playwright test --project=desktop` against the containers | **9/9** (`evidence/remediation/compose-stack-e2e.txt`) |
| Simulated eval | `python -m storypanel.evals.harness --subset all` | 63 cases: 60 completed **and exported**, 3 blocked, 0 errors, explicit denominators (`evidence/remediation/eval-simulated-all.json`). Earlier runs exposed a deadlock introduced by the deletion guard (fixed in migration 0009) and a missing bucket bootstrap |
| Alert delivery | Prometheus v3.7.3 + Alertmanager v0.29.0 on Compose, two workers | WorkerInstanceDown → ticket receiver; WorkerDown → page receiver; both resolved and delivered (`evidence/alerting-drill.txt`). The drill exposed that DNS-SD drops dead targets, so an `up == 0` rule could never fire; rewritten |
| Alert/route config | `promtool check rules`, `amtool check-config` | 19 rules OK; config OK |
| Terraform | `terraform fmt -check` (container) | passes after fixing a syntax error the earlier static check missed; `init/validate` blocked: `registry.terraform.io` Forbidden (B6) |
| Infra static checks | `python scripts/validate-infra.py` | 0 problems |
| Dependency audit | `pnpm audit --prod`; `pip-audit` | no known vulnerabilities |
| Release drill | `python scripts/verify-release.py` | **3/3 PASS** (`evidence/release-verification.json`): SIGKILL mid-request → resumed, `succeeded|1|calculated`, 1 attempt, provider-side submissions 8 = recorded attempts 8, 64.5 s to the gate; load 20 users / 1,588 requests, 0 errors, p95 0.153 s, zero ledger/hold drift, 0 provider rows left open; restore 33 tables / 4,177 rows identical at head 0009 |
| Client bundle secrets | `scripts/check-bundle-secrets.sh` | PASS (41 files) |

### Limits of this evidence
- Provider behaviour (404 vs expiry, cancellation after completion, billing of failed jobs) is modelled by the
  fake provider and fal's published SDK types, not observed live.
- Stripe payload shapes are checked against the installed SDK's generated types for the pinned API version and a
  local stub; no real Stripe event has been processed.
- Repair effectiveness, sheet-layout pass rates on real models, and every §8.3 quality/cost/latency number need
  the approved live budget (B2).

## Earlier round (2026-11-11)

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
