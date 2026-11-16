# Comic quality: prompts v3 (2026-11-16)

Baseline and evidence: [`baseline.md`](baseline.md). Evaluation set and criteria:
[`../../evals/quality/`](../../evals/quality/). Nothing here has been measured on real images yet; see
"Proposed live comparison" below.

## What changed, and why

All changes are in **prompts v3**:
- New comics use v3.
- Comics already made keep their recorded prompt version. **v2 is frozen byte for byte**: a test pins
  the SHA-256 of a v2 prompt taken from the previous release's code.
- Stored scripts, sheets, balloons and approvals are not recomputed.
- No new model calls were added. The new judgments ride on calls that already happen.

| Area | Change | Baseline finding |
|---|---|---|
| Story | Script prompt: setup, development, turn, payoff expressed as events; every panel states what it changes (`purpose`) and where the eye lands (`focus`); cause before effect; the ending must use the setup; dialogue must add what the picture can't show; the word limit is a ceiling. Contradictions are reported, not silently resolved | 4 |
| Story checks (free, code) | Warnings for repeated lines, panels that would look interchangeable, a page that never changes its framing, a last panel without the final beat, panels without a purpose (v3 scripts only) | 4 |
| Story checks (existing call) | The Story Bible check also judges, as advice: does each panel add something; does its lettering only restate the art; does the ending pay off the setup | 4 |
| Story Bible | Rules are written one checkable fact each, with situations as their own location rule; a carried prop is both a signature feature and a rule, worded identically. Characters get a silhouette and proportions (optional fields; earlier characters unchanged) | 5 |
| Reference sheets | Sheet check: a malformed prop or a silhouette or proportion mismatch is a defect at cast approval | 3, 5 |
| References | Each reference image is listed with its purpose: a character's identity (and which view), the location (not the composition), or the previous panel (only lighting and object state; new camera position). The view follows the shot. A drawn character without an approved reference, or more characters than the model accepts, stops the panel **before** any paid call. A location or previous-panel reference left out for lack of slots is recorded, never silent | 2, 3, 6 |
| Visual plan | One ordered plan per panel: purpose, framing for the panel's real shape (very wide, landscape, square, tall), subjects with identity, pose and expression, relative scale, action, required elements, setting and mood, focal point, corrections, lettering space. Close-ups name whose face; the large panel is the page's strongest moment | 9 |
| Lettering space | One plan used by both the image prompt and the letterer: space above the first speaker (or across the top for several speakers or captions), sized from the actual text at the normal lettering size; none for silent or SFX-only panels | 1 |
| Lettering | Balloons prefer the planned space; required props that QA located are kept clear almost as strongly as faces. If impossible, the balloon is flagged "Covers an important object" in the editor (text is never shrunk below the readable minimum). "Reads out of order" now has an editor label too | 1, 8 |
| QA | Per-panel aspects reported separately (story, required elements, identity, composition), each `ok`, `defect`, `uncertain` or `not_checked`. The reviewer is told to write notes only for confirmed defects, not taste | 8 |
| Repairs | A repair is only requested with concrete notes: every blocking problem without one gets a specific note written from the panel's own spec. Notes keep what already works. If the same problems survive the repair aimed at them, no further attempt is paid for: the panel goes to the user. The existing cap (2) and spending limits are unchanged. Safety refusals are never retried on another provider (unchanged) | 7 |
| Editor | Script panels show "What this panel adds" and "Focal point", editable before approval | — |

Unchanged on purpose:
- design system;
- pricing and credits;
- provider selection;
- paid-request safeguards;
- fal and Voyage stay disabled;
- the layout templates and their choice;
- approved panel order;
- editor, PNG and PDF rendering parity (all three render the same stored balloon geometry).

## Tests run

