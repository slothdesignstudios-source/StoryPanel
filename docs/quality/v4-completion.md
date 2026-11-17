# Prompts v4: completion pass (2026-11-17)

This follows [`v4.md`](v4.md). Nothing here used a paid call. Every image is labelled with how it was
produced.

## 1. The automatic face-position path

**Traced:**

    panel check (vision call)                          boxes as fractions of the GENERATED IMAGE
      -> qa.evaluate_panel -> PanelVersion.qa_scores    head_boxes, body_boxes, action_box, prop_boxes, faces_unlocated
      -> stages_art.letter_panel                       image -> panel mapping through the renderer's crop; bad boxes refused
      -> lettering.layout_panel                        located faces avoided; guessed faces reported as uncertain
      -> Balloon (geometry, flags, style.basis)         basis = which picture and which faces the placement used
      -> editor / PNG / PDF                            the same stored geometry; export lists flags, marks stale balloons

**What it found and fixed:**

| Finding | Status | Fix |
|---|---|---|
| The check's boxes are fractions of the generated image; the letterer used them as fractions of the panel. The renderer crops the image to fill the panel (Train p6: a 16:9 image in a 920×565 slot loses about 4.6% of its width on each side) | **Code-confirmed** | Mapped through the same crop (`lettering.image_box_to_panel`) using the stored image size |
| Malformed or out-of-range boxes (missing keys, text, negative sizes, percentages or pixels) went straight into placement | Code-confirmed | Refused and recorded (`box_problems`). That face counts as not located, so overlaps with it are reported as uncertain, never trusted |
| A face cropped out of the visible panel | Code-confirmed | Dropped (`cropped_out`) |
| Balloons were never re-placed when a panel's picture changed (regenerate or edit), so they could sit over faces that had moved | **Code-confirmed** | Untouched balloons are placed again for the new picture. Balloons you moved by hand are never moved; they're flagged "Picture changed after you placed this". An export marks balloons placed for a different picture (for example after restoring a version) |
| No record of which faces a placement used | — | Each balloon stores its basis: picture version, and faces located, partial or guessed |
| The check sometimes returns no face boxes | Unknown how often | The v4 check prompt requires boxes for every visible character, and `faces_unlocated` is recorded per attempt |

**Root cause of the exported overlaps: strongly supported by reconstruction, not confirmed.** The cfa4534
letterer reproduces the exported balloon positions to within 1–14 page units using *guessed* faces,
and misses by 52–128 using faces read off the art. This holds until your generation records show
what the check actually returned (`head_boxes`, `faces_unlocated` in the dossier).

**Tests on the real Train artwork, all free** (`tests/test_face_pipeline.py`). A panel-check answer is
**simulated**: boxes written in image coordinates, standing in for the model. It is then run through
the real QA scoring, storage format, conversion, letterer, PNG renderer and PDF exporter.
- **Panels 4 and 6, faces located:** no face overlap, and tails end within 5% of the panel width of
  their speakers.
- **Conversion:** without the crop mapping, Teo's box in panel 6 is off by more than 3% of the panel
  width.
- **The original failure, kept:** no boxes means the balloon still covers Teo's face, now flagged
  "possibly covers a face", never passed as clear.
- **Bad and unknown inputs:** malformed, out-of-range, cropped-out and unknown-size boxes are each
  handled explicitly.
- **PDF placement:** the PDF puts the balloon text where the letterer placed it, within 6 pt.
- **Stale lettering (database level):** untouched balloons are placed again; hand-placed balloons are
  kept and flagged.

The older placement tests now supply face positions and **also** assert that, without them, the same
placement is reported as uncertain, so the original failure stays covered.

## 2. Lettering on your existing pages

[`../evidence/quality-2026-11-17/reletter/`](../evidence/quality-2026-11-17/reletter/). Every image
is stamped at the top with how it was produced:

- **"AUTOMATIC: faces guessed from the script (as exported)":** what the pipeline does when the check
  located no faces. Placement matches the export; every affected balloon is now flagged as uncertain.
- **"MANUAL face positions typed in by hand":** what the letterer does once faces are known. **This is
  not the automatic pipeline.** The positions are mine, read off the art.

Train p6's annotation was corrected after a finer measurement: Mira's head is at 24–35% across,
31–44% down.

Measured with the manual face positions (page units, 1000-unit page):

