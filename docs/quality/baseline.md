# Comic quality baseline (2026-11-16, before prompts v3)

## What evidence exists

| Source | Real or simulated | What it can show |
|---|---|---|
| Your live comics (Ingrid the lighthouse keeper, and any others) | **Real**, but on your computer only; this session can't see them | Everything. Not inspected yet: run `scripts/comic-dossier.py` (below) and send the folder |
| The cast-approval error you pasted on 2026-11-16 | **Real** model output (a continuity rule the Story Bible writer produced) | One defect, finding 5 |
| `docs/evidence/smoke/*` (7 runs) and the screenshots | **Simulated**: placeholder art from the fake image provider; scripted fixture text | Lettering, layout, exports and pipeline decisions. Nothing about drawing, identity or story quality |
| The code and prompts at fb4a1cc | Deterministic | What is sent to the models and what is decided from their answers |

No real panel image, reference sheet or real script was available in this environment. So every finding
below about prompts says what the code **sends or decides**. It does not claim a prompt caused a visual
problem. Whether these defects show up in real images is what the live comparison must measure.

To give me your real comics (free, read-only, local only, no keys written):

```bash
scripts/live-env.sh services/backend/.venv/bin/python scripts/comic-dossier.py <comic-id> ~/dossier-<name>
```

The dossier holds the idea, Story Bible versions, cast with sheet reviews, scripts with checks, every
panel attempt with its QA verdict, the prompt rebuilt from stored inputs (marked "reproduced" only when
its SHA-256 matches the recorded one), the pages lettered at 2000 px, a contact sheet, and the
newest exports.

## Findings, highest impact first

1. **Lettering space and balloons pointed in different directions.**
   - Prompts v2 asked the picture to leave the side *opposite* a lone speaker empty ("upper right" for a
     speaker on the left: `art.balloon_zone`).
   - The letterer, however, places speech *above and near the speaker* (`lettering.layout_panel` cost
     terms). So the reserved space and the balloon could end up on opposite sides, with the balloon
     over drawn content.
   - Panels with no lettering were still asked to keep the "top edge" empty, and the requested space
     never depended on how much text the panel had.
   - Code fact; the visual effect is unmeasured.
2. **The previous panel was passed as an unlabelled reference** with "Continue directly from the
   previous panel: same place, lighting and outfits", whenever two panels share a location. This is
   the strongest risk for near-copy compositions. Unmeasured: no real images.
3. **Reference images had no stated purpose.** The image model saw "from image 2" with no indication
   that image 2 shows a location rather than a composition to copy, or which view of a character it is.
4. **Story structure was labels, not events.**
   - The script prompt asked for beats, but nothing required each panel to change something, the
     ending to use the setup, or the dialogue to add what the picture can't show.
   - No check caught repeated lines or interchangeable panels. The simulated fixture script repeats
     "I calculated you were hungry." in panels 1 and 3 and passed every check (fixture text, but it
     proves the gap).
5. **Continuity rules with situations inside them (real).** Your live run's Story Bible produced
   "Ingrid carries her brass lantern with a warm golden glow whenever she is out in the fog". A
   reference sheet can't show fog, so the cast was flagged at approval although the lantern was
   drawn correctly in every view. fb4a1cc stopped such rules from flagging; the cause (how the rule
   is written) was still there.
6. **References could be dropped silently.** A drawn character whose matching view was missing got
   no reference image and no warning, and characters beyond the image model's reference limit were
   cut off without a message.
7. **Repairs could be blind or repeated.**
   - A failed check started a new paid attempt even when the reviewer gave no concrete instruction.
   - A third attempt was paid for even when the same problems survived the repair aimed at them. The
     simulated "always fails" case shows 3 attempts per panel.
8. **QA mixed its aspects.**
   - Story (action), required elements, identity and composition problems were one flat list.
   - The letterer didn't know where required props were, so a balloon could cover the lantern the
     story depends on.
9. **Framing ignored the panel's real shape.** The prompt said "4:3 frame" or "9:16 frame" but not what
   that means for the composition. "Close-up" referred to "the main character" without naming them.
10. **Strip lettering at print size.** On the simulated 3-panel strip, text prints at 5.94 pt at the 6-inch
    width, below the 6.48 pt floor. This was already reported per page with the width needed (6.6 in),
    and is unchanged.

What could not be assessed without real outputs:
- actual identity drift;
- prop shape errors;
- anatomy;
- whether panels look alike;
- whether the model honours lettering space;
- how often QA is wrong.
