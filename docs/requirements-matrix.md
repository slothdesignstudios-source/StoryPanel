# Requirements matrix

Audited 2026-11-11; updated 2026-11-12 after the independent review remediation (see [`remediation-tracker.md`](remediation-tracker.md)).

## Status vocabulary
| Status | Meaning |
|---|---|
| **Implemented** | Code exists. No test or run exercises it. |
| **Locally verified** | Exercised here by the named test or script against real PostgreSQL, Redis and a Temporal dev server, with **simulated providers** and local stand-ins for third parties. |
| **Live integration verified** | Exercised against the real third-party service. **Nothing has this status.** |
| **Staging verified** | Observed in a deployed staging environment. **Nothing has this status.** |
| **Blocked** | Cannot be done without a credential, access or decision listed in [`blockers.md`](blockers.md). |
| **Deferred** | Documented later release, or explicitly decided not to build for the MVP. |

A configuration file on its own is never counted as verified. "Schema-verified" below means a request or
response was checked against the provider's published machine-readable schema, offline; it is weaker than a live call.

Evidence keys: `U` test_unit · `A` test_api · `F` test_flow · `R` test_resilience · `P` test_providers ·
`AU` test_auth · `PR` test_paid_recovery · `D` test_deletion · `B` test_billing · `X` test_export_retry
(all under `services/backend/tests`; see verification-report.md for counts) · `E2E` apps/web/e2e (10 tests: native stack; 9 desktop tests also against the Compose containers) ·
`OBS` docs/evidence/observability.json · `REL` docs/evidence/release-verification.json ·
`INFRA` docs/evidence/infra-static-validation.json · `EVAL` simulated eval, 63 prompts, now including export
(`docs/evidence/remediation/eval-simulated-all.json`) · `ALERT` docs/evidence/alerting-drill.txt.

## 1. Product journey (spec §3)
| Requirement | Status | Evidence |
|---|---|---|
| Prompt → story, ≤3 skippable questions, defaults (4 pages, max 8) | Locally verified | F strip+question test; U config |
| Western page and 3–4 panel strip; 4 style presets, tokens verbatim | Locally verified | U layout and compiler tests; F; EVAL |
| Character cards, reference sheets, crops, embeddings, revision | Locally verified (placeholder art); crops only from validated sheet layouts | F; R approvals; U sheet-layout tests; E2E journey |
| Editable Story Bible, beat sheet, script | Locally verified | F bible test; R approvals; E2E |
| Cast and script approval gates before spending | Locally verified | R approvals; R insufficient-credits; P spend tests |
| Progressive generation with live progress | Locally verified | F; E2E; A SSE test |
| Regenerate, fix by instruction, versions, restore | Locally verified | F; R late-result; E2E |
| Balloon move/resize/retype, layout change, panel reorder | Locally verified | F; A editor test; E2E journey and keyboard test |
| Reader, PDF/PNG/CBZ export | Locally verified | F (validity, order, 2000×3000 px); R provenance; X retries (attempt counter); E2E incl. Compose stack |
| Print PDF: vector lettering, embedded fonts, bleed | Locally verified | U: selectable text, embedded font subsets, TrimBox/BleedBox, rasterised lettering IoU 0.866 vs PNG renderer, edge art extended into bleed |
| Editor and export lettering agree | Locally verified | E2E lettering: per-balloon ink overlap ≥ 0.997 vs ≤ 0.57 for mismatched pairs |
| Credits, history, billing screens | Locally verified; purchases Blocked | A; AU Stripe request shapes; E2E. Real Checkout needs Stripe test keys (B3) |
| Account switch in one tab shows nothing of the previous account | Locally verified (dev auth); Clerk path Implemented | E2E isolation (3): failed on the old code, pass now |
| Account deletion | Locally verified; Stripe/Clerk calls against stubs only | D (11); AU subscription-cancellation request shapes |
| Sign-in and account settings | Dev mode locally verified; Clerk validation locally verified; real Clerk Blocked | AU (9 rejection cases, alg confusion, refresh, isolation); E2E |
| Recovery for failed/interrupted/partial work | Locally verified | R outage, partial, expired; REL worker kill |
| Reload restores project and active jobs; save status; conflicts | Locally verified | F; A; E2E (two reloads, 409 path) |
| Accessibility | Locally verified (automated only) | E2E a11y: axe WCAG 2.1 A/AA clean on 9 screens; keyboard-only create and edit. Manual screen-reader audit not done |

