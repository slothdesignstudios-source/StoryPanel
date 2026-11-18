# Stage 1A: automatic face location on the existing artwork (approved: USD 0.20 application cap)

**Approved scope:**
- face-location checks on the existing Train and Tulips artwork only;
- no image generation, repairs or stage 1B;
- **one combined USD 0.20 budget for both comics**, including any retry, interrupted or resumed run. It
  is never reset: a rerun starts from what stage 1A has already counted;
- the cap is the app's limit, **not a guaranteed provider billing ceiling**.

**Who runs it.** You, on your computer. This cloud session can't reach your computer, your database or
your keys, so it has made **no** calls.

**Order:**
1. Setup and the free preflight (this document, sections 1–4).
2. You send me both preflight outputs.
3. Only then do I send the paid command (section 6 describes it).

## 0. Pin

Use the full commit SHA given in the handoff message. That commit contains this file, the corrected
`scripts/face-check.py` (combined budget, corrected outcomes) and its tests. Commit 9e78b38 is not
enough: its budget counted only the current run on one comic.

## 1. A separate checkout pinned to that commit

```bash
cd ~/storypanel && git fetch origin main
```

```bash
git -C ~/storypanel worktree add --detach ~/storypanel-1a <FULL-SHA>
```

```bash
git -C ~/storypanel-1a rev-parse HEAD
```

The last line must print the same full SHA.

**What this changes in `~/storypanel`:**
- `git fetch` updates only the remote-tracking refs;
- `worktree add` records the new checkout under `.git/worktrees`;
- your files, your branch and your running processes are untouched.

**Removing it later:**

```bash
git -C ~/storypanel worktree remove ~/storypanel-1a
```

## 2. Its own dependencies (your running install is not modified)

```bash
cd ~/storypanel-1a/services/backend && env -u VIRTUAL_ENV -u UV_PROJECT_ENVIRONMENT uv sync --frozen
```

**What this does:**
- It creates `~/storypanel-1a/services/backend/.venv`, from the pinned `uv.lock`.
- It reuses uv's download cache. `~/storypanel/services/backend/.venv` is not touched.
- Unsetting the two variables stops uv from writing into an activated environment.

**Why not reuse the running install's Python?** StoryPanel is installed *editable*, so that Python
imports the code in `~/storypanel` (cfa4534), not this checkout. The run would then use the wrong
code.

**Check:**

```bash
~/storypanel-1a/services/backend/.venv/bin/python -c "import storypanel; print(storypanel.__file__)"
```

It must print a path under `~/storypanel-1a/`.

## 3. Your existing secrets, without printing them

```bash
ln -s ~/storypanel/.env ~/storypanel-1a/.env && ls -l ~/storypanel-1a/.env | cut -c1-10
```

**How the key is handled:**
- It's a link, not a copy, so the key stays in one file with its existing permissions. The check
  prints only the link's mode, never the contents.
- `.env` is git-ignored, so it can't be committed from the new checkout.
- `scripts/live-env.sh` reads only the key lines (`SP_ANTHROPIC_API_KEY` and the other provider keys)
  from `.env`, as text, never executing or printing it. It then adds `config/live-smoke.env` and the
  local stack's addresses: database `127.0.0.1:5432`, Redis `127.0.0.1:6379`, storage
  `127.0.0.1:9000` (Versity).

Your running stack must be up, started from `~/storypanel` as usual. **Don't start or stop anything
from `~/storypanel-1a`.**

## 4. Comic IDs and the free preflights (no provider call)

```bash
cd ~/storypanel-1a && scripts/live-env.sh services/backend/.venv/bin/python scripts/comic-dossier.py --list
```

This prints id, created, status and title for your 50 most recent comics. It is read-only.

Then, with the two IDs:

```bash
cd ~/storypanel-1a && scripts/live-env.sh services/backend/.venv/bin/python scripts/face-check.py preflight <train-id>
```

```bash
cd ~/storypanel-1a && scripts/live-env.sh services/backend/.venv/bin/python scripts/face-check.py preflight <tulips-id>
```

