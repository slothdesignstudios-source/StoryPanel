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
   tr -d '\r' < /mnt/c/Users/<windows-user>/Downloads/inspect-install.sh > ~/inspect-install.sh && bash ~/inspect-install.sh ~/storypanel > ~/storypanel-inspect.txt 2>&1; cat ~/storypanel-inspect.txt
   ```

   It copies the script into your WSL home folder (removing Windows line endings if any), runs it
   against `~/storypanel`, and saves the report to `~/storypanel-inspect.txt`.
3. Paste the printed report into the chat.

What it does:
- It only reads:
  - `git` metadata and process information;
  - `psql` queries in read-only transactions, against the database the running worker is
    configured for;
  - the local S3 stand-in's object listing;
  - per-comic Temporal workflow status (structured output, at the configured address and namespace).
- It contacts no external service.
- It writes only two files in your WSL home folder: the script copy and the report.
- It never prints the database URL or any credential: only "SP_DATABASE_URL configured: yes/no".
  From `.env` it prints only variable **names** and the file mode.
- A comic is reported as **lost** only when Temporal is healthy, the namespace exists, and Temporal
  answers that comic's workflow with "not found" or a closed status. Any other failure or unreadable
  answer is reported as **unknown**.

## After step 1: the sequence (commands follow your inspection output)

The concrete commands will be one script, written for your actual revision, encoding and processes,
pinned to a reviewed commit (full SHA, checked out detached; never "pull main"). It stops
automatically, before any further change, on:
- local changes in the checkout (other than the git-ignored `.env`);
- an unexpected schema revision;
- any failed check.

`.env` is only ever read. Nothing below is to be run yet.

**Durable image storage comes first.** `scripts/native-stack.sh` used to run the local S3 stand-in
(moto) in memory, so stopping it deleted every stored image. It now runs a local S3-compatible
server that keeps each object as a file under `$SP_DATA/s3`: Versity S3 Gateway v1.11.0, the same
one `docker-compose.yml` uses, POSIX backend. Persistent storage is required before the paid test.
Images are imported **only into that durable storage**: importing into in-memory storage and then
restarting it would erase them again, and `native-stack.sh stop` now refuses to do that.

| Phase | What happens | Stops automatically if |
|---|---|---|
| A. Prove persistence, isolated | From a separate checkout of the reviewed commit: install the storage server (`scripts/install-versitygw.sh`), then run `scripts/persistence-check.sh`. It uses its own database, bucket, Temporal server and namespace, ports and data folder. It seeds a simulated comic, does a **full** stop and start, then checks every object's SHA-256, retrieves an image through the app's signed URL, confirms the workflow is still running, and approves. Your comics are not touched | any check fails |
| B. Stop writes | Wait until no comic is planning, scripting or drawing and no export is rendering. Stop **only** the web app, API and worker (`native-stack.sh stop-app`). The in-memory storage and the in-memory Temporal **keep running**, so images and waiting workflows are still held | a comic or export is still working; an app process does not exit |
| C. Final backup | `pg_dump` with the app stopped; `s3-snapshot.py export` of every object from the running in-memory storage (all pages, SHA-256 manifest); `.env` copied with mode 600; the current commit recorded | any copy fails or is empty; free space is short |
| D. Verify the backup | Restore the dump into a scratch database. Compare it with the live database table by table: row count **and** a content checksum per table. Run the credit reconciliation on both. `s3-snapshot.py verify` against the running storage must report every object ok, and the snapshot files are re-hashed | any count, checksum, reconciliation or hash differs |
| E. Move images to durable storage | Only now stop the in-memory storage. Start the durable one and import the snapshot; every object is re-read and hash-checked. Then stop and start the durable storage **alone** and verify every object again | any object is missing, unknown or different |
| F. Code and database | Check out the reviewed SHA. If SQL_ASCII, convert into a new UTF-8 database and validate it: content checksums, encoding, spot checks of non-ASCII text, credit reconciliation. Migrate, then validate the pre-existing tables' counts and checksums (original columns) and the credit reconciliation | local changes; HEAD ≠ reviewed SHA; schema before migrating ≠ the inspected revision; any validation differs |
| G. Workflows kept running | Before any new worker connects to the still-running Temporal, download the histories of its running workflows and **replay them against the new code**. Only if every history replays does the new worker start (`start-app`), simulated first | any history fails to replay (the new worker is then not started) |
| H. Live mode | `stop-app`, then `SP_STACK_LIVE=1 start-app`. Run the free preflight, which now fails on in-memory storage. Run `persistence-check.sh --live` (isolated; creates and approves nothing). Sample real images through the app | any check fails |

**Temporal: what can and cannot be preserved.** Your Temporal very likely runs in memory; the
inspection says. An in-memory Temporal cannot be exported. An empty or new Temporal does **not**
restore waiting workflows to their previous state: those comics would show "waiting for approval"
with nothing behind them. To preserve them, Temporal is **not stopped** during the upgrade (phases B
and G). The new worker continues the existing workflows once their histories replay.

Moving to the file-backed Temporal, which the paid test's preflight requires, means stopping the
in-memory one, and that loses whatever is still running or waiting. It is done only when you choose:
- ideally when no comic is waiting;
- otherwise after the read-only recovery dry run has listed each affected comic and you have decided
  what happens to each.

`native-stack.sh stop` refuses to stop an in-memory Temporal unless `SP_ALLOW_TEMPORAL_RESET=1`
is set.

`.env` is never modified.

## Rollback

| Situation | What a rollback does | What it never does |
|---|---|---|
| A check fails before phase E | Nothing to undo. The app is restarted on the old code; storage and Temporal were never stopped | — |
| A check fails in phases E–G, **before any live call** | Stop the app. Restore the phase-C dump into a fresh database and swap the names; the upgraded one is kept as `storypanel_failed_upgrade`. Images stay in the durable storage: it holds the verified snapshot, and the old code reads it the same way. Check out the recorded old commit | Start an **old** worker against a Temporal that a **new** worker has already used: those histories contain the new code's steps and the old code cannot replay them. If a new worker has run, the in-memory Temporal is reset (its waiting workflows are lost, stated explicitly), or the rollback is not done |
| **After any live paid activity** | **No automatic database rollback.** Restoring the backup would erase the records of real provider charges and credits, and restoring a database does not undo an external charge. Fix forward instead. Any manual decision starts from an export of the post-upgrade charge records | Automatic restore. The rollback script refuses when the database holds any non-simulated provider attempt made after the upgrade began |

PostgreSQL, images and Temporal are always returned to **the same point** (the phase-C snapshot), or
not at all.
