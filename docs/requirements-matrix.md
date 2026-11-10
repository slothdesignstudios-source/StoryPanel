# Requirements matrix

Status vocabulary, used strictly:
- **Implemented**: code exists; not exercised here.
- **Locally verified**: exercised in this repository by the named automated test or command, with real
  Postgres, Redis and Temporal dev server, S3 emulation, and **fake providers**.
- **Integration verified**: exercised against the real third-party service. *Nothing in this project has reached this level.*
- **Production validated**: observed under real traffic. *Nothing has reached this level.*

Tests: `F` = `tests/test_flow.py`, `R` = `tests/test_resilience.py`, `A` = `tests/test_api.py`,
`U` = `tests/test_unit.py`, `E2E` = `apps/web/e2e/journey.spec.ts`, `EVAL` = simulated eval harness.

## MVP: product journey (spec §3)
| Requirement | Implementation | Evidence | Status |
|---|---|---|---|
| Prompt → story with ≤3 skippable clarifying questions | `pipeline/story.py:run_intake`, `stages_plan.stage_intake`, `Questions` screen | F `test_strip_with_clarifying_question` | Locally verified |
| Sensible defaults (4 pages, max 8, default style) | `config.py`, `story.normalise_brief` | U config test, F | Locally verified |
| Formats: Western page, 3–4 panel strip | `packages/layouts/templates.json` (12 page + 4 strip templates), `art.choose_template` | U layout test, F, R | Locally verified |
| Four style presets, tokens verbatim and versioned | `packages/styles/presets.json`, `catalog.styles` | U prompt compiler test | Locally verified |
| Character cards, reference sheets (portrait, turnaround, expressions), crops, embeddings | `stages_plan.stage_character_sheet` | F reload test, E2E | Locally verified (placeholder art) |
| Revise a character by instruction | `projects.revise_character`, workflow `revise_character` signal | R approvals test, E2E | Locally verified |
| Recurring location references | `stages_plan.stage_location_ref` | F, R | Locally verified |
| Editable Story Bible | `PATCH /v1/projects/{id}/bible`, `BibleEditor` | F `test_story_bible_edit_invalidates_cast_approval_target`, E2E | Locally verified |
| Beat sheet + script, editable inline | `story.write_script`, `PATCH …/script`, `ScriptReview` | R approvals test, E2E | Locally verified |
| Cast and script approval gates before spending | workflow `_gate`, `approvals` table, credit holds at each gate | R approvals, R insufficient-credits | Locally verified |
| Progressive page/panel generation | workflow fan-out, `page.ready` events, `PagePreviews` | F, E2E | Locally verified |
| Regenerate, edit by instruction | `PanelWorkWorkflow`, `editor.request_panel_work` | F, E2E | Locally verified |
| Version comparison and restore | `GET …/versions`, `POST …/restore`, `Versions` dialog | F, R late-result, E2E | Locally verified |
| Balloon move / resize / retype | `PATCH /v1/balloons/{id}`, Konva editor + keyboard/button alternative | F, A, E2E | Locally verified |
| Layout change, panel reorder | `editor.change_layout`, `editor.reorder_panels` | A editor test, E2E (layout) | Locally verified |
| Reader preview | `/projects/[id]/read` | E2E | Locally verified |
| Export PDF, PNG, CBZ | `ExportWorkflow`, `pipeline/render.py` | F (validity, order, 2000×3000 px), E2E (PDF) | Locally verified |
| Credit balance, usage history, billing screens | `GET /v1/credits`, `/billing` | A, E2E | Locally verified (purchases not exercisable without Stripe) |
| Authentication and account settings | Clerk verification + dev mode; `/settings`, account deletion | A, R deletion, E2E (dev mode) | Dev mode locally verified; **Clerk: Implemented only** |
| Project dashboard with empty states | `/dashboard` | E2E | Locally verified |
| Recovery for failed / interrupted / partial work | `projects.resume`, `Stopped`, "Retry missing panels" | R outage, R partial, R expired | Locally verified |
| Save status, conflict handling, reload restores state | editor save state machine; optimistic versions | F, A, E2E (reload ×2) | Locally verified |
| Accessibility basics | labels, focus ring, skip link, live regions, keyboard balloon controls, canvas transcript in reader | `e2e/responsive.spec.ts` (skip link, no overflow) | Implemented; **no screen-reader or contrast audit** |

