# Runbook: provider outage or throttling

**Alerts:** `ProviderOutage`, `ProviderThrottling`. **Dashboard:** StoryPanel overview → Providers and spend.

## What users see
Steps retry with backoff (4–5 attempts). After that a planning step fails the comic with a
"try again" message and a Resume button; a panel fails on its own and the comic finishes
as "finished with gaps". Credits for undelivered work are released automatically.

## Triage
1. Which provider and operation? `sum by (provider, operation, outcome) (rate(sp_provider_calls_total[5m]))`.
2. Check the provider's status page. Search logs for `message="provider_call" status="failed"` and read `error_code`.
3. `provider_throttled` only → we are over our rate limit, not an outage. Lower
   `SP_PROVIDER_CONCURRENCY_IMAGE` / `_TEXT` / `_VISION` (takes effect on worker restart).

## Mitigation
- **Image provider down (OpenAI or Gemini):** a comic keeps the provider it was created with. Its panels
  are never moved to the other provider automatically, and refusals are never rerouted. Panels fail
  individually and users retry later. To steer **new** comics away, set `SP_IMAGE_PROVIDER_DEFAULT` to the
  other provider. To hide the failing one from the New comic page, remove it from `SP_IMAGE_PROVIDERS`;
  existing comics on it then report that their provider is unavailable. Restart the API and workers after
  either change. (The fal circuit breaker and fallback models apply only with `SP_FAL_ENABLED=true`.)
- **Gemini identity QA down:** QA errors put panels into manual review ("needs attention"). They do not
  pass and are not repainted. Users can accept or retry them once Gemini recovers.
- **Text/vision provider down:** there is no second text provider configured. Pause intake by setting
  `SP_SPEND_LIMIT_GLOBAL_DAILY_USD=0` (new paid work is refused with a clear message; nothing is lost).
- Do not raise retry counts during an outage: retries are bounded on purpose.

## Recovery
Users resume from the project page. To find affected comics:
`select id, status, failure->>'code', resume_from from projects where status='failed' and updated_at > now() - interval '2 hours';`

## Ambiguous outcomes
OpenAI and Gemini image calls are synchronous: there is no request to resume. A timeout, a dropped connection
or a worker dying mid-call marks the attempt `ambiguous` (an unresolved potential charge at its reservation).
It is never re-sent automatically. The user's Retry is a new, separately reserved attempt. The provider and
unresolved amount show in `sp_provider_unresolved_usd{provider}`. Resolve them from the provider's usage
dashboard with `live-smoke.py reconcile --charges`, using `attempt:<id>` when there is no request id.
(fal queue requests, when fal is enabled:) Requests that timed out after submission keep their provider
request id and are resumed or re-fetched, never resubmitted. A request whose *submission* timed out is marked `ambiguous`, keeps its estimated cost and fails that
unit of work permanently (the user can retry it, which is a new, separately counted request). The maintenance job
asks the provider about every request left open for 30 minutes (`provider_requests_reconciled` log line).
Before issuing goodwill credit, compare `provider_usage.provider_request_id` with the provider's dashboard.
