# Runbook: backup, restore and data loss

## What holds state
| Store | Contents | Backup | Target |
|---|---|---|---|
| PostgreSQL | everything authoritative: projects, scripts, ledger, events | Managed daily snapshots + point-in-time recovery (enable on the paid Render plan); weekly `pg_dump` to a separate bucket | RPO ≤ 5 min with PITR, RTO ≤ 1 h |
| Object storage | images and exports | Bucket versioning (7 days of old versions) | Undelete within 7 days |
| Temporal | in-flight workflow state | Temporal Cloud (managed) | n/a |
| Redis | rate limits, spend counters, concurrency leases, pub/sub | none: rebuildable | Loss is tolerated |

## Restore PostgreSQL
1. Stop workers first (scale to 0) so nothing writes while you restore.
2. Restore to a **new** instance at the chosen time (Render: database → Recovery). Do not overwrite the original.
3. Check it: `alembic current` equals the deployed head; run the two reconciliation queries in billing-mismatch.md.
4. Point `SP_DATABASE_URL` at the restored instance, start the API, then workers.
5. Comics that were mid-run at the restore point: Temporal may be ahead of the database. Their next activity
   re-reads state and is idempotent; anything inconsistent fails cleanly and can be resumed by the user.
   Payments made after the restore point: replay Stripe events from the dashboard (safe, deduplicated).

Manual dump/restore:
`pg_dump --format=custom --no-owner "$DATABASE_URL" > storypanel-$(date +%F).dump`
`pg_restore --no-owner --dbname "$NEW_DATABASE_URL" storypanel-<date>.dump`

## Restore deleted objects
`aws s3api list-object-versions --bucket <bucket> --prefix users/<id>/…` then delete the delete-marker.
Only within 7 days, and never to undo a user's own deletion request.

## Losing Redis
Rate limits and spend counters reset (spend limits fail open until counters rebuild from new calls).
Running image calls lose their concurrency lease but continue. No user data is affected.

## Restore drill
Run this quarterly against staging and record the measured RTO here. **Not yet performed.**
