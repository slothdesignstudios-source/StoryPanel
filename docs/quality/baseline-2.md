# Comic quality baseline 2 (2026-11-17): two real comics made with prompts v3

## Evidence

| Artifact | Real or simulated | Available here |
|---|---|---|
| "The Tulips Remember": finished page (PDF export) | **Real** (live OpenAI images, v3 prompts) | `../evidence/quality-2026-11-17/tulips-page.png` (rendered from your PDF) |
| Tulips: cast and Story Bible screen; script review screen | **Real** (your screenshots) | `tulips-cast.webp`, `tulips-script.webp` |
| "The Last Train Home": finished page (PDF export) | **Real** | `train-page.png` |
| Train: cast and continuity rules screen | **Real** | `train-cast.webp` |
| Compiled prompts, reference inputs, per-panel QA verdicts, lettering flags, costs | Real, but **not available**: stored on your computer only | Run `scripts/comic-dossier.py` for each comic |
| Train script review screen | Not provided | — |

Without the dossiers, these are unknown:
- whether you removed Train rule 4;
- whether the Tulips "doesn't apply" exceptions were ticked;
- what the panel checks said;
- whether the letterer flagged anything.

Findings below say what is **seen** on the page, and separately what the **code** does. A code path is
not claimed as the cause unless the page and the code agree.

## What works (real)

- **Story shape.** Both scripts have a clear setup, development, turn and payoff, and each panel's
  stated purpose is a change, not a label. Dialogue is sparse and natural: one line in Tulips, short
  lines in Train.
- **Identity.** Walter, Mira and Teo stay recognisable in every panel, including extreme close-ups.
- **Locations.** The garden and the café are consistent from panel to panel.
- **Framing.** It varies with purpose (wide arrival, close-up reaction, a large final panel), and no two
  panels are interchangeable.
- **Lettering.** Balloons are mostly above their speakers. Silent panels are free of empty patches.
  The Train thought cloud is readable.

## Defects, highest impact first

1. **The payoff is visible before the turn (both comics).**
   - Seen:
     - Tulips: the script hides the tulips in panel 1 ("tucked in shade"), but they are in full bloom
       in panels 1 and 2.
     - Train: the ticket stub with Mira's portrait, the final reveal, lies on the café table in
       panels 1 to 5.
   - Code, three ways the ending reaches early panels:
     - **Story Bible rules.** The Bible wrote the reveal as a **must** continuity rule tied to the
       location (Train rule 4: "The ticket stub left on the table has a small drawing of Mira on the
       back"), so every café panel is required to show it.
     - **Location references.** The garden reference image and description show the payoff state
       ("beds of red tulips"). The location reference is sent with every panel in that place.
     - **Exceptions.** A rule marked "doesn't apply" to a panel is simply left out of its prompt
       (`art.rules_for_panel`). Nothing tells the image model what is different there.

     These agree with what is seen; which one dominated in each panel needs the dossier.
2. **A balloon covers a face (Train, panel 4).** "Delayed! I can make it!" sits over Teo's head in a
   tall, narrow panel.
   - Code: the letterer only lets a balloon cross the panel border when its text doesn't fit. It
     never does so to avoid a face.
   - Code: lettering flags are not reported with the export, so a face-covering balloon reaches the
     PDF without a warning.
3. **A flagged detail repeats in every panel (Train).** The sheet review said Mira's "pencil tucked behind
   her ear" reads as a hair clip. It was right: she wears a yellow clip in panels 2, 5 and 6. Two
   things made the real flag easy to dismiss:
   - **False alarms.** Every sheet showed "a turnaround view may be cut off or missing" while all
     four turnaround figures were complete. Code: the warning appears when *either* sheet's layout
     can't be verified. Your expression sheets have four edge-to-edge close-ups, which the separator
     can't split. So an expression-sheet result was reported as a turnaround problem.
   - **Small features judged at a glance.** The sheet check sees only the full-body portrait and the
     turnaround. Facial details (Walter's eyebrows, Mira's pencil) are tiny there, hence "couldn't
     tell".
4. **Reading order with reversed speakers (Train, panel 2).** Teo speaks first from the right; Mira
   answers from the left. Her balloon is left and only slightly lower, so it reads first for many
   readers.
   - Code: the letterer requires a later balloon to be only marginally later in reading order.
   - Code: nothing in the script asks the first speaker to stand on the left, and no check warns.
5. **Near-identical compositions (Tulips, panels 1 and 2).** The gate sits in the same place in both.
   - Code: the previous panel is sent as a reference whenever two panels share a location, even
     when a location reference is already there.
   - The cause is not confirmed.
6. **Minor lettering issues.**
   - The Train panel 1 sound effect covers a background figure.
   - The final Train balloon's tail ends near the sun rather than clearly at Mira.
   - The Tulips balloon slightly overlaps the cap brim.
