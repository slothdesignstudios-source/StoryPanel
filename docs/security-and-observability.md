# Security and observability

## Security controls (implemented)
| Area | Control | Where |
|---|---|---|
| Authentication | Clerk RS256 session tokens verified against JWKS: issuer, expiry, not-before, authorised party | `api/deps.py` |
| Authorization | every project/panel/page/balloon/export/event-stream lookup is scoped to the owner; a foreign id returns the same 404 as a missing one | `services/repo.py:get_project`, `services/editor.py:owned_*` |
| CSRF | bearer tokens only (no cookies) + `Origin` allowlist on state-changing requests | `api/app.py` |
| CORS | explicit origins, no credentials, no wildcard in deployed environments | `api/app.py`, `config.py` |
| Headers | API: CSP `default-src 'none'`, nosniff, frame deny, HSTS when deployed. Web: CSP, nosniff, frame deny, referrer and permissions policy | `api/app.py`, `apps/web/next.config.ts` |
| Rate limits | Redis fixed window per user; stricter hourly limit on generation routes | `api/deps.py` |
| Request size | 256 KB cap enforced on declared and streamed bodies | `api/app.py:BodyLimit` |
| Input validation | Pydantic models with `extra="forbid"`; validation errors return field paths, never submitted values | `api/schemas.py` |
| Webhooks | Stripe signature + 5-minute tolerance; Clerk via Svix signature | `services/billing.py`, `api/routes/account.py` |
| Images | type allowlist, dimension and byte limits, decompression-bomb guard, full decode, re-encode to strip metadata | `storage.py:validate_image` |
| SSRF | provider result URLs must be HTTPS and resolve only to public addresses; no redirects; size cap | `providers/fal_image.py` |
| Rendering | user text is only ever drawn as glyphs; XML/SVG output is escaped; download filenames are sanitised | `pipeline/render.py`, `storage.py` |
| Storage | private bucket, SSE, TLS-only policy, 10-minute signed URLs, quarantined assets never signed | `storage.py`, `infra/terraform` |
| Secrets | server-side only, `SecretStr`, validated at startup, never in images or the web bundle | `config.py` |
| Least privilege | app IAM user limited to one bucket; containers run as uid 10001, read-only root FS, no capabilities | `infra/` |
| Prompt injection | user text is data inside tagged blocks in the user turn; models get no tools; outputs are schema-validated; ids, counts, style tokens and limits are enforced in code afterwards | `pipeline/story.py`, `domain/validation.py`, `providers/anthropic_text.py` |
| Content policy | rule lists + LLM classifier on prompt, bible, script, script edits, revision and edit instructions; image safety on every generated image; minors+sexual is an unconditional block | `pipeline/moderation.py`, `pipeline/qa.py` |
| Audit | approvals, creation, deletion recorded with pseudonymous user reference | `services/repo.py:audit` |

**Known gaps.** Database connections do not enforce TLS in code (set `?ssl=require` in the URL). No WAF or bot
protection. No account verification beyond Clerk's. The IP and likeness filters are seed lists plus a classifier:
they are a product control, not a legal determination. No penetration test has been done.

## Observability
**Logs.** JSON, one event per line: `timestamp, level, service, environment, release, message`, plus bound
context: `request_id, trace_id, span_id, user (pseudonymous), project_id, workflow_id, workflow_run_id, stage,
panel_id, attempt`, and on provider calls `provider, model, operation, prompt_version, duration_ms,
input_tokens, output_tokens, images, cost_usd, error_code, provider_request_id`; on failures `error_code,
retryable, retry_outcome`.

**Redaction.** A processor removes values under sensitive keys (authorization, cookies, secrets, tokens, API keys,
signatures, emails, prompt/instruction text, image bytes) and scrubs key-like strings, JWTs, bearer tokens and
signed-URL parameters anywhere in a log line. Raw prompts and images are never logged; `SP_LOG_PROMPTS` exists for
Langfuse debugging in non-production and defaults to false. The same masking is applied to Langfuse (`mask=`) and
Sentry (`before_send`, no PII, no local variables, no request bodies). The collector drops auth headers, query
strings and SQL text as a second layer.

**Traces.** W3C trace context: browser request (`traceparent` set by the web client) → API → stored on the outbox row → re-attached when the workflow is
started or signalled → Temporal's OpenTelemetry interceptor carries it into workflow and activity spans. Verified
by `test_trace_context_crosses_api_outbox_workflow_and_activity`. Sampling: parent-based ratio at the SDK
(`SP_TRACE_SAMPLE_RATIO`), tail sampling at the collector (all errors, all slow traces, 20% of the rest).

**Metrics.** Prometheus, `/metrics` on the API and port 9464 on workers. Labels are low-cardinality only
(route template, status, code, provider, operation, outcome). User, project and panel ids live in logs and traces.
Dashboard: `infra/observability/grafana/dashboards/storypanel-overview.json`. Alerts: `infra/observability/alerts.yml`.

**Health.** `/healthz` (liveness), `/readyz` (database, Redis, storage, Temporal; fails during shutdown).
Telemetry is best effort everywhere: an exporter failure increments `sp_telemetry_failures_total` and nothing else.

**Not built.** Browser-side spans: the web app starts the trace (`traceparent` on every API call) but exports no spans of its own; browser errors go to Sentry if a DSN is set.
Log shipping (Loki or similar) is left to the platform. Dashboards and alert rules have not been loaded into a
running Grafana/Prometheus; metric names were checked against the code by script.
