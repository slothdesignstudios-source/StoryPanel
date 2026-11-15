# Upgrade verification and a safe upgrade path

## What was verified (2026-11-15, simulated providers, no paid calls)

`scripts/upgrade-verify.sh c6a45b1` runs the version before the Phase 2 changes (database at
migration 0011) and then this version on the same data. It uses its own throwaway database
(`storypanel_upgrade`), its own Temporal dev server, S3 and Redis database, so nothing else is
touched. Evidence is in `docs/evidence/upgrade/`. The run below had **27 checks pass and none fail**.

| Step | Result | Evidence |
|---|---|---|
| The old code (c6a45b1, revision 0011) creates three comics: one paused at the cast approval, one paused at the script approval, one completed | done | `old-revision.txt` |
| Their workflow histories, saved from Temporal, replay against this version's workflow code (`temporalio.worker.Replayer`) | 3/3 replay with no non-determinism | `histories/`, `replay.json` |
| `alembic upgrade head` (0011 → 0014) | every pre-upgrade table keeps its row count. `credit_reservation_events` is added, empty. | `migrate.log`, `rows.txt`, `new-revision.txt` |
| This version takes over the same database and Temporal server | each comic keeps its status. Balance and held credits are unchanged. | `finish.json` |
| Legacy rows are reported honestly | Sheets drawn before reviews existed show no review and their rule currency is "unknown". The old Story Bible shows its world rules unchanged and no continuity rules. A script written before the Bible check is reported as "not checked" and needs an explicit confirmation. Holds made before the hold history existed are listed as "history unavailable". Panel checks of the old completed comic report rule currency "unknown". | `finish.json` |
| The comic paused at the cast approval continues | It is approved after the upgrade. Its new script gets the new Story Bible check, is approved, and finishes. | `finish.json` |
| The comic paused at the script approval continues | Approval without confirming "not checked" is refused (422). With the confirmation it is approved (recorded) and finishes. | `finish.json` |
| The completed comic | It still reads with every panel and exports with the new code. | `finish.json` |
| Credits | Nothing is left held. The ledger reconciles (cached balances equal the ledger; holds equal open reservations). | `reconcile.txt` |
| SQL_ASCII → UTF-8 on copies of that data | See the next section. | `sql-ascii.txt` |

No workflow code changed between 7040061 and this version (`git diff 7040061 -- services/backend/src/storypanel/workflows` is empty), so histories from 7040061 replay trivially.

**Not verified:**
- The owner's own database and histories. Run the script there, or follow the steps below.
- Temporal Cloud.
- Live providers.

### Found during this verification: approvals after a local restart did nothing

`scripts/native-stack.sh` started the Temporal dev server **in memory**. Stopping the stack, which
every upgrade does, dropped every running workflow. Each comic waiting at an approval then
accepted the approval (200) but never moved (`outbox_target_gone` in the API log). This was
reproduced; see `docs/evidence/upgrade/restart.txt`. It is fixed in three parts:

1. The dev server now keeps its state in `$SP_DATA/temporal.db`, so paused comics survive a restart.
   Reproduced as fixed: approved after a restart, the comic moved on to the script approval.
2. `scripts/recover-lost-comics.py` lists running comics whose workflow is gone (read-only). With
   `--apply` it marks each as stopped at its stage and releases its hold. Resume then restarts it
   from that stage: an approval gate is shown again, and nothing already made is redrawn.
3. Approval signals are now keyed per workflow run. Before, re-approving a resumed comic at the
   same version was deduplicated away, which also affected resumed *expired* comics.

Test: `tests/test_lost_workflows.py`.

## New databases vs. an existing SQL_ASCII database

- **New installs:** `scripts/native-services.sh` creates the cluster and the databases as UTF-8.
  This only applies when they are created.
- **An existing database is never changed by the scripts.** If yours was created as SQL_ASCII (a
  cluster made under a C locale before this fix), it stays SQL_ASCII. `native-services.sh` and
  `live-smoke.py preflight` warn about it.
- **Why it matters:** the app writes non-ASCII text in JSON as `\uXXXX` escapes. SQL_ASCII refuses
  them ("unsupported Unicode escape sequence"), so a comic with an accent in a name fails partway
  through. Reproduced on the copy.
