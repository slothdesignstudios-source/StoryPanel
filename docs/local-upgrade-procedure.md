# Local upgrade procedure (your WSL installation)

**Status:** prepared and rehearsed here, not run on your machine. Your computer is not connected to
this session, so its actual commit, schema and processes are not known yet. Step 1 finds them out
read-only. Nothing in steps 1–2 changes your installation. Step 3 onward does, and needs your go-ahead
after you have seen step 1's output.

Rehearsal on this machine:
- `scripts/upgrade-verify.sh c6a45b1`: an old version, comics paused at both gates, a completed comic, migration, takeover; 27 checks pass;
- `scripts/restart-check.py`: pass;
- the recovery tests: `tests/test_lost_workflows.py`, 3 pass.

**Never:** reset, drop or recreate the `storypanel` database; delete `/tmp/storypanel-data`; or run
`--apply` before reading the dry run.

## 1. Look (read-only)

A second checkout of the new version, next to yours. Your own checkout and running app are not
touched:

```bash
git -C ~/storypanel fetch origin && git -C ~/storypanel worktree add ~/storypanel-next origin/main
```

```bash
~/storypanel-next/scripts/inspect-install.sh ~/storypanel > ~/storypanel-inspect-before.txt; cat ~/storypanel-inspect-before.txt
```

It prints:
- your commit and local changes;
- whether Temporal keeps state in memory;
- the API and worker provider mode;
- `.env` mode and variable names (never values);
- database encoding, schema revision and size;
- comics by status, with each running or waiting comic and whether its workflow still exists;
- open credit holds and unresolved potential charges;
- free disk space.

**Send me that output.** I will confirm or adjust the steps below to what it shows. In particular:

| If the report shows | Then |
|---|---|
| `server_encoding: SQL_ASCII` | step 3b (copy to a new UTF-8 database) is needed |
| a schema revision other than 0011 | I check the migration path from that revision first |
| comics in `queued`, `planning`, `scripting` or `illustrating` | wait until they reach an approval or finish before step 4. Stopping cuts off their paid calls |
| `temporal: state IN MEMORY` | every waiting comic loses its workflow at step 4. Step 6 lists them, and you choose which to resume |
| `.env` mode other than 600, or `git-ignored: NO` | fix that first (`chmod 600 ~/storypanel/.env`) |

## 2. Back up (changes nothing)

```bash
pg_dump -h 127.0.0.1 -U storypanel -Fc --no-owner -f ~/storypanel-backup-$(date +%F-%H%M).dump storypanel && ls -la ~/storypanel-backup-*.dump
```

```bash
(cd ~/storypanel-next/services/backend && uv sync -q && uv run python ../../scripts/check-credits.py)
```

The second command installs the new version's dependencies in `~/storypanel-next` only, then records
whether balances and holds match the ledger *before* the upgrade. It reads only tables that exist in
the old schema. A
mismatch here is a finding about the old data. It is not to be fixed by hand, and it stays separate
from the open 12-credit question.

## 3. Prepare the database (needs your go-ahead)

**3a. Stop the app.** Postgres keeps running:

```bash
cd ~/storypanel && scripts/native-stack.sh stop
```

**3b. Only if step 1 showed SQL_ASCII:** copy into a new UTF-8 database, compare, then swap the names. The
old database stays as `storypanel_sqlascii_old`.

```bash
psql -h 127.0.0.1 -U storypanel -d postgres -c "create database storypanel_utf8 encoding 'UTF8' template template0" && psql -h 127.0.0.1 -U storypanel -d storypanel_utf8 -c "create extension if not exists vector"
```

```bash
pg_restore -h 127.0.0.1 -U storypanel --no-owner --exit-on-error -d storypanel_utf8 $(ls -t ~/storypanel-backup-*.dump | head -1)
```