## MVP: pipeline (spec §7)
| # | Requirement | Implementation | Evidence | Status |
|---|---|---|---|---|
| 1 | Intake validation and moderation | `moderation.py` (rules + classifier), API pre-check | U, A, R moderation | Locally verified (classifier is a fake) |
| 2 | Structured ComicBrief, versioned Story Bible | `domain/schemas.py`, `story_bibles.version` | F | Locally verified |
| 3 | Character specs, sheets, crops, locations | see above | F | Locally verified |
| 4 | Human cast approval | gate | R | Locally verified |
| 5 | Beat sheet and structured script | `ScriptDraft` | F, EVAL | Locally verified |
| 6 | Script validation + ≤2 repairs + human approval | `domain/validation.py`, `write_script` loop | U (12 rule cases), EVAL (`script_valid_after_repair`), R | Locally verified |
| 7 | Deterministic layout and prompt compilation | `pipeline/art.py` | U (byte-identical prompts, fixed order, no dialogue) | Locally verified |
| 8 | Reference-conditioned generation, view matching, continuity chains | `art.compile_panel`, `stage_plan_pages` chains, `stage_generate_panel` | U, F | Locally verified with fake images; **real conditioning unverified** |
| 9 | Safety, identity, adherence, defect, stray-text checks | `pipeline/qa.py` | R QA tests | Logic locally verified; **judgement quality unverified** |
| 10 | Bounded repair (≤2 automatic retries) | `definitions.run_panel` | R (exactly 3 versions) | Locally verified |
| 11 | Vector lettering and composition | `pipeline/lettering.py`, `render.py` | U (order, overlap, overflow escalation, determinism) | Locally verified |
| 12 | Finalise, upscale, export | `stage_export_page` (cached upscale), assemble | F | Locally verified with fake upscaler |
| – | ≤4 characters, ≤3 per panel, 4–6 panels, 25 words, 3 balloons, valid refs | `validation.py`, `story.py`, `editor._check_text` | U, A | Locally verified |
| – | Text never in pixels | constraint line in every image prompt; dialogue excluded from prompts; stray-text QA | U | Locally verified (prompt side) |
| – | Safety failures never become visible | quarantine flag; never signed/listed/restorable | R safety test | Locally verified |

## Durability and data integrity
| Requirement | Implementation | Evidence | Status |
|---|---|---|---|
| Explicit state transitions | `domain/states.py` | U | Locally verified |
| Deterministic workflows, I/O in activities | `workflows/definitions.py` (no imports with I/O) | runs under Temporal's sandbox in every workflow test | Locally verified |
| Timeouts, bounded retries, backoff with jitter | activity options + `_backoff` | R throttling/malformed | Locally verified |
| Provider concurrency limits | Redis lease semaphore | used in every run | Implemented (no contention test) |
| Cancellation | workflow cleanup, credit release | R cancellation | Locally verified |
| Worker restart recovery | Temporal + idempotent stages | R restart (between activities) | Locally verified; **mid-activity kill not tested** |
| Expired approvals | gate timeout → `expired` → resume | R expired | Locally verified |
| Resumable exports | per-page `pages_done` | R export test | Locally verified |
| Idempotent approvals, generation requests, webhooks | version-scoped approvals; idempotency keys; event-id dedupe | A create, R duplicate approvals, A webhooks (5 concurrent) | Locally verified |
| Transactional outbox | `services/outbox.py` | A (row in same tx), R restart (delivery while no worker) | Locally verified |
| SSE with sequence ids, replay, retention | `project_events`, `/events` | A SSE test over real HTTP | Locally verified |
| Approvals scoped to content versions; edits invalidate | `approvals`, `cast_version`/`script_version` | R approvals | Locally verified |
| Late results cannot overwrite newer edits | `panels.intent_seq` | R late-result | Locally verified |
| Immutable export revision | `exports.revision` + sha256 | R export | Locally verified |
| Provider request ids, reconcile ambiguous timeouts | `providers/metering._paid_request` | R ambiguous (8 submits, not 9) | Locally verified with fake; **fal behaviour unverified** |
| Schema: entities, FKs, indexes, uniqueness, optimistic concurrency | `db/models.py`, `migrations/0001` | `alembic check`, all tests | Locally verified |