| Train panel | Tail end to speaker's face | To nearest other face | Result |
|---|---:|---:|---|
| 2, Teo | 7.3 | 120.7 | clear; reads first (higher) |
| 2, Mira | 8.5 | 131.4 | clear; reads second (clearly lower, left) |
| 4, Teo | 6.2 | 87.8 | clear; balloon in the free window area, face uncovered |
| 6, Mira | 3.5 | 577.1 | the tail now reaches her head. At reading size the sun sits right beside her hair, so at a glance it is still a borderline read |
| 3, Mira | inside her face | — | **unresolved: "covers a face" (confirmed).** The close-up leaves no space wide enough for the cloud at the readable size; narrower shapes were tried. Choices: shorten the thought, move it over the sketchbook, or redraw the panel with room above her head |

Two more observations at reading size:
- Panel 2's reply sits over Mira's sketchbook, a required prop I didn't mark in that panel.
- Panel 1's sound effect now covers the chair and Mira's lap rather than the figure in the window.

**The page is not "fixed".** With faces known, panels 2, 4, 5 and 6 are clear, and panel 3 is
reported, not solved.

**Whether the automatic check will locate these faces is untested.** That is the first paid step below.

## 3. Reveal-state semantics

- **Not required vs opposite.** "Not required here" and "the opposite holds here" are now different
  choices per rule in the script editor.
  - **Not required:** the prompt says "Not required in this panel: … (show it only if the action calls
    for it)", and the location reference is kept in full.
  - **Opposite holds:** "Opposite in this panel: … does not hold here." The reference is limited to
    architecture and light, and the panel check is told the statement must not be true in the
    picture.
  - Old exceptions keep working: an "opposite" rule is also recorded as an exception.
- **Rule vs approved timing.** The approved timing wins in that panel. You confirmed this when
  approving, because the conflict was a review item, and its wording now says which instruction wins.
  - The prompt carries only the timing instruction, never both.
  - The panel check isn't asked for the rule.
  - The decision is recorded per attempt (`rules_yielded`).
- **References.**
  - Character identity references are never removed.
  - A location or previous-panel reference is left out only when its wording conflicts, with the
    reason recorded (`references_omitted`).
- **Conflict detection is word matching, and incomplete.** Tests document:
  - a paraphrase that is **missed** ("his train ticket" vs "the ticket stub");
  - a negation that is treated as agreeing ("Mira never carries the ticket stub");
  - plurals, which are folded.
- **Same retry loop.** Reveal-timing repairs use the existing panel loop: the same cap (first attempt
  plus 2), the repeat-stop, and the same metering and spending limits. A hidden element drawn on
  every attempt gets one repair, then the panel is delivered as needing attention (tested).

## 4. Getting your records: the dossier

**Find the comic IDs.** The ID is in the address bar when a comic is open (`/projects/<id>`). Or list
them, read-only:

```bash
scripts/live-env.sh services/backend/.venv/bin/python scripts/comic-dossier.py --list
```

This prints ID, date, status and title for the 50 most recent comics in your local database, all
accounts on that machine included. It prints no emails.

**Then, for each comic:**

```bash
scripts/live-env.sh services/backend/.venv/bin/python scripts/comic-dossier.py <comic-id> ~/dossier-train
```

```bash
scripts/live-env.sh services/backend/.venv/bin/python scripts/comic-dossier.py <comic-id> ~/dossier-tulips
```

**What a dossier contains** (local reads only, no provider calls, nothing written to the database):

- **Your creative content:**
  - your original idea (prompt text);
  - every Story Bible version;
  - the cast specs and sheet reviews;
  - every script version with its issues and checks;
  - the dialogue;
  - your artwork: reference sheets, every panel attempt, lettered pages, exports.

  **This is sensitive creative content.**
- **For every panel attempt:**
  - provider and model;
  - prompt version and hash;
  - the prompt rebuilt from stored inputs, marked "reproduced" only when the hash matches;
  - reference asset IDs and roles;
  - the QA verdict, problems, notes and aspects;
  - the face, body, action and prop boxes the check returned, and `faces_unlocated`;
  - cost and latency.
- **Balloons:** each with its geometry, flags and placement basis.
- **Automatic re-lettering:** `pages/page-N-relettered-auto.png`, each page re-lettered by the current
  code from the boxes your checks stored (no hand-placed faces), with a per-panel report.
- **`review_flags`:** conflicts in the approved script found with today's checks. Reported only;
  nothing is changed.

**What it doesn't contain:**
- API keys or credentials;
- signed URLs or storage keys (verified on a simulated dossier: no URLs, no signed-URL parameters, no
  email addresses);
- account emails or owner IDs;
- other comics or other accounts' records.

Please look through it before sharing. It's your work.

## 5. Smaller first stage (proposal; nothing paid has run)

This tests the two things the free work can't: whether **automatic** face detection fixes the lettering
on real art, and whether **reveal timing** holds in one real generation. There's no A/B in this stage.