- **Backend.** Full suite (results in the commit message). New: `tests/test_comic_quality.py`, 23 tests:
  - v2 frozen;
  - v3 plan order and contents;
  - reference purposes;
  - limits and missing references;
  - view by shot;
  - framing by aspect;
  - lettering plan, letterer zone and prop avoidance;
  - story checks, passing and failing;
  - Story Bible judgments as advice;
  - QA aspects;
  - repair notes;
  - repeat-repair stop.

  `test_resilience` now expects 2 attempts per always-failing panel instead of 3.
- **Web.** Typecheck and lint.
- **Harness end to end, simulated.** The A/B harness ran 2 cases on the previous release (fb4a1cc) and on
  this code, built blind sheets, and rebuilt every v3 prompt from stored inputs with matching hashes.
  Output: `evals/quality/runs/sim-check2/` (**SIMULATED**: placeholder art; the counted cost is the
  fakes' price calculation, not money).

## Before/after evidence available now

- **Prompts.** `evals/quality/runs/sim-check2/*/*/dossier/dossier.json` has the exact v2 and v3 prompts for
  the same inputs.
- **Lettering (deterministic code on simulated art).** `blind/dialogue-roadtrip.png`, arm A = new. In the
  large two-speaker panel, the new letterer keeps "Same time tomorrow?" next to its speaker. The old
  one let it drift beside the other character.
- **No real drawing, identity or story evidence yet.** Simulated images can't show whether v3 draws
  better.

## Remaining limitations

- Whether image models follow the visual plan is unmeasured: lettering space, new framing despite a
  continuity reference, focal point.
- QA, the sheet check and the storytelling judgments are model judgments. They can be wrong, and
  their accuracy isn't measured here.
- Not changed this round:
  - thought-cloud shape;
  - balloon padding and line-break rules;
  - tail curvature;
  - SFX styling beyond keeping clear of props, faces and lettering.

  The letterer already handled reading order, tails to speakers, faces, bodies and overflow flags.
- Strips printed 6 inches wide stay below the readable floor; this is reported, not changed.
- The 7-case set is small; differences under 0.5 on a criterion count as none.

## Proposed live comparison (needs your approval; nothing has been run)

**Before running.**
- Persistent storage is required, and your `persistence-check.sh --live` passed on 2026-11-16.
- Run on your machine, old arm first, so no old worker ever sees v3 workflows. Steps:
  [`ab-procedure.md`](ab-procedure.md).

**Round 1:** 3 cases (`prop-lighthouse`, `dialogue-roadtrip`, `twist-cat-fishbowl`) × 2 arms (fb4a1cc vs this
commit). Matched prompts, OpenAI `gpt-image-2.5-sunburst` medium quality, the same text and QA models,
automatic approvals, no edits.

| | Per arm (USD) | Both arms (USD) |
|---|---:|---:|
| Expected | 6.16 | 12.32 |
| Pessimistic (every image at its full 6,500-token output reservation, a repair on every panel and a second on a third of them) | 17.42 | 34.84 |

- Itemised per case: `scripts/quality-ab.py plan --cases prop-lighthouse,dialogue-roadtrip,twist-cat-fishbowl`.
- Image token counts are undocumented by OpenAI. The estimate uses the assumptions in
  `live-test-proposal.md`.
- Each arm runs under its own lifetime spending cap of USD 17.42 on a dedicated evaluation user, and
  each comic stays under the per-comic cap of USD 8.
- Provider-side limits are separate (`smoke-test-cost.md` §5).
- The full 7-case set would be USD 28.38 expected and 82.95 pessimistic. It's only worth running if
  round 1 is inconclusive.

**Recorded per case:**
- status and failures;
- repairs, and repairs stopped as repeats;
- panels needing attention;
- lettering flags;
- story warnings;
- time to the last page;
- counted cost by stage;
- the full dossier and the exports.

You then score the blind sheets with `evals/quality/criteria.md` before opening `key.json`.

To approve, reply exactly: **"approved: quality A/B round 1, USD 17.42 per arm"**.
