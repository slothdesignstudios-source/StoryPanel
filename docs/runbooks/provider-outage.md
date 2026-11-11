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
- **Image model down:** after five provider errors in a minute the circuit breaker routes *new* requests to
  `SP_IMAGE_MODEL_FALLBACK` / `SP_IMAGE_MODEL_FALLBACK_GENERATE` for two minutes at a time (log line
  `provider_fallback_used`). If no fallback is configured, panels fail individually and users can retry.
  To force the fallback for longer, swap the primary and fallback settings and restart workers. Both must have a
  verified contract and a current price or the worker will refuse to start.
- **Text/vision provider down:** there is no second text provider configured. Pause intake by setting
  `SP_SPEND_LIMIT_GLOBAL_DAILY_USD=0` (new paid work is refused with a clear message; nothing is lost).
- Do not raise retry counts during an outage: retries are bounded on purpose.

## Recovery
Users resume from the project page. To find affected comics:
`select id, status, failure->>'code', resume_from from projects where status='failed' and updated_at > now() - interval '2 hours';`

## Ambiguous outcomes
Requests that timed out after submission keep their provider request id and are resumed or re-fetched, never
resubmitted. A request whose *submission* timed out is marked `ambiguous`, keeps its estimated cost and fails that
unit of work permanently (the user can retry it, which is a new, separately counted request). The maintenance job
asks the provider about every request left open for 30 minutes (`provider_requests_reconciled` log line).
Before issuing goodwill credit, compare `provider_usage.provider_request_id` with the provider's dashboard.
