# Performance and provider cost: what is measured, what is pending

Tool: `scripts/stage-report.py <comic-id>`. It is read-only and covers one comic: calls, attempts,
latency and counted cost per stage, from the paid-request ledger, plus wall-clock time against
summed call time and the panel chains. Simulated runs give exact call counts and structure but
meaningless latencies (the fakes answer in milliseconds). **No live timing exists yet.**

## Call structure of one 4-panel strip (simulated, exact counts)

| Stage | Clean run | Every panel fails its checks (`docs/evidence/perf/stage-report-simulated.json`) |
|---|---|---|
| Character sheets (portrait + turnaround + expressions) | 3 images per character | same |
| Sheet review (new) | 1 vision call per character | same |
| Location references | 1 image each | same |
| Script + Story Bible check (new) | 1 story call + 1 light call | same |
| Panel images | 1 per panel | 3 per panel (2 automatic retries), plus any user redraws |
| Checks per panel image | safety + layout + identity (3 calls, one after another) | same, per attempt |

In the forced worst case: 15 panel images and 45 check calls for 4 panels; panel chains `[3, 1]`
(three panels in one location drawn one after another).

## Where the time goes (from the code; live measurements pending)
- **Panels in one location run in order.** Each uses the previous panel as a reference, so a
  one-location strip is fully sequential. Different locations run in parallel.
- **Checks per image run in order:** safety first (a blocked image is not checked further), then
  the layout check, then the identity check.
- **A failed panel can take three image attempts** before it is marked for review. A prop the model
  cannot draw costs all three on every panel that needs it.
- **A character revision redraws three images** (portrait, turnaround, expressions) and runs a
  safety check and a review on each new sheet.
- Character sheets, location references, and the turnaround and expression sheets already run
  in parallel where nothing depends on them.

## Deliberately not changed (needs evidence first)
| Idea | Why it is pending |
|---|---|
| Run the layout check and the identity check at the same time | Changes the identity check's input (today it checks only the characters the layout check found) and can cost more when a character is missing. Needs the new input contract, a comparison of outcomes, and cost and cancellation checks. |
| Drop or reword the previous-panel reference, so panels can draw in parallel | A quality trade-off: continuity between panels may get worse. Needs paid A/B images. |
| Smaller strip images (1024 instead of 1536 on the long edge) | Cost and quality trade-off; prints at about 1.2 to 1.8 in per panel. Needs paid comparison. |
| Stop retrying when consecutive attempts fail the same way | Saves cost but lowers the chance of a passing panel. Needs live failure data. |
| Lighter revision redraws | The sheets must stay consistent with the new portrait. |

No paid experiment has been run (owner decision 2026-11-14). The first live comic's
`stage-report.py` output is the input for any of these.
