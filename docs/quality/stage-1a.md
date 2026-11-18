# Stage 1A: automatic face location on the existing artwork (approved: USD 0.20 application cap)

**Approved scope:**
- face-location checks on the existing Train and Tulips artwork only;
- no image generation, repairs or stage 1B;
- the USD 0.20 cap is the app's limit, **not a guaranteed provider billing ceiling**.

**Who runs it.** You, on your computer. This cloud session can't reach your computer, your database or
your keys, so it has made **no** calls. The runs below use the production code from a separate
checkout. Your running installation stays as it is.

## 1. A separate checkout (your running app is not touched)

```bash
cd ~/storypanel && git fetch origin
```

```bash
git worktree add --detach ~/storypanel-1a origin/main && ln -s ~/storypanel/.env ~/storypanel-1a/.env
```

No new dependencies and no database migration are needed: neither changed since cfa4534. The
commands below use your existing Python environment.

## 2. Find the two comic IDs (read-only)

```bash
cd ~/storypanel-1a && scripts/live-env.sh ~/storypanel/services/backend/.venv/bin/python scripts/comic-dossier.py --list
```

Use the IDs of "The Last Train Home" and "The Tulips Remember". If either is missing, the records
aren't available. Stop there and run the dossier instead (section 6).

## 3. Preflight: free, makes no provider call

```bash
cd ~/storypanel-1a && scripts/live-env.sh ~/storypanel/services/backend/.venv/bin/python scripts/face-check.py preflight <train-id>
```

```bash
cd ~/storypanel-1a && scripts/live-env.sh ~/storypanel/services/backend/.venv/bin/python scripts/face-check.py preflight <tulips-id>
```

Each preflight checks:
- **Artwork:** every panel image and portrait it will use is read back from storage, and its SHA-256
  compared with the hash recorded when it was saved.
- **Storage:** that it is on disk, not in memory.
- **Panels and faces:** every panel, listed with its expected faces (from the script) and lettering;
  the ones that will be checked are marked CHECK.
- **Cost:** the app's exact reservation for each call, from the real request and images.
- **Credentials:** live mode and the vision model, and whether the Anthropic key is configured
  (yes/no only).

It prints `preflight: PASS` or `FAIL`. **Send me both outputs before running anything paid.**

## 4. Which panels, and why "7"

The two pages have 11 panels. Only **lettered** panels have balloons to place, so only those are
checked. A face check on an unlettered panel tests detection but has nothing to letter.

| Page | Panel | Lettering | Expected faces (from the script; preflight confirms) |
|---|---|---|---|
| Train | 1 | sound effect | Mira (and Teo, if the script includes him in the window) |
| Train | 2 | Teo, then Mira | Mira, Teo |
| Train | 3 | Mira's thought | Mira: **known infeasible** close-up; expected to stay flagged |
| Train | 4 | Teo | Mira, Teo |
| Train | 5 | Mira | Mira |
| Train | 6 | Mira | Mira, Teo |
| Tulips | 3 | Walter | Walter |
| Tulips | 1, 2, 4, 5 | none | not checked |

That's **7 calls and about 10 faces** (11 if Teo is scripted in Train panel 1).

## 5. The paid run, capped

**Train,** at most USD 0.15:

```bash
cd ~/storypanel-1a && scripts/live-env.sh ~/storypanel/services/backend/.venv/bin/python scripts/face-check.py run <train-id> ~/stage1a-train --approve-usd 0.15 --annotations docs/evidence/quality-2026-11-17/reletter/annotations.json --comic train
```

**Tulips,** at most USD 0.05:

```bash
cd ~/storypanel-1a && scripts/live-env.sh ~/storypanel/services/backend/.venv/bin/python scripts/face-check.py run <tulips-id> ~/stage1a-tulips --approve-usd 0.05 --annotations docs/evidence/quality-2026-11-17/reletter/annotations.json --comic tulips
```

Together that's USD 0.20. The script refuses any amount above 0.20.

**Expected cost:**
- about USD 0.001 per call, roughly USD 0.01 in total;
- reserved about USD 0.009 per call (the full output cap), so about USD 0.065 for the 7.

**The cap:**
- Before each call, the run's counted spend plus that call's exact reservation must fit the approved
  amount, or the run **stops there and doesn't retry**.
- A failed call is recorded and not retried.
- The app's per-comic and per-user limits also apply.

**What's written:**
- **Only to the output folder:** the boxes, the evaluation, overlays, pages and the PDF.
- **The paid-request ledger** gets its normal rows for these calls; that's how they're counted.
- **Never changed:** your comics, their QA results and their balloons.

**The production path:** the v4 panel check's vision call, then boxes in the stored QA format, then
the crop-coordinate conversion, then the letterer, then the PNG renderer and PDF exporter.

**The annotations** are read only afterwards, to score the result. They are never an input to
detection or placement.

## 6. If the records aren't available

Stop before any paid step and run the free dossier:

```bash
cd ~/storypanel-1a && scripts/live-env.sh ~/storypanel/services/backend/.venv/bin/python scripts/comic-dossier.py <comic-id> ~/dossier-train
```

`v4-completion.md` §4 lists what it contains.

## 7. What to send back

Send each output folder:

| File | What it holds |
|---|---|
| `face-check.json` | per panel: faces returned, unlocated faces, box problems, balloons with flags, the evaluation against your annotations; per call: status, tokens, reserved, counted, calculated and confirmed cost, unresolved count |
| `overlay-p*.png` | the displayed, cropped panel with detected faces (green), hand-placed faces (magenta) and the placed balloons and tail tips (blue) |
| `page-N-before-reading-size.png` and `page-N-after-auto-reading-size.png` | your page as stored, and re-lettered automatically, both 1000 px wide |
| `after-auto.pdf` | the PDF export of the automatic result |

## How it will be judged

| # | Question | Pass |
|---|---|---|
| 1 | Correct character-to-face assignment | each detected face is nearer its own hand-placed face than any other character's |
| 2 | Box alignment on the displayed, cropped panel | IoU with the hand-placed face ≥ 0.3, or centre error ≤ 10% of the panel diagonal |
| 3 | Balloon overlap and reading order | no balloon covers more than 15% of a true face (hand-placed); balloons in reading order |
| 4 | Tail attribution | every tail ends nearer its speaker's true face than anyone else's |
| 5 | Honest warnings | every face the check didn't locate shows a "possibly" flag; no confirmed flag rests on a guess |

- **A warning alone is not a successful placement.**
- **Train panel 3** passes only if it is flagged; placement there is expected to fail.
- **Stage 1A succeeds** if at least 6 of the 7 panels pass questions 1–4, and question 5 holds
  throughout.