```bash
for db in storypanel storypanel_utf8; do echo "== $db"; psql -h 127.0.0.1 -U storypanel -d $db -Atc "select 'users',count(*) from users union all select 'projects',count(*) from projects union all select 'panels',count(*) from panels union all select 'credit_ledger',count(*) from credit_ledger union all select 'credit_reservations',count(*) from credit_reservations union all select 'provider_attempts',count(*) from provider_attempts union all select 'assets',count(*) from assets"; done
```

Continue only if both lists are identical:

```bash
psql -h 127.0.0.1 -U storypanel -d postgres -c "alter database storypanel rename to storypanel_sqlascii_old" -c "alter database storypanel_utf8 rename to storypanel"
```

## 4. Switch the code and migrate

```bash
cd ~/storypanel && git status --short && git rev-parse HEAD > ~/storypanel-previous-commit.txt && git pull --ff-only && (cd services/backend && uv sync && uv run alembic upgrade head && uv run alembic current)
```

The last line must print `0014 (head)`. Migrations 0012–0014 only add tables and columns. If
`git status` shows local changes, stop and send them to me first.

## 5. Check restart survival with free simulated providers, then start live

```bash
scripts/native-services.sh start && scripts/native-stack.sh start && services/backend/.venv/bin/python scripts/restart-check.py
```

This must end with `PASS`. Then restart with live providers:

```bash
scripts/native-stack.sh stop && SP_STACK_LIVE=1 scripts/native-stack.sh start
```

## 6. Lost workflows: dry run first

```bash
services/backend/.venv/bin/python scripts/recover-lost-comics.py --min-age-minutes 0
```

This is read-only. For each comic whose workflow is gone, it lists:
- the stage it stopped at;
- the credits it holds now;
- what Resume would redo, and whether that makes paid calls.

Comics whose workflow still runs, and finished, cancelled or deleted comics, are never listed. Not
every paused comic needs recovery: a comic you no longer want can simply be cancelled in the app.

Only if you want them resumable:

```bash
services/backend/.venv/bin/python scripts/recover-lost-comics.py --min-age-minutes 0 --apply
```

This marks the listed comics as stopped at their stage and releases their held credits. Nothing is
charged and nothing is drawn. Then press **Resume** only on the ones you want:
- At an approval, nothing is redone.
- In the drawing stage, only the missing panels are drawn, which is **paid** with live providers.

Tested here:
- no double charge;
- no redraw of finished panels;
- cancelled and deleted comics are never revived.

## 7. Verify

```bash
scripts/inspect-install.sh ~/storypanel > ~/storypanel-inspect-after.txt; services/backend/.venv/bin/python scripts/check-credits.py; scripts/live-env.sh services/backend/.venv/bin/python scripts/live-smoke.py preflight
```

Send me `~/storypanel-inspect-after.txt` and the preflight output. Neither contains keys.

## Rollback

The backup from step 2 and, if made, `storypanel_sqlascii_old` are untouched.

```bash
cd ~/storypanel && scripts/native-stack.sh stop && psql -h 127.0.0.1 -U storypanel -d postgres -c "alter database storypanel rename to storypanel_failed_upgrade"
```

```bash
psql -h 127.0.0.1 -U storypanel -d postgres -c "create database storypanel encoding 'UTF8' template template0" && psql -h 127.0.0.1 -U storypanel -d storypanel -c "create extension if not exists vector" && pg_restore -h 127.0.0.1 -U storypanel --no-owner --exit-on-error -d storypanel $(ls -t ~/storypanel-backup-*.dump | head -1)
```

```bash
git -C ~/storypanel checkout $(cat ~/storypanel-previous-commit.txt) && scripts/native-stack.sh start
```

If the backup was taken from a SQL_ASCII database, rename `storypanel_sqlascii_old` back to
`storypanel` instead of restoring: a UTF-8 database is fine for the old code too. The failed attempt
stays as `storypanel_failed_upgrade` until you drop it yourself.

Later, when no longer needed:

```bash
git -C ~/storypanel worktree remove ~/storypanel-next
```