### A. Automatic faces on your existing pages, no image generation

```bash
scripts/live-env.sh services/backend/.venv/bin/python scripts/face-check.py <train-id> ~/face-check-train --approve-usd 0.10
```

```bash
scripts/live-env.sh services/backend/.venv/bin/python scripts/face-check.py <tulips-id> ~/face-check-tulips --approve-usd 0.10
```

- **Calls:** one v4 vision check (`claude-haiku-5-5`) per lettered panel: Train 6, Tulips 1. Each
  sends the panel image and the speakers' approved portraits. No safety or identity call, no image
  generation.
- **Reused:** the existing panel images and approved portraits.
- **Output:** the returned boxes, which faces were located, box problems, and each page re-lettered
  automatically from those boxes. Your comic, its QA records and balloons aren't changed.
- **Cost:** expected about USD 0.001 per call, so **about USD 0.01** in total. Reserved up to USD 0.009
  per call (16,000 output tokens): **cap USD 0.10 per comic, USD 0.20 for both**. Prices are verified
  for `claude-haiku-5-5` at $0.10 in and $0.50 out per million tokens; token counts are estimates.
- **Success:**
  - faces are located for every visible character in at least 6 of the 7 panels;
  - the automatic re-lettering has no confirmed face overlap except Train p3 (known infeasible);
  - each located face's centre is within 10% of the panel size of the manual position.

### B. One new v4 comic targeting reveal timing and automatic lettering

```bash
scripts/live-env.sh services/backend/.venv/bin/python scripts/quality-ab.py run --run-id stage1 --arm new --commit "$(git rev-parse HEAD)" --approve-usd 4.65 --cases stage1-reveal-strip
```

**Case `stage1-reveal-strip`:** Mira hides a wrapped gift under Teo's cup; he finds it only in panel 3.
A 3-panel strip with 2 characters and a few short lines.

**Calls in a clean run:**

| Provider | Calls |
|---|---|
| Anthropic | intake, Story Bible, 2 sheet checks, script (+0–2 repairs), the Story Bible check, and per panel an image-safety and a vision check |
| Gemini | 1 identity check per panel |
| OpenAI images | 2 portraits, 2 turnarounds, 2 expression sheets, 1 location, 3 panels = **10 images** |

Repairs follow the existing cap: at most 2 per panel, and fewer once the repeat-stop applies.

- **Reused:** nothing. A new comic needs its own cast; reusing a cast across comics is not supported.
  Your existing comics can't be redrawn with timing states, because their scripts are already
  approved.
- **Cost (app model):** **expected USD 1.75**, worst case USD 4.63. The worst case is 15 images at the
  full 6,500-token output reservation (repairs included) plus text and checks.
- **Caps:**
  - a lifetime cap of **USD 4.65** on a dedicated evaluation user (the app stops starting calls there);
  - the per-comic cap of USD 8;
  - provider-side limits are separate;
  - image token counts are undocumented by OpenAI; the estimate uses the assumptions in
    `live-test-proposal.md`.
- **Success:**
  - the script gives the gift a state in every panel;
  - by **human** judgment, the gift isn't visible in panels 1–2 (or is only a small hint, if marked
    "unnoticed") and is clearly visible in panel 3;
  - the panel check's verdict agrees with the human judgment in each panel;
  - faces are located for every character in every panel;
  - no confirmed face overlap;
  - every tail ends nearer its speaker than anyone else.

### Totals and what this can establish

| Stage | Expected (USD) | App caps (USD) |
|---|---:|---:|
| A: face check on both existing comics | ~0.01 | 0.20 |
| B: one v4 comic | 1.75 | 4.65 |
| **Stage 1** | **~1.76** | **4.85** (vs 36.91 for the full A/B) |

**It can establish:**
- whether the automatic chain works with real model output: boxes, conversion and placement;
- whether one real generation honours one hidden/revealed timing;
- whether the panel check's element verdicts match a person's on those panels;
- the real token counts and cost of these calls.

**It can't establish:**
- that v4 beats v3 (there's no comparison arm);
- any rate (one comic, one run);
- composition repetition (one strip, one location, three panels);
- performance on other stories or styles.

Expand to the v3-vs-v4 comparison ([`v4.md`](v4.md), USD 18.45 per arm) only if stage 1 works end to
end. A failure in stage 1 says what to fix first, more cheaply.

To approve stage 1, reply exactly: **"approved: stage 1, USD 0.20 face check + USD 4.65 one v4 comic"**.
You may approve A alone: **"approved: stage 1A face check, USD 0.20"**.