- **Do not recreate or reset the database.** Move the data to a new UTF-8 database with
  dump/restore, as below. The old database stays untouched until you remove it yourself.

Checked on copies (`sql-ascii.txt`):
- The SQL_ASCII copy rejects non-ASCII JSON.
- `pg_restore --exit-on-error` into a new UTF-8 database succeeds. Invalid bytes would stop the restore with an error; they are never dropped silently.
- Every table has the same row count.
- Non-ASCII text typed into a text column arrives intact (`Café … ñandú, Ærø, «olé», 漫画`).
- The copy migrates to 0014, and then stores non-ASCII JSON.

## Upgrade steps on your machine (WSL, `~/storypanel`)

All commands run inside WSL from `~/storypanel`. None of them makes a paid call.

1. **See where you are** (read-only):

   ```bash
   psql -h 127.0.0.1 -U storypanel -d storypanel -Atc "show server_encoding"
   ```

   ```bash
   psql -h 127.0.0.1 -U storypanel -d storypanel -c "select id, status, updated_at from projects where deleted_at is null and status not in ('completed','partially_completed','failed','cancelled','blocked','expired') order by updated_at"
   ```

   The second query lists comics that are still running or waiting at an approval.

2. **Stop the app** (Postgres keeps running):

   ```bash
   scripts/native-stack.sh stop
   ```

3. **Back up** (keep this file until you are satisfied):

   ```bash
   pg_dump -h 127.0.0.1 -U storypanel -Fc --no-owner -f ~/storypanel-backup-$(date +%F).dump storypanel
   ```

4. **Only if step 1 printed `SQL_ASCII`:** copy into a new UTF-8 database, check it, then swap the names.

   ```bash
   psql -h 127.0.0.1 -U storypanel -d postgres -c "create database storypanel_utf8 encoding 'UTF8' template template0"
   ```

   ```bash
   psql -h 127.0.0.1 -U storypanel -d storypanel_utf8 -c "create extension if not exists vector"
   ```

   ```bash
   pg_restore -h 127.0.0.1 -U storypanel --no-owner --exit-on-error -d storypanel_utf8 ~/storypanel-backup-$(date +%F).dump
   ```

   If the restore stops with an encoding error, stop here. The error names the table, and your
   original database is unchanged. Next, compare the row counts. The two lists must be identical:

   ```bash
   for db in storypanel storypanel_utf8; do echo "== $db"; psql -h 127.0.0.1 -U storypanel -d $db -Atc "select 'users',count(*) from users union all select 'projects',count(*) from projects union all select 'panels',count(*) from panels union all select 'credit_ledger',count(*) from credit_ledger union all select 'assets',count(*) from assets"; done
   ```

   ```bash
   psql -h 127.0.0.1 -U storypanel -d postgres -c "alter database storypanel rename to storypanel_sqlascii_old" -c "alter database storypanel_utf8 rename to storypanel"
   ```

   The old database stays as `storypanel_sqlascii_old`. Drop it yourself only when everything works.

5. **Update the code and migrate:**

   ```bash
   git pull && (cd services/backend && uv sync && uv run alembic upgrade head && uv run alembic current)
   ```

   The last line should print `0014 (head)`.

6. **Start the app.** From now on, Temporal keeps its state in `/tmp/storypanel-data/temporal.db`.

   ```bash
   scripts/native-services.sh start && scripts/native-stack.sh start
   ```

7. **Comics that were waiting or running before step 2 lost their workflow when the old in-memory
   Temporal stopped.** List them (read-only), then mark them so they can be resumed:

   ```bash
   services/backend/.venv/bin/python scripts/recover-lost-comics.py --min-age-minutes 0
   ```

   ```bash
   services/backend/.venv/bin/python scripts/recover-lost-comics.py --min-age-minutes 0 --apply
   ```

   Then press **Resume** on each one. A comic stopped at an approval returns to that approval
   without redrawing. A comic stopped while drawing panels draws only the missing ones, and **with
   live providers that is paid work**. Resume only the ones you want.

8. **Check** (free):

   ```bash
   scripts/live-env.sh services/backend/.venv/bin/python scripts/live-smoke.py preflight
   ```

   This now also checks that the database is UTF-8 and at the latest migration.

To roll back, stop the app and restore the step 3 backup into a new database. Then check out the
previous commit. Migrations 0012–0014 only add tables and columns.
