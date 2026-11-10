# Runbook: stuck generation

**Alerts:** `StuckProjects`, `OutboxBacklog`, `ActivityQueueAge`, `WorkerDown`.

A comic is "stuck" if it is in a working state (not waiting for the user) with no event for 15 minutes.

1. **Are workers running?** `up{job="worker"}`. If not, restart them. Workflows resume exactly where
   they stopped; finished activities are not repeated.
2. **Is the outbox draining?** `sp_outbox_pending`, `sp_outbox_oldest_pending_seconds`.
   `select id, kind, attempts, last_error, payload->>'workflow_id' from outbox where status <> 'delivered' order by id;`
   - `pending` with rising `attempts` → the API/worker cannot reach Temporal (address, API key, TLS, namespace).
   - `dead` (20 failed attempts) → fix the cause, then `update outbox set status='pending', attempts=0, next_attempt_at=now() where id=…`.
3. **Inspect the workflow** in the Temporal UI: id `comic-<project id>` (resumed runs end `-rN`),
   `panel-<request id>`, `export-<export id>`. The pending activity and its last failure are shown there.
   `temporal workflow describe -w comic-<id>` / `temporal workflow show -w comic-<id>`.
4. **Queue age high but healthy?** Add workers. Provider concurrency is capped globally in Redis
   (`sp:slots:*`), so more workers will not exceed provider limits.
5. **One workflow wedged?** Cancel it from the app (or `temporal workflow cancel -w …`). Cancellation runs
   the workflow's cleanup: credit holds are released and the comic is marked cancelled.
   Use `temporal workflow terminate` only as a last resort, because it skips that cleanup. After a
   terminate, release the holds and mark the comic yourself:
   ```
   cd services/backend && uv run python - <<'PY'
   import asyncio, uuid
   from storypanel.pipeline.stages_art import stage_end
   asyncio.run(stage_end(uuid.UUID("<project id>"), status="failed", code="operator_terminated",
                         message="We had to stop this comic. You can try again.", resume_from="illustrating"))
   PY
   ```
   Set `resume_from` to the stage it was in so the user gets a Resume button.
6. **Leaked concurrency slots** (worker killed mid-call) expire by themselves after 5 minutes.