**Each preflight prints:**
- **Comic and status,** and whether the app would allow paid checks on it. A cancelled, failed,
  expired, blocked or deleted comic is refused.
- **Storage:** the endpoint, whether it is on disk or in memory, and how many images were read back.
  Each image's SHA-256 is compared with the hash recorded when it was saved; any mismatch or missing
  image is a `PROBLEM`.
- **Every panel,** `CHECK` or `skip`, with its expected faces (from the script), its lettering and
  the app's exact reservation for that call.
- **Stage 1A budget so far:** live face-check calls already counted, on either comic, in any run,
  and what remains of USD 0.20.
- **Live mode,** the vision model, and `Anthropic key configured: yes/no`. The key itself is never
  shown.
- **`preflight: PASS`** or `FAIL`.

**Send me both outputs.** Nothing paid runs before that.

### If preflight is blocked

| What you see | Blocker | Smallest safe next action |
|---|---|---|
| A title is missing from `--list` | The records aren't in this database | Run nothing else. Send me the `--list` output; we check which database the app uses before anything else. |
| `PROBLEM … missing` | An image isn't in storage | Run nothing paid; don't regenerate. Send me the PROBLEM lines; I'll give you the read-only dossier command for that comic. |
| `PROBLEM … sha256 differs` | The stored image changed since it was recorded | Same: stop and send the lines. |
| `IN MEMORY` | Storage isn't durable here (`SP_LOCAL_S3=moto` in your shell) | Run `unset SP_LOCAL_S3` in that terminal and repeat the preflight only. |
| `Anthropic key configured: no` | The link or the key line is missing | `grep -c '^SP_ANTHROPIC_API_KEY=' ~/storypanel/.env` (prints a count, not the key). Send me the number. |
| `paid checks allowed on this comic: NO` | The app refuses paid work on this comic | Stop. Don't change the comic's status to get round it. |
| `remaining USD` below one reservation | The combined budget is spent | Stop. Stage 1A ends there. |
| `connection refused` | Your stack isn't running | Start it from `~/storypanel` as you normally do, then repeat the preflight. |

## 5. Safety of the runner (verified in code and by test)

**What `face-check.py` and `comic-dossier.py` never do:**
- **Start workers or contact Temporal.** Neither script calls `Client.connect` or builds a `Worker`.
  The test below replaces `Client.connect` with a tripwire.
- **Apply migrations.** No Alembic call; nothing in the imported code creates tables. The test
  tripwires `alembic.command.upgrade`.
- **Modify comic content.** Comic content is the project, pages, panels, versions, QA results,
  balloons, assets and exports. Both scripts only read it. Outputs go to a new folder, and the script
  refuses an existing one.
- **Start a server, or stop or start your stack.** No metrics server, no `native-stack.sh`.
- **Send telemetry.** Langfuse and Sentry start only from the app's tracing setup, which these scripts
  never call.

**Proof, from a test on a completed comic:** in
`test_the_face_check_writes_only_ledger_rows_and_never_starts_workers_or_migrations`, a simulated run
fingerprints every database table before and after. Only `provider_usage` and `provider_attempts`
change.

**Intended database writes during the paid step (per call):**
- `provider_usage`: one row inserted (the operation), then updated as it settles. If the provider
  serves a different model, the model column is updated.
- `provider_attempts`: one row inserted with the reservation, then its status, tokens and costs
  updated. Stage `face_check`, with your comic's ID and your user ID.

Nothing else is written.

**What it shares with your running app:**
- **Database reads,** plus a brief spend lock (`pg_advisory_xact_lock`, milliseconds) while each call
  reserves its cost.
- **One Redis slot** in the app's vision semaphore (`sp:slots:vision`). If the app is busy, either
  side may wait briefly for a slot.
- **Storage reads** only.
- **The app's spend limits.** These calls count toward your per-comic, per-user and global daily
  limits, like any paid request: about USD 0.009 reserved per call while it runs, about USD 0.001
  once settled.

Don't generate in the app during the run.

## 6. The paid step (command sent after your preflights)

**The approved panels.** The two pages have 11 panels; only the **lettered** ones have balloons to
place, so only those are checked.

