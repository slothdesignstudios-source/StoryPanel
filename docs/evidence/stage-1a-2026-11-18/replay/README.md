# Free replay: the letterer fix on the existing Train and Tulips artwork

**How it was produced:**
- No artwork was generated or changed, and no provider was called.
- Your comics, their balloons and any balloons you edited by hand are untouched. This writes only to
  this folder.

**What goes in, labelled on every image:**

| Input | Source | Used for |
|---|---|---|
| **Stored detection** | the head boxes the live stage 1A run saved (`../*-face-check.json`) | placement |
| **Script** | characters, positions and dialogue, from your preflight output | placement |
| **Manual annotation** | `../../quality-2026-11-17/reletter/annotations.json` | measuring only, after placement |
| **Art** | the art layer of your exported PDFs (`pdfimages -png`) | the picture |

**The comparison sheets** (`train-comparison.jpg`, `tulips-comparison.jpg`), left to right:

| # | Version | Faces |
|---|---|---|
| 1 | Stored: your export as delivered | guessed at export time |
| 2 | Stage 1A live run | detected; letterer before the fix |
| 3 | Replay | stored boxes; letterer **before** the fix (`lettering_before_fix.py`, commit bb69f70) |
| 4 | Replay | stored boxes; letterer **after** the fix |

**Reproduce:**

```bash
services/backend/.venv/bin/python docs/evidence/stage-1a-2026-11-18/replay/replay.py ART_DIR docs/evidence/stage-1a-2026-11-18/replay/pages docs/evidence/stage-1a-2026-11-18/replay/lettering_before_fix.py
```

## The fix (`services/backend/src/storypanel/pipeline/lettering.py`)

1. **No line on another character's head.** A balloon that reaches down onto another character's head
   is penalised, unless it is also over its own speaker. So is a balloon whose tail would cross another
   face.
2. **Face cover allows for approximate boxes.** `covers_face` now also measures the face with a margin
   of 10% of the head size (`face_margin`). This applies to the warning only, not to placement.
3. **A tail must end clearly nearer its speaker.** It must be nearer by at least 5% of the panel
   diagonal, or it is flagged `tail_ambiguous`.

**Tried and dropped:** two other versions caused regressions on real artwork.
- **Margin in placement as well.** It pushed Train panel 3's thought balloon onto Mira's sketchbook,
  the panel's subject.
- **A flat "over another head" penalty.** It pushed the panel 4 fixture's balloon onto Teo's face.

## Results (measured against the manual annotation)

| Panel | Before the fix (replay = live run) | After the fix |
|---|---|---|
| Train 2 | Teo's line over Mira's head (17% of her face covered); Mira's tail 27.6 vs 26.2 units, nearer Teo; **unflagged** | Teo's line beside Teo, Mira's beside Mira; no face covered; tails 18.1 vs 127.0 and 10.9 vs 131.3 |
| Train 3 | Thought balloon on Mira's hair, flagged `covers_face` | Unchanged (still flagged) |
| Train 1, 4, 5, 6; Tulips 3 | as before | Unchanged |

**Tests:**
- `tests/test_lettering_stage1a.py` covers panel 2's placement, the warning the old placement now gets,
  and that panel 4 is unchanged.
- The lettering suites (174 tests) pass.

## Limitations

- **Heads only.** The live run's body and action boxes weren't saved, so the replay uses heads only.
  Train panels 1 and 5 therefore don't match the live run, even before the fix: the sound effect and
  the "Oh…" balloon land elsewhere. Those two panels say nothing about the fix.
- **Train 2 balloons sit on the characters' bodies,** below their heads, because Teo's head touches the
  top of the panel. That's readable and correctly attributed, but not the classic placement. With body
  boxes available, production may place them differently, which is unverified.
- **Train 3 remains unresolved.** The close-up leaves no clear space, and the sketchbook (the panel's
  subject) was never located as a prop. It is flagged, not fixed.
- **Detection accuracy limits what lettering can do.** The run's box for Mira in Train 2 sits about
  6.5% of the panel height below her real face. The fix avoids the problem by placement, not by better
  detection.
- **Narrow sample.** This is two comics and seven lettered panels. It doesn't show the fix generalises.
- **Not yet in your running app.** The app is at cfa4534, which places balloons using guessed faces.
  Neither this fix nor automatic face location has been deployed.

**Full pages and measurements:** `pages/`. It holds `*-before-fix-reading-size.jpg`,
`*-after-fix-reading-size.jpg` and `replay.json`, which has the flags and the measurements against the
manual annotation per panel.
