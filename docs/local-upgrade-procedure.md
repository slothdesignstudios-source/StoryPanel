# Local upgrade procedure (your WSL installation)

**This session has no connection to your computer.** You run every command, and I tailor the next
steps to what step 1 shows. Steps 2–7 below are a **draft**: do not run them until I have adapted them
to your inspection output. Your `.env` and data are kept throughout. The database is never reset,
dropped or recreated.

## 1. Inspect (read-only, no secrets, about one minute)

1. Save the attached `inspect-install.sh` from the chat to your Windows **Downloads** folder.
   Keep the name exactly `inspect-install.sh`; if the browser saved it as `inspect-install (1).sh`,
   rename it.
2. In your Ubuntu (WSL) terminal, run this one command:

   ```bash
   tr -d '\r' < "$(wslpath "$(cmd.exe /c 'echo %USERPROFILE%' 2>/dev/null | tr -d '\r')")/Downloads/inspect-install.sh" > ~/inspect-install.sh && bash ~/inspect-install.sh ~/storypanel > ~/storypanel-inspect.txt 2>&1; cat ~/storypanel-inspect.txt
   ```

   It copies the script into your WSL home folder (removing Windows line endings if any), runs it
   against `~/storypanel`, and saves the report to `~/storypanel-inspect.txt`.
3. Paste the printed report into the chat.

What it does:
- It only reads: `git` metadata, process information, `psql` queries in a read-only transaction,
  and the Temporal workflow list.
- It writes only two files in your WSL home folder: the script copy and the report.
- From `.env` it prints only variable **names** and the file mode, never values.

If `cmd.exe` is not available in your WSL, use this path instead, replacing `<you>` with your
Windows user name: `/mnt/c/Users/<you>/Downloads/inspect-install.sh`.

**Steps 2–7: draft, tailored after step 1.**

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

## 6. Lost workflows: dry run first, paid recovery only with your approval per comic

```bash
services/backend/.venv/bin/python scripts/recover-lost-comics.py --min-age-minutes 0
```

This is read-only. It lists running comics whose workflow is gone; comics whose workflow still runs,
and finished, cancelled or deleted comics, are never listed. For each comic it shows separately:

| Group | Meaning | What recovery does with it |
|---|---|---|
| **Reusable** | saved results: sheets, Story Bible, script, drawn panels | kept and reused; never paid again |
| **Retrievable requests** | provider requests whose result can still be fetched by request id | fetched by reconciliation. With OpenAI and Gemini images there are none: their calls are synchronous |
| **Ambiguous requests** | timed out, cut off or never answered: **may already have been billed**, up to the amount shown | never re-sent. Settle them with the provider's dashboard figures (`live-smoke.py reconcile --charges`) |
| **Unfinished images** | an image was saved but its checks never finished | **not** reused or redrawn automatically |
| **Would redo with new paid requests** | panels or planning steps still missing, with the possible additional cost | only after your explicit approval for that comic |

Then, only if you want to:

```bash
services/backend/.venv/bin/python scripts/recover-lost-comics.py --min-age-minutes 0 --apply
```

This stops the listed comics and releases their held credits. Nothing is drawn, sent or charged.
- A comic waiting at an approval becomes resumable; its Resume redoes nothing.
- A comic whose Resume would make paid requests is **not** made resumable.

For each of those you choose to continue, after reading its possible additional cost:

```bash
services/backend/.venv/bin/python scripts/recover-lost-comics.py --approve-paid-resume <comic-id>
```

Its Resume button then starts exactly the paid work listed under "would redo". A comic you do not
want can be left stopped, or deleted in the app.

Tested here (`tests/test_lost_workflows.py`):
- the groups are reported correctly, including an ambiguous request and an unfinished image;
- `--apply` never makes a paid resume possible without the approval;
- unknown, finished, cancelled and deleted comics are refused or ignored;
- after approval: no double charge, and no redraw of finished panels.

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