## Credits and billing
| Requirement | Implementation | Evidence | Status |
|---|---|---|---|
| Append-only ledger | DB trigger | A append-only | Locally verified |
| Atomic reserve / settle / release / reconcile | `services/credits.py` | A (20 concurrent → 6 succeed), A idempotency, every R test ends reconciled | Locally verified |
| Credits separate from provider cost | `credit_ledger` vs `provider_usage` | – | Locally verified |
| Policy for retries, regenerations, cancel, partial failure, refunds | `docs/billing-policy.md` | R QA, R safety, R partial, R cancel, A refunds | Locally verified |
| Webhook signatures, duplicates, replay, ordering | `services/billing.py` | A webhook test (hand-signed payloads) | Locally verified; **Stripe: Implemented only** |
| Checkout and portal | `billing.create_checkout/portal` | none | Implemented |
| Configurable entitlements and prices | `config.py` | – | Implemented |
| Per-user / per-project / global spend limits | `metering.check_spend_limits` | A `test_spend_limits_refuse_new_paid_work` | Locally verified (limit check); end-to-end enforcement with real prices unverified |
| Estimate before expensive generation | `GET …/estimate`, wizard and approve dialog | A, R, E2E | Locally verified |

## Errors and security
| Requirement | Evidence | Status |
|---|---|---|
| Typed error contract (code, message, request id, retryable, fields) | U, A | Locally verified |
| No stack traces, secrets, prompts or provider responses to users | A | Locally verified |
| Frontend error boundaries and recovery controls | `app/error.tsx`, `Notice` + retry everywhere | Implemented |
| Permanent failures not auto-retried | U (non-retryable list), R outage | Locally verified |
| Ownership checks everywhere | A (20 routes as another user → 404) , E2E | Locally verified |
| CSRF, CORS allowlist, headers, rate limits, size limits | A | Locally verified |
| Image validation, SSRF defence, SVG/XML escaping | U | Locally verified |
| Startup config validation | U | Locally verified |
| Content restrictions | U, A, R | Locally verified (rules); classifier quality unverified |
| Data deletion | R account deletion; A `test_deleted_projects_are_purged_with_their_files` | Locally verified |

## Observability and operations
| Requirement | Evidence | Status |
|---|---|---|
| Structured logs with ids, redaction | U redaction | Locally verified |
| Trace propagation API → outbox → workflow → activity | R trace test | Locally verified |
| Metrics (requests, workflows, providers, quality, credits, health) | names cross-checked against dashboards/alerts by script | Implemented; **not viewed in Grafana** |
| Langfuse, Sentry with the same privacy controls | none | Implemented |
| Dashboards, alert rules, runbooks | `infra/observability`, `docs/runbooks` | Implemented; **not loaded into Prometheus/Grafana** |
| Liveness, readiness, graceful shutdown | `/readyz` checked by the local stack | Locally verified (readiness) |
| Local setup, Compose, observability profile | native scripts used for every run here | Native path locally verified; **Compose not run (no Docker here)** |
| Multi-stage non-root images | `infra/docker` | Implemented; **never built** |
| `.env.example`, migrations, demo seed | – | Migrations locally verified |
| CI | `.github/workflows` | Implemented; **never run** |
| IaC, staging/production separation | `infra/render`, `infra/terraform` | Implemented; **not validated or applied** |
| Smoke tests, rollback, migration strategy, backup/restore, retention | `scripts/smoke.sh`, `docs/operations.md`, runbooks | Implemented; **restore drill not performed** |
| Eval harness, golden set, rubrics, reporting | `evals/`, `storypanel/evals/harness.py` | Simulated run locally verified; **no live evidence** |

## Roadmap items deliberately not built
| Item | Release | Extension point left in place |
|---|---|---|
| Upload own character art / style reference | v2 | `validate_image` is upload-ready; `character_refs` accepts any asset |
| Webtoon, right-to-left manga | v2 | `ComicFormat` enum, layout catalog, `reading_key` is the one place order is defined |
| 10+ presets, custom style adapters | v2 / v3 | presets are data; `characters.lora_id` column |
| Full balloon editor | v2 | geometry/style JSON already stores rotation and tail |
| Character library / series | v2 | `characters.library_id` column |
| Translation, multilingual lettering | v2 | text is a separate vector layer; Latin fonts only today |
| Share links, public reader, gallery | v2 / v3 | none |
| Motion comics, public business API | v3 | none |
| Inpainting, two-pass multi-character compositing, C2PA credentials | unscheduled | `ImageRequest.mode`; export metadata hook in `render.py` |

## Release gates not met
1. No live provider call has ever been made: model ids, request shapes, cost and latency are unverified.
2. No quality metric from §8.3 has been measured (the simulated eval cannot measure them).
3. Clerk and Stripe have not been exercised against real accounts.
4. Container images have not been built; CI has not run; nothing has been deployed.
5. No accessibility audit, load test, security review or restore drill.
6. Legal review of terms, IP/likeness policy, output ownership and data-protection obligations is outstanding.
7. Golden set has 55 prompts (the document asks for 60–100).
