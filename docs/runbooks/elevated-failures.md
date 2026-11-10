# Runbook: elevated failures or quality regression

**Alerts:** `ApiHighErrorRate`, `ApiSlow`, `ElevatedActivityFailures`, `QaPassRateLow`, `SafetyBlockSpike`, `TelemetryExportFailing`.

1. **What changed?** Compare the `release` label in logs with the last deploy. If the spike started at a deploy, roll back first (docs/operations.md), investigate second.
2. **Which code?** `sum by (code, surface) (rate(sp_errors_total[5m]))`.
   - `internal_error` → Sentry, or logs `error_code="internal_error"`; every line carries `request_id` and `trace_id`.
   - `provider_*` → provider-outage.md. `storage_error` / dependency → restore.md.
   - `version_conflict`, `invalid_state`, `validation_error` are normal user-facing outcomes; investigate only a sharp change.
3. **Follow one request:** take the `request_id` from the user's error message, search logs, open `trace_id` in Tempo. The trace continues from the API through the outbox into the workflow and its activities.
4. **QA pass rate dropped** with no deploy → the image or vision model changed behaviour. Run the eval
   (`python -m storypanel.evals.harness --subset all --live --max-usd …`) and compare with the last live report. Prompts and style tokens are versioned; roll back the version, not individual words.
5. **Safety blocks spiked** → look at `sp_moderation_total` by `surface`. A spike on `image` with normal `prompt` volume suggests the image model, not users. Never loosen the safety check as mitigation.
6. **Telemetry failing** → generation is unaffected by design. Fix the sink; no user action needed.
