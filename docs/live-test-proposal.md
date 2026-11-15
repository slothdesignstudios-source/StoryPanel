# Proposed live test: one 3-panel strip (awaiting your spending approval)

**Nothing here has been run against a paid provider.** The same flow passes with simulated providers
(`docs/evidence/smoke/sim-fidelity-openai/`, `sim-fidelity-gemini/`).

## What it covers

One comic from a fixed prompt: a lighthouse keeper who always carries a brass lantern, a seagull,
3 panels. Details are in [`first-live-comic.md`](first-live-comic.md).

| Requirement | How the run exercises it | What the run records |
|---|---|---|
| Persistent visual prop | Continuity rule "The keeper carries a brass lantern" (must, visual). It is checked as a required element in every panel where the keeper is drawn. | `prop_rule`; `panel_qa[]` (status and reasons per panel); the confirmation of the sheet review made before the rule existed |
| Distinct camera shots | One script edit sets wide / medium / close-up. The run fails if the drawn panels do not use them. | `generated_shots`, `final_shots` |
| A script edit | The same edit changes the last panel's action. The new version is re-checked against the Story Bible; blocking issues stop the run before any panel image. | `script_edit` (version, check status, errors, confirmations) |
| Instruction edit | "Make the seagull hold the sandwich in its beak" on panel 1 | `edit_request` |
| All exports | PDF, PNG, CBZ: downloaded and validated, with DPI and lettering-size reports. No paid call. | `exports`, `print_quality` |

## Provider, prices and cap

**Proposed: OpenAI images.** This exercises all three keys: Anthropic for text and checks, OpenAI for
images, Gemini for identity QA. The Gemini-images variant costs less but leaves the OpenAI key
untested.

| Model | Used for | Price assumed | Status |
|---|---|---|---|
| `claude-sonnet-5-5` | Story Bible, script | $2 in / $10 out per MTok | **Verified** 2026-11-12 (official page) |
| `claude-haiku-5-5` | intake, moderation, safety, layout, sheet and Story Bible checks | $0.10 / $0.50 per MTok | **Verified** 2026-11-12 |
| `gpt-image-2.5-sunburst` (medium) | sheets, locations, panels, edit | text in $5, image in $8, image out $30 per MTok | **UNVERIFIED** (search summary) |
| `gemini-3.8-flash` | identity QA | $1.50 / $7.50 per MTok (standard rate) | **UNVERIFIED** |

| | OpenAI images (proposed) | Gemini images (alternative) |
|---|---|---|
| Estimated clean run | about USD 3.33 | about USD 1.20 |
| Smallest cap that can finish | USD 4.53 | USD 1.74 |
| **Application cap to approve** | **USD 8.00** | USD 4.00 |
| Bad case: every panel and the edit use both repair rounds, plus script repairs and a question | USD 7.66 with holds (fits) | USD 2.71 (fits) |
| Worse: every paid call fails once and is re-sent | USD 11.98 (does not fit: the run stops at the cap) | USD 4.65 (stops at the cap) |

The cap is an **application admission limit**: the app refuses a call whose reservation would cross
it. It is not a provider-enforced ceiling.

## Charges that can remain unresolved or exceed the cap

See [`smoke-test-cost.md`](smoke-test-cost.md) §4.

- **An image call that times out (240 s).** It may still be billed. It is counted at its full
  reservation, never re-sent automatically, and stays *unresolved* until you import the provider's
  figure with `live-smoke.py reconcile --charges`.
- **Image output larger than the reservation assumption.** Image output cannot be capped by the
  caller. The overshoot is counted afterwards (`provider_estimate_exceeded`), but the cap did not
  prevent it.
- **A wrong price** in the unverified rows shifts every estimate and reservation for that model.
- **Hidden API-added input** beyond the assumed 4,000 tokens per text call.
- **No provider-side limit is configured by this repository.** For a hard ceiling, set an Anthropic
  workspace limit and an OpenAI project budget yourself. Google budgets only alert.

## Before approving

1. Check the three unverified prices on the providers' pages. If one differs, edit
   `packages/pricing/pricing.2026-11-14.json` and rerun `estimate`.
2. If this machine ran an earlier version, follow [`upgrade-verification.md`](upgrade-verification.md)
   first.
3. `live-smoke.py preflight` must pass. It is free and now includes the database encoding and
   migration checks.

## What one run can and cannot show

- **It can show:**
  - the live integrations end to end;
  - real usage and cost against the estimate;
  - real latency per stage (`scripts/stage-report.py`);
  - whether each check behaves sensibly on real images.
- **It cannot show:**
  - output quality in general;
  - how often the checks are right;
  - readability of lettering on real art across comics.

  Those need a person to look at the results and, later, a larger evaluation sized from this run's
  measured cost.
- **What to look at:** whether the lantern is visible in each keeper panel, whether the three shots
  read as different framings, whether the edit did what it said, and the lettering in the PDF and PNG.

To proceed, reply with an explicit amount, e.g. "approved: USD 8.00, OpenAI images". The command
is then:

```bash
scripts/live-env.sh services/backend/.venv/bin/python scripts/live-smoke.py run --image-provider openai --approve-usd 8.00 --run-id live-openai-1
```
