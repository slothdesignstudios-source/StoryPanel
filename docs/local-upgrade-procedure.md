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

## Steps 2–7: the corrected sequence (commands follow your inspection)

The commands for these steps will be written for your actual revision, encoding, processes and
data, as one script, `scripts/upgrade-local.sh`. That script stops automatically, before changing
anything further, if any check fails. Nothing below is to be run yet.

**Found while preparing this: images may be lost on every restart.** `scripts/native-stack.sh` runs
the local S3 stand-in (moto) **in memory**, so stopping the stack deletes every stored image. The
database keeps its rows. On this machine, after a restart, the bucket holds 0 objects for 2,537
asset rows. Your inspection's "stored images" section shows whether your installation is already
affected. The upgrade therefore exports the images **before** anything is stopped and imports them
afterwards. Keeping images on disk permanently is a follow-up decision, not part of this upgrade.

| Step | What happens | Stops automatically if |
|---|---|---|
| 2. Stop writes safely | Wait until no comic is planning, scripting or drawing and no export is rendering. Stop the web app, API and worker only. Postgres, S3 and Temporal keep running: S3 and Temporal hold their state in memory | a comic or export is still working; an app process is still running; the database still has connections from the app |
| 3. Final backup (after writes stop) | `pg_dump` in custom format; export **every** object from the S3 stand-in with a manifest (key, size, SHA-256); copy `.env` (mode 600) and the current commit id; if Temporal is file-backed, copy its database file after stopping it. An in-memory Temporal cannot be exported: its waiting comics are recorded from the inspection | any copy fails or is empty; free space is short |
| 4. Verify the backup | Restore the dump into a scratch database. Compare it with the live one table by table: row counts **and** a content checksum per table. Run the credit reconciliation on both. Re-hash the exported objects against the manifest, and compare the object count with the bucket listing | any count, checksum, reconciliation or hash differs |
| 5. Migrate (pinned commit) | Stop S3 and Temporal, since their state is now captured. Check out the **reviewed target commit by its full SHA**, detached; never "pull main". If the database is SQL_ASCII, restore the verified dump into a new UTF-8 database and validate it as in step 4, adding an encoding check; then swap the names. Install dependencies and run `alembic upgrade head` | local changes in the checkout (other than the git-ignored `.env`); HEAD differs from the reviewed SHA; schema revision before migrating differs from the one your inspection showed; a validation differs; head after migrating is not the expected revision |
| 6. Validate and restart | Check that row counts and content checksums of the pre-existing tables are unchanged by the migration, for the columns that existed before. Run the credit reconciliation. Start the stack with simulated providers, import the exported objects, verify count and hashes. Run the free restart check, then switch to live providers and run the free preflight | any check fails |
| 7. Lost workflows | Read-only dry run first. `--apply` and any paid resumption only with your separate approval | (decided by you) |

`.env` is never modified. It is only read, and copied into the backup with mode 600.

**Why one final backup, not an earlier one:** a backup taken while the app is running can miss
writes made after it. If the upgrade needs a database swap (SQL_ASCII) or a rollback, it restores
**only** the final backup from step 3, taken after writes stopped and verified in step 4. An earlier
online backup can be kept as an extra copy, never as the restore point.

**Validating a database conversion (SQL_ASCII → UTF-8):** row counts alone are not enough. The
check also needs:
- a content checksum per table: the md5 of every row's text, ordered by primary key, identical
  in the old and new database;
- no invalid UTF-8: `pg_restore --exit-on-error` refuses such bytes, and any rows containing them
  are listed beforehand;
- spot checks of non-ASCII text;
- identical credit reconciliation results: balances and holds against the ledger.

## Rollback: keep PostgreSQL, images and Temporal at the same point

A rollback returns **all three to the step 3 snapshot**, never a mix:

1. Stop the stack.
2. Rename the upgraded database aside (`storypanel_failed_upgrade`). Restore the step 3 dump into a
   fresh database named `storypanel`. A UTF-8 database is fine for the old code.
3. Start the old code (the commit recorded in step 3). Re-import the step 3 image export into the
   S3 stand-in.
4. Rename the new file-backed Temporal database aside. Its workflows belong to the upgraded data
   and must not run against the restored database. The old version's Temporal starts empty, as it
   did before. Comics that were waiting are in the same state as before the upgrade.

Anything created after the upgrade (comics, images, charges) is not in the snapshot. A rollback
discards it, which is why the snapshot is taken with writes stopped and the upgrade is validated
before you use the app again.