| Page | Panel | Lettering | Expected faces (the preflight confirms) |
|---|---|---|---|
| Train | 1 | sound effect | Mira (and Teo if scripted) |
| Train | 2 | Teo, then Mira | Mira, Teo |
| Train | 3 | Mira's thought | Mira: a known infeasible close-up |
| Train | 4 | Teo | Mira, Teo |
| Train | 5 | Mira | Mira |
| Train | 6 | Mira | Mira, Teo |
| Tulips | 3 | Walter | Walter |

That's **7 calls, about 10 faces.**

**The budget, one for both comics:**
- Before every call, the script sums every live `face_check` row already in the ledger, on either
  comic, in every run (failed, interrupted and resumed ones included).
- It adds that call's exact reservation. If the total would pass USD 0.20, it stops and does not retry.
- A rerun starts from what is already counted, never from zero.

**Expected cost:**
- about USD 0.001 per call, roughly USD 0.01 in total;
- about USD 0.065 reserved across the 7 calls while they run.

**The production path:**
1. the v4 panel check's vision call;
2. boxes in the stored QA format;
3. crop-coordinate conversion;
4. the letterer;
5. the PNG renderer and the PDF exporter.

The boxes are written to the output folder, not to the database.

**Manual annotations** (`docs/evidence/quality-2026-11-17/reletter/annotations.json`, in the checkout)
are read only after placement, to compare. They are never an input to detection or placement.

## 7. How the result is reported

Each lettered panel gets exactly one outcome. They are counted separately, never added together:

| Outcome | Meaning | Counts as successful placement? |
|---|---|---|
| `clean_automatic_placement` | every expected face located, no balloon flagged, and the manual annotation shows nothing the flags missed | **yes** |
| `unresolved_defect_detected` | the letterer couldn't place cleanly **and said so** with a confirmed flag (e.g. `covers_face`) | **no**: successful *defect detection* |
| `uncertain_faces_not_located` | a face wasn't located; balloons carry "possibly" flags | no |
| `missed_defect` | the manual annotation shows a covered face, a misattributed tail, a misaligned or misassigned box, or wrong reading order that **no flag reported** | no: a failure |
| `clean_unverified` | no flags, but no manual annotation exists for the panel | no: judge it from the overlay |
| `call_failed` | the call failed (recorded, not retried) | no |

**Train panel 3** is expected to be `unresolved_defect_detected`. That is the correct warning, and it
counts as defect detection, not placement.

**Measurements against ground truth:**
- These are face overlap (IoU), centre error, assignment, balloon cover of a true face, and tail
  attribution.
- They are computed **only where a manual annotation exists**. Train panels 1–6 and Tulips panel 3
  have one, so all 7 are expected to be measured.
- Without an annotation, the report says `"unavailable: no manual annotation for this panel; judge
  from the overlay"`, and no overlap or alignment figure is claimed.

**Pass marks, where measured:**
- box aligned: IoU ≥ 0.3 or centre error ≤ 10% of the panel diagonal;
- correctly assigned: nearer its own true face than anyone else's;
- a balloon covers no more than 15% of a true face;
- every tail ends nearer its speaker than anyone else.

**Stage 1A judgement (reported as two separate figures):**
- **Placement:** how many of the 7 are `clean_automatic_placement`. A flag alone never counts.
- **Detection:** how many defects were correctly flagged (`unresolved_defect_detected`), and how many
  were missed (`missed_defect`). Any missed defect is reported as a failure of the warnings.

## 8. What to send back after the paid step

| File | What it holds |
|---|---|
| `face-check.json` | per panel: faces returned, faces not located, boxes on the displayed panel, balloons with flags, the outcome and any measurements; per call: status, tokens, reserved, counted, calculated and confirmed cost; totals for this run and for stage 1A cumulatively |
| `overlay-p*.png` | the displayed, cropped panel: detected faces green, hand-placed faces magenta, placed balloons and tail tips blue |
| `page-N-before-reading-size.png` and `page-N-after-auto-reading-size.png` | your page as stored, and re-lettered automatically, both 1000 px wide |
| `after-auto.pdf` | the PDF export of the automatic result |
