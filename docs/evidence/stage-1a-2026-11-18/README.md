# Stage 1A results: automatic face location on the existing Train and Tulips artwork

**How it was run:**
- Run by the owner on their machine, 2026-11-18, from commit `15eeef8735785014452fb80680e107c5e5547358`.
- `face-check.py`, live, combined application cap USD 0.20.
- The comics themselves were not changed.
- `*-face-check.json` are the run's own outputs. The `.webp` pages are screenshots of its 1000 px
  reading-size renders: "before" is the page as stored, "after-auto" is re-lettered from the detected
  boxes.

## Calls and cost

| | Train | Tulips | Stage 1A total |
|---|---|---|---|
| Calls (all succeeded) | 6 | 1 | 7 |
| Reserved while running | USD 0.057899 | USD 0.009516 | USD 0.067415 |
| Calculated (tokens × the app's price table) | USD 0.010913 | USD 0.000984 | **USD 0.011897** |
| Provider-confirmed | none yet | none yet | none |
| Unresolved | 0 | 0 | 0 |
| Input / output tokens | 54,627 / 10,900 | 8,015 / 365 | 62,642 / 11,265 |

**Budget:**
- Counted: USD 0.011897 of the 0.20 cap. Nothing was retried, and no run stopped at the cap.
- The calculated figure is not a provider bill; the provider's billing is authoritative.
- Per call it was about USD 0.0017, above the USD 0.001 estimate, because the v4 check returns a full
  report (1.4–2.2k output tokens on Train).

## Outcomes, reported separately

| Outcome | Panels |
|---|---|
| Clean automatic placement | Train 4, 5, 6; Tulips 3 (**4 of 7**) |
| Unresolved defect correctly flagged (detection, not placement) | Train 1 (`sfx_covers_character`), Train 3 (`covers_face`) |
| Missed defect | Train 2 |
| Faces not located, call failed, unverified | none |

## Detection

- **Faces returned:** all 11 expected faces were returned with boxes.
- **Faces measured:** 10. Train panel 1's Teo has no manual annotation, so his measurement is
  unavailable; the overlay is the evidence.
- **Assignment:** all 10 measured faces were assigned to the right character.
- **Alignment:** all 10 pass, but two pass only on the centre-error criterion:

| Panel | IoU | Centre error |
|---|---|---|
| Train 4, Mira | 0.28 | 0.008 |
| Train 6, Teo | **0.02** | 0.071 |

Train 6's Teo box doesn't overlap the hand-placed one, so either the box or the annotation is off. It
needs the overlay; it didn't affect placement, because Mira is the speaker.

## Lettering, against the stored pages (observed in the reading-size renders)

| Panel | Stored page | Automatic result |
|---|---|---|
| Train 4 | Teo's head is hidden behind "Delayed!" | Teo's face clear; tail on Teo. **Improved.** |
| Train 6 | Balloon in the sky | Balloon in the sky, tail to Mira. Similar. |
| Train 5, Tulips 3 | Clean | Clean. Tulips' tail now points at Walter's face rather than his cap, but the balloon sits close to his brow (6% of the face covered). |
| Train 1 | SFX across the window where Teo runs | Same, and now flagged. |
| Train 3 | Thought balloon over Mira's hair | Same, and flagged `covers_face` (38%). |
| Train 2 | Teo's line above Teo; Mira's between them | **Worse.** Teo speaks first, so his balloon went top-left over Mira's head (18% of her face covered). Mira's line went top-right beside Teo, and its tail ends nearer Teo (27.6 vs 26.2 units). **Neither was flagged.** |

**Train 2, likely cause** (hypothesis, not verified):
- **The crossing.** The speakers are on the opposite sides from their speaking order. Reading order
  pushed the first speaker's balloon left, and the letterer had no cost for a tail crossing to the far
  side.
- **The missing flags.** The letterer measured cover and tail distance against the *detected* boxes,
  which differ from the hand-placed ones. So the warnings didn't fire.

## Conclusion

- **Detection is good enough to use:** 11 of 11 faces returned, and 10 of 10 measured faces
  correctly assigned.
- **The remaining errors are in the letterer:** speaker-side crossing, and warning thresholds measured
  against the detected boxes.
- **The boxes are stored in the JSON,** so letterer fixes can be replayed on them with no further calls.