## 2. Pipeline (spec §7)
| # | Requirement | Status | Evidence |
|---|---|---|---|
| 1 | Intake validation and moderation | Locally verified; classifier quality Blocked | U; A; R moderation. Rules are real; the LLM classifier is simulated |
| 2–3 | ComicBrief, versioned Bible, specs, sheets, locations | Locally verified | F; EVAL |
| 4, 6 | Human cast and script approval, version-scoped | Locally verified | R approvals (stale 409, duplicates, edits invalidate) |
| 5–6 | Beat sheet, script, validation, ≤2 repairs | Locally verified | U (12 rule cases); EVAL `script_valid_after_repair` = 1.0 |
| 7 | Deterministic layout and prompt compilation | Locally verified | U (byte-identical prompts, fixed order, no dialogue) |
| 8 | Reference-conditioned generation, view matching, continuity | Schema-verified + locally verified with fake images; real conditioning Blocked | P contract tests (108 cases vs fal's published schema); U; F |
| 9 | Safety, identity, adherence, defect, stray-text checks | Logic locally verified; judgement quality Blocked | R QA tests (incl. missing localisation = insufficient evidence); P Anthropic refusal handling |
| – | Edited dialogue passes the full moderation classifier before export | Locally verified (classifier simulated) | X `test_edited_dialogue_is_fully_moderated_before_export` |
| 10 | Bounded repair: ≤2 automatic retries | Locally verified | R (exactly 3 versions) |
| 10 | Targeted repair of a local defect | Locally verified: `reference_edit` (default; panel + approved portrait, composited through the mask) and `inpaint`; identity benefit Blocked (B2) | R repair test, both modes: pixels outside the mask identical |
| 11 | Vector lettering, reading order, tails, overflow handling | Locally verified | U; E2E lettering |
| 12 | Finalise, upscale, export | Locally verified with fake upscaler; schema-verified for `fal-ai/esrgan` | F; P |
| – | Limits: 4 characters, 3 per panel, 4–6 panels, 25 words, 3 balloons | Locally verified | U; A |
| – | Safety failures never visible | Locally verified | R safety test + URL audit across project JSON, dashboard, version history, exports |
| – | Primary and fallback image models | Locally verified (routing); fallback contract schema-verified; real failover Blocked | P breaker test; P Seedream contract |
| – | Provenance labelling | Locally verified (IPTC `trainedAlgorithmicMedia` in PNG XMP, PDF, CBZ) | R provenance test. Signed C2PA manifests: Deferred, see decisions.md D-PROV |

## 3. Provider correctness and cost enforcement
| Requirement | Status | Evidence |
|---|---|---|
| fal request/response contracts from the official schema, not guesses | Schema-verified | `providers/fal_profiles.py`; P: 108 request cases + 6 response cases against `tests/fixtures/fal_schema_fields.json` (extracted from `@fal-ai/client` 1.10.1) |
| Unverified endpoints cannot be configured | Locally verified | P: live config rejects an unknown model id |
| Embedding model accepts images | Locally verified (allow-list); live Blocked | P: config rejects non-multimodal Voyage models; SDK routes images only to multimodal models |
| Async polling, queued failure, unknown id, timeout classification | Locally verified against the SDK's status classes | P `test_queue_outcomes…`: job failure vs 5xx/429/reset/timeout on status, result and download vs expired result |
| Resume, fetch, cancel and settle use the persisted endpoint and price | Locally verified | PR (breaker opens/closes between attempts; upscaler model change) |
| Transient retrieval failures retry retrieval, never generation | Locally verified | PR: 6 fault types, exactly 1 provider submission |
| Every potentially billed attempt stays counted (immutable attempts) | Locally verified | PR: billed failures + success, ceiling with failed attempts, concurrency, DB immutability trigger, provider-confirmed import |
| Cancellation reaches the provider | Locally verified | P cancellation test |
| Missing, zero, malformed or stale pricing refuses billable work | Locally verified | P: 8 bad-entry cases; live config refusals; pre-dispatch refusal with zero provider calls |
| Explicitly free operations are separately marked | Locally verified | P (`"free": true`) |
| Estimated cost reserved against ceilings before dispatch; settled to actual | Locally verified | P: 20 concurrent calls, exactly 10 admitted at the ceiling; text call reserves full output budget |
| Unknown costs stay visible and conservatively counted | Locally verified | P/PR ambiguous, cancelled, lost and expired-result tests (a 404 is not treated as unbilled); `sp_provider_unreconciled_usd` |
| No resubmission after an ambiguous submission | Locally verified | P: 3 attempts, 1 submission |
| Timeout after submission resumes the same request | Locally verified | P; REL worker kill |
| A paid result lost in a crash is re-fetched, not re-bought | Locally verified | P refetch test; PR crash tests; REL drill now uses a durable simulated queue and asserts provider-side submission counts |
| Customer credits separate from provider cost | Locally verified | separate tables; REL load run reconciles both |
| Real prices, real billing behaviour of each provider | Blocked | no price in the example table is verified; see blockers B1, B5 |

## 4. Durability and data integrity
| Requirement | Status | Evidence |
|---|---|---|
| Explicit state machine | Locally verified | U |
| Deterministic workflows, I/O only in activities | Locally verified | every workflow test runs under Temporal's sandbox |
| Timeouts, bounded retries, backoff with jitter, concurrency caps | Locally verified (caps not load-tested to contention) | R throttling; REL load |
| Cancellation releases credits | Locally verified | R |
| Worker restart at a gate | Locally verified | R restart test |
| **Worker SIGKILL during an in-flight provider request** | Locally verified | REL: recovered in 74 s (60 s heartbeat timeout), no duplicate rows, sheets complete |
| Expired approvals, resumable exports | Locally verified | R |
| Idempotent creation, approvals, webhooks | Locally verified | A; R; A webhooks (5 concurrent deliveries) |
| Transactional outbox | Locally verified | A; R restart |
| SSE sequence ids, replay, retention reset | Locally verified | A, over real HTTP |
| Late results cannot overwrite newer edits; immutable export revision | Locally verified | R |
| Schema, constraints, migrations | Locally verified | `alembic check`; upgrade on populated DB; REL restore keeps head and triggers |
| Backup restore and integrity after restore | Locally verified (logical dump, small data); managed PITR Blocked | REL: 25 tables, 3,318 rows identical, ledger digest identical, restored API serves a comic |

## 5. Credits and billing
| Requirement | Status | Evidence |
|---|---|---|
| Append-only ledger; atomic reserve/settle/release; reconciliation | Locally verified | A (20 concurrent → 6 succeed); REL load: zero drift across 21 accounts |
| Billing policy for retries, regenerations, cancel, partial failure, refunds | Locally verified | R; A refunds |
| Webhook signature, duplicates, replay, out-of-order, equal timestamps | Locally verified (authoritative state fetched from Stripe on every subscription event; stub); real delivery Blocked | A; B; AU authoritative-sync test |
| Plan credits = entitlement per subscription period (renewal, upgrade, downgrade, trial, failed payment) | Locally verified with payloads checked against the pinned SDK types; real test-mode payloads Blocked | B (9) |
| Refund clawback only for known credit packs | Locally verified | B |
| Checkout idempotency and second-subscription guard | Locally verified against a local stub | AU |
| Checkout and portal request shapes; customer idempotency | Locally verified against a local stub of the Stripe API | AU |
| Real Checkout, subscription lifecycle, cancellation | Blocked | B3 |
| Estimates before spending; configurable prices and entitlements | Locally verified | A; R; E2E |

## 6. Security
| Requirement | Status | Evidence |
|---|---|---|
| Typed error contract; nothing internal leaks | Locally verified | U; A; AU |
| Ownership on every project/panel/page/balloon/export/stream | Locally verified | A (20 routes as another user → 404); AU with Clerk-mode tokens; E2E |
| CSRF posture, CORS allowlist, headers, rate limits, size limits | Locally verified | A |
| Image validation, SSRF defence, markup escaping | Locally verified | U |
| Local auth and fake providers impossible in staging/production | Locally verified | AU: 8 refusal cases per environment, including via environment variables |
| No secrets in browser bundle | Locally verified | `scripts/check-bundle-secrets.sh` with 10 canary secrets: none in 41 client files |
| No secrets, tokens, emails or prompt text in logs, traces, error reports | Locally verified | OBS: 6 canaries + JWT/signed-URL/email/prompt patterns absent from logs, Sentry envelope and Langfuse payload |
| Content restrictions; data deletion | Locally verified | U; A; D (durable deletion job, versioned objects, unconfirmed deletions kept, retained-token rejection, no repeat welcome credits) |
| Penetration test, legal review of policy | Blocked | B8 |

## 7. Observability
| Requirement | Status | Evidence |
|---|---|---|
| Correlated trace: request → workflow → activity → provider call | Locally verified over real OTLP/HTTP | OBS: one trace id, 35 spans, services api + worker, release on spans |
| Structured, redacted logs with identifiers | Locally verified | OBS: all lines carry base fields; activity and provider lines carry ids, model, tokens, cost, basis |
| Metrics emitted with bounded labels | Locally verified | OBS: 59 `sp_` families scraped; no identifier-like label values; max 90 series per metric; routes are templates |
| Sentry capture with release, no PII | Locally verified against a local envelope receiver | OBS: 1 event, release and environment set, no user block, no local variables |
| Langfuse tracing with privacy controls | Locally verified against a local OTLP receiver | OBS: 13 observations with model, usage, prompt version; no prompt text |
| Application keeps working when telemetry is down | Locally verified | OBS: comic reaches the cast gate with all three sinks refused |
| Alert rules and dashboard queries are valid PromQL and reference existing runbooks | Locally verified (syntax) | INFRA: 18 alerts, 36 queries parse |
| Sampling ratio governs browser-originated traces | Locally verified | U sampling test (≈20 % at 0.2, consistent across hops) |
| Browser error reports scrubbed | Unit-verified | web `telemetry.test.ts` |
| An alert fires, reaches its receiver, and resolves | See ALERT and verification-report.md (Prometheus + Alertmanager + local receiver on Compose, two workers) | ALERT |
| Dashboards render; collector, Tempo, Grafana work | Not run (Prometheus and Alertmanager were; Grafana/Tempo/collector images not exercised) | — |

## 8. Infrastructure and operations
| Requirement | Status | Evidence |
|---|---|---|
| Local run without Docker | Locally verified | every run above used `scripts/native-*.sh` |
| Docker images build, run non-root; Compose stack | Locally verified | both images built; Compose stack healthy; 9/9 desktop browser tests against the containers (`compose-stack-e2e.txt`) |
| Browser-reachable signed asset URLs | Locally verified | D signing test; Compose journey |
| Deployed configuration is complete and validated | Locally verified (static) | AU blueprint test |
| CI workflow | Implemented; every command in it was run locally except Docker, Terraform, promtool and Trivy steps | this file's evidence |
| Terraform | Syntax parsed, expected resources present (INFRA); `terraform validate`/`plan` Blocked | B6, B7 |
| Render blueprint | Parsed; yields a valid staging config (AU); Render plan Blocked | B7 |
| Smoke test, rollback, migration strategy, runbooks | Implemented | docs; smoke script not run against a deployment |
| Load under a documented workload | Locally verified (application plane only) | REL: 20 concurrent users, 1,321 requests, p95 149 ms, 0 errors |
| Quality, cost and latency targets (spec §8.3) | Blocked | B1, B2: need live providers and an approved budget. Harness now measures finished (exported) comics with explicit denominators and a persisted run budget |

## 9. Deferred (documented later releases, unchanged)
Upload of own art and style references; webtoon and right-to-left manga; 10+ presets and custom adapters; full
balloon editor; character library; translation; share links and gallery; motion comics; public API (v2/v3).
Also deferred by decision: signed C2PA manifests (D-PROV), two-pass multi-character compositing, a second text
provider, browser-side trace spans.

## Release gates
| Gate | State |
|---|---|
| G1 All MVP requirements implemented and locally verified | Met for what can be exercised locally; the review's eight defects are fixed with regression tests. Items marked Blocked are excepted |
| G2 Each provider exercised live with the configured models | Not met (B1) |
| G3 §8.3 quality, cost and latency measured on the golden set | Not met (B1, B2) |
| G4 Clerk and Stripe exercised in test mode | Not met (B3, B4) |
| G5 Images built, CI green on the hosting platform, staging deployed and smoke-tested | Partly: images build and the Compose stack passes the browser suite locally; CI and staging Not met (B7) |
| G6 Observability stack running with a fired and resolved alert | Locally on Compose (see ALERT); real receivers (PagerDuty/Slack) and staging Not met (B7, B9) |
| G7 Manual accessibility audit, security review, restore drill on managed infrastructure | Not met (B8) |
| G8 Legal review: terms, IP/likeness policy, provenance obligations, data protection | Not met (B8) |

**The application is not production-ready.** G1 is the only gate with evidence.
