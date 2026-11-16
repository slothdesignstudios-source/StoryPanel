# Comic quality criteria (quality-v1)

These criteria are for **people** reading finished comics. A model may help (for example, by
listing panels where a prop seems missing), but a model's score is never the evidence that quality
improved.

## How to score

- Score each comic on the full-page export at normal reading size: the PNG page on a laptop screen
  at 100%, or the PDF page at its printed size. Don't zoom in to judge readability. Zoom in only to
  check a specific defect you already noticed.
- Score blind. The review sheets show arm **A** and arm **B** in random order per case. The key
  (which arm is the old code) stays in `key.json`, and you open it only after scoring.
- Score every case, including failed or partly drawn comics. A comic that stopped part-way scores
  what is on the page, and the failure is recorded in the run report. Never re-run a case to get a
  better result.
- Use the anchors below. When torn between two scores, give the lower one and write a sentence why.

## The seven criteria (1–4 each)

| Criterion | 4 | 3 | 2 | 1 |
|---|---|---|---|---|
| **Story clarity**: can a new reader follow what happens? | Clear from the pictures alone; the lettering adds to it | Clear with the lettering; one moment needs a second look | One jump or unexplained change confuses | Can't tell what happens |
| **Payoff**: does the ending use the setup? | The last panel lands a reveal or reversal that the first panels made possible | Ends on the premise, but softly | Generic ending (a shrug, everyone laughs) | No ending, or unrelated to the setup |
| **Identity consistency**: the same characters throughout? | Every character instantly the same design in every panel | Small drift (a colour, an accessory) in one panel | A character is hard to recognise in a panel | A character becomes someone else |
| **Prop continuity**: required props and states carry over? | Every required prop present, same shape and colour, in the right hands | One small deviation | A required prop missing or malformed once | Missing or changing repeatedly |
| **Composition**: framing serves each panel? | Each panel's framing fits its moment; clear focal point; no two panels interchangeable | Mostly; one flat or redundant panel | Several panels look alike or cramped; focal point unclear | Panels are near-copies or unreadable |
| **Lettering**: placement, order, legibility | Reads in the right order; tails point at the right speakers; nothing important covered; comfortable size | One awkward placement | A balloon covers a face or key prop, or reads out of order | Hard to read or attribute |
| **Overall readability**: would you read it to the end and get it? | Yes, and enjoy it | Yes | With effort | No |

## Also recorded per comic (from the run report, not scored)

- Panels needing attention, failed or blocked; automatic repairs per panel; repairs stopped because
  the same problem survived.
- Lettering flags on the finished pages (covers face, covers prop, overflow, reading order).
- Story warnings and Story Bible check judgments on the approved script.
- Wall-clock time from creation to the last page, and provider cost (counted at reservation for any
  unresolved call).

## Deciding

An arm is better only if:
- its mean human score is higher on **story clarity, identity consistency and lettering** together;
- no criterion's mean is lower by more than 0.5;
- it isn't worse on failures, needs-attention panels, or cost per comic by more than 25%.

With 7 cases per arm this is a small sample, so report the per-case scores, not only means. Treat a
difference of under 0.5 on a criterion as no difference.
