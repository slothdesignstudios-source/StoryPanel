# Operations: environments, deploys, migrations, retention

## Environments
| | local / test | staging | production |
|---|---|---|---|
| `SP_ENV` | `local` / `test` | `staging` | `production` |
| Providers | fake (default) or live | live, low spend limits | live |
| Auth | dev sign-in | Clerk development instance | Clerk production instance |
| Stripe | off, or test keys | test mode | live mode |
| Config source | `.env` | Render env group `storypanel-staging` | Render env group `storypanel-production` |
| Storage | MinIO / moto | `storypanel-assets-staging` | `storypanel-assets-production` |
| Temporal | dev server | Temporal Cloud namespace `…-staging` | Temporal Cloud namespace `…-production` |

Staging and production share no database, bucket, namespace, API key or webhook secret. `Settings` refuses to
start in either with fake providers, dev auth, wildcard CORS or a default pseudonym salt.

## Deploy
1. Merge to `main` with CI green (lint, types, tests, contract drift, fast eval, image build and scan).
2. Staging deploys from `main`. Run `scripts/smoke.sh <api> <web>` and one comic end to end by hand.
3. Promote the same image to production. `preDeployCommand` runs `alembic upgrade head` before traffic moves.
4. Run `scripts/smoke.sh` against production. Watch the overview dashboard for 15 minutes.

Workers drain on SIGTERM (30 s grace): running activities finish or are retried by another worker.
The API fails `/readyz` first, then stops; open event streams reconnect and replay from their last id.

### Workflow code changes
Running workflows replay their history against new code. Changing the *order or number* of activities in
`workflows/definitions.py` breaks replay for comics already in flight (they can wait at a gate for 14 days).
Either gate the change with `workflow.patched("…")`, or deploy the new definition under a new workflow type/task
queue and let old runs finish on the old workers. Activity bodies and stage code can change freely.

## Rollback
- **Application:** redeploy the previous image (Render → Rollback). Safe at any time, because migrations are
  backward compatible (see below). Verify with the smoke test.
- **Prompts, styles, layouts:** these are versioned files. Revert the commit; every stored panel records the prompt version that made it.
- **Database:** do not run `alembic downgrade` in production. Fix forward, or restore (runbooks/restore.md).

## Migration strategy: expand, then contract
1. *Expand*: add tables/columns/indexes as nullable or with defaults; deploy. Old and new code both work.
2. Backfill in batches outside the migration.
3. *Contract*: remove what is unused in a later release, once no running version reads it.
Never rename or drop in the same release that stops using something. Build large indexes `CONCURRENTLY`.
CI applies all migrations to an empty database, checks they match the models (`alembic check`) and round-trips downgrade/upgrade.

## Retention and deletion (enforced by the worker's maintenance loop, every minute)
| Data | Kept for | Setting |
|---|---|---|
| Progress events | 14 days (clients older than that get a `reset` and refetch) | `SP_EVENT_RETENTION_DAYS` |
| Exports | 30 days, then file deleted and marked expired | `SP_EXPORT_RETENTION_DAYS` |
| Deleted comics | hidden at once; rows and objects purged after 7 days | `SP_DELETED_PROJECT_PURGE_DAYS` |
| Deleted accounts | content purged immediately; account row anonymised | – |
| Credit ledger, audit log | kept (financial/audit record; pseudonymous after account deletion) | – |
| Delivered outbox rows / idempotency keys | 7 days / 2 days | – |
| Old object versions (bucket) | 7 days | Terraform |
| Logs / traces | set in the log and trace backends (recommended 30 / 14 days) | backend config |

Account deletion is available in Settings and via the Clerk `user.deleted` webhook.
Whether keeping ledger rows after deletion satisfies each launch market's law needs legal review.

## Local operation
See the README. `scripts/native-services.sh` and `scripts/native-stack.sh` run everything without Docker.
