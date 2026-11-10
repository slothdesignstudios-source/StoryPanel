# Runbook: failed export

**Alert:** `ExportFailures`.

An export renders a frozen snapshot (`exports.revision`), so retrying is always safe and reuses pages that
already rendered (`exports.pages_done`). Users have a Retry button; it starts workflow `export-<id>-rN`.

1. `select id, format, status, error, pages_done from exports where status='failed' order by created_at desc limit 20;`
2. `error.code`:
   - `provider_*` / `timeout` → the upscaler. See provider-outage.md. Upscaled panels are cached per panel version, so progress is kept.
   - `storage_error` → object storage. Check `sp_dependency_up{dependency="storage"}` and bucket credentials.
   - `internal_error` → find the trace: logs with `export_id=<id>` carry `trace_id`. A render crash on one page usually means unusual balloon geometry; the snapshot JSON has it.
3. Retry on the user's behalf: `POST /v1/exports/<id>/retry` as the owner, or ask them to press Retry.
4. Downloads return 403/expired → signed URLs last 10 minutes by design; reloading the page issues a new one. Exports themselves expire after `SP_EXPORT_RETENTION_DAYS` and can simply be re-created.
