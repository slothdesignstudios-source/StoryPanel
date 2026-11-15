# Proposed live test: one 3-panel strip (awaiting a spending approval)

**No paid call has been made.** This is a smoke test of the live integrations, not production
certification. Launch requirements are tracked separately in [`launch-checklist.md`](launch-checklist.md).

## Scope (fixed; not expanded)

One comic from the fixed prompt: a lighthouse keeper who always carries a brass lantern, and a
seagull, as a 3-panel strip. The steps are in [`first-live-comic.md`](first-live-comic.md).
- **Persistent prop:** the rule "The keeper carries a brass lantern" is a required element in every
  panel QA where the keeper is drawn.
- **Script edit before approval:** three distinct shots (wide, medium, close-up) and a changed
  action, then a Story Bible re-check. A blocking issue stops the run before any panel image.
- **One instruction edit** of panel 1.
- **Exports:** PDF, PNG and CBZ. No paid call; DPI and per-page lettering-size reports.

The same flow passes with simulated providers (`docs/evidence/smoke/sim-fidelity-*`).

## Where it runs, and keys

It runs on **your machine**, after [`local-upgrade-procedure.md`](local-upgrade-procedure.md). The keys
stay in `~/storypanel/.env`:
- mode 600, git-ignored;
- read only by `scripts/live-env.sh`;
- never passed to the web process.

This cloud session cannot hold them: the Anthropic host does not accept a cloud network secret here.
**Never paste a key into chat.**

**You run the commands.** This session has no connection to your computer and cannot run anything
there. After the run, send back the folder `docs/evidence/smoke/live-openai-1/` (result, exports, no
keys) and the reconcile output. The report is prepared from those files.

## Before approving: free preflight

Run it on your machine:

```bash
scripts/live-env.sh services/backend/.venv/bin/python scripts/live-smoke.py preflight
```

It must print `preflight: PASS`. It checks, without paid calls:

| Requirement | Check |
|---|---|
| Real providers, no fake fallback | API and **worker process** in live mode, no simulated scenario. The registry has no live-to-fake fallback; live mode refuses to start without keys |
| fal and Voyage disabled | settings and the worker's environment |
| Secrets configured securely | `.env` git-ignored, untracked, mode 600; no key in committed config; each key valid via free model lookups (Anthropic Models API, OpenAI `models.retrieve`, Gemini `models.get`) |
| Workflow state survives a restart | Temporal runs with a database file; `scripts/restart-check.py` (step 5 of the upgrade) proved it with simulated providers |
| Costs and QA recorded per attempt | `provider_attempts` (attempt, status, reserved, counted and calculated cost, tokens, latency, request id), `provider_usage` (stage, target), `panel_versions` (attempt, QA status and scores); the database migrated to head and UTF-8 |
| Network | the three provider hosts are reachable |

## Estimate reconciliation (OpenAI images, clean run)

Prices: verified on the official pages (2026-11-15; Anthropic 2026-11-12). Token counts marked
**UNDOCUMENTED** are assumptions: the provider publishes no figure.

| Calls in a clean run | Count | Token basis | Expected (USD) | Pessimistic (USD) | Reserved before each call (USD) | All counted at reservation (USD) |
|---|---:|---|---:|---:|---:|---:|
| OpenAI `gpt-image-2.5-sunburst` images: 2 portraits, 4 turnaround/expression sheets, 2 locations, 3 panels, 1 edit | 12 | Input per reference image **UNDOCUMENTED**: 7,000 assumed (expected and reserved). Output per image **UNDOCUMENTED**: expected 628 (OpenAI's calculator formula, which OpenAI says does not estimate 2.5), pessimistic and reserved 6,500 | 1.65 | 3.76 | 0.235–0.515 | 4.16 |
| Gemini `gemini-3.8-flash` identity checks (3 panels + the edit) | 4 | Input images 1,120 tokens each (documented). Output about 700 tokens (assumed); reserved at the hard `max_tokens` cap | 0.06 | 0.06 | 0.081 | 0.33 |
| Anthropic text and checks (intake, moderation, Story Bible, script, 2 Story Bible checks, sheet reviews, safety and layout checks) | 27 | Input bound from bytes plus the documented image-token rule; output sizes assumed; reserved at `max_tokens` | 0.14 | 0.14 | 0.009–0.185 | 0.60 |
| **Total** | **43** | | **1.85** | **3.97** | | **5.09** |
| + reservations that can be open at the same moment (the 4 largest: 2 panels or edit at 0.515, a sheet at 0.291, a portrait or location at 0.235) | | | +1.56 | +1.56 | | |
| **Estimated reservation requirement** | | | **3.41** | **5.52** | | |

**Why the numbers differ:**
- **Expected (1.85)** is what a clean run should cost: each call at the token counts above, settled
  from reported usage.
- **Pessimistic (3.97)** changes one undocumented number: every OpenAI image uses its full 6,500
  reserved output tokens instead of 628. Everything else is identical.
- **Reservation requirement (3.41 / 5.52)** is about admission, not cost. Before each call the app
  checks *counted spend + this call's full reservation ≤ cap*. While up to four calls run in
  parallel, their full reservations are held at once, so a cap must exceed the expected total by
  that headroom for a clean run not to be stopped part-way.
- **All counted at reservation (5.09)** is the count if every call stayed at its reservation, for
  example if they all timed out. It is not an extra cost on top: each timeout is counted once, at its
  reservation.
- **The USD 8.00 cap** is above all of these. At expected token counts it also admits every panel
  and the edit using both repair rounds (7.82 including open reservations). In the pessimistic case
  such a run is stopped by the cap (9.93 would be needed). What can exceed the cap is in
  [`smoke-test-cost.md`](smoke-test-cost.md) §4; provider-side controls are separate (§5).

## The spending approval requested

| | |
|---|---|
| **Image provider and model** | OpenAI `gpt-image-2.5-sunburst`, quality `medium`, sizes from 1536×656 to 1536×1536 (long edge 1536), up to 5 reference images per call. It exercises all three keys: Anthropic `claude-sonnet-5-5` / `claude-haiku-5-5` for text and checks, Gemini `gemini-3.8-flash` for identity QA |
| **Prices** | Verified on the official pages 2026-11-15 (Anthropic 2026-11-12): image in $8, image out $30, text in $5 per MTok (2.5, standard) |
| **Expected cost** | **about USD 1.85** at the token counts OpenAI's calculator formula gives (628 output tokens per image) and 7,000 tokens per reference image. **About USD 3.97** if every image uses its full 6,500-token output reservation. OpenAI does not publish 2.5's token counts; this run measures them |
| **Reservation assumptions** | Per OpenAI image call: 7,000 tokens per input image + 6,500 output tokens + prompt. Text calls: input bound + `max_tokens`. Estimated reservation requirement: USD 3.41 (5.52 pessimistic) |
| **Proposed application cap** | **USD 8.00** (a lifetime cap on the smoke user, plus the per-comic ceiling of USD 8) |
| **Overrun beyond the cap** | Only (a) calls that use more tokens than reserved, counted after they return; and (b) unresolved calls whose real charge exceeds their reservation. Timeouts are already counted at their reservation and are not added again. Prices could also change after 2026-11-15. Provider-side limits are separate and optional: OpenAI's *hard* spend limit and Anthropic's spend limit stop new requests after a lag (alerts and budgets only notify); none is configured by this repository. Details: [`smoke-test-cost.md`](smoke-test-cost.md) §4–5 |
| **If the budget is exhausted** | The call that would cross the cap is never sent (`spend_limit_reached`, not retried). That step fails without a credit charge, the unused hold is released, and finished panels are kept. The comic ends partly drawn or failed; the run records the error and stops. Nothing is re-sent automatically |
| **Alternative** | Gemini `gemini-3.1-flash-image` (1K): about USD 1.20, requirement 1.74, cap USD 4.00. It leaves the OpenAI key untested |

To approve, reply with the exact words: **"approved: USD 8.00, OpenAI images, run live-openai-1"**.
This is accepted only after your preflight output shows `preflight: PASS`. You then run:

```bash
scripts/live-env.sh services/backend/.venv/bin/python scripts/live-smoke.py run --image-provider openai --approve-usd 8.00 --run-id live-openai-1
```

## Afterwards (no further paid calls)

1. **Unresolved attempts:**

   ```bash
   scripts/live-env.sh services/backend/.venv/bin/python scripts/live-smoke.py reconcile --run-id live-openai-1
   ```

   This lists unresolved attempts. Import the providers' dashboard figures for them with
   `--charges`.
2. **Report**, prepared from the files you send back: the comic and all exports, with an assessment of:
   - prop consistency (is the lantern in every keeper panel?);
   - shot variety;
   - lettering size per page and readability on real art;
   - QA accuracy: each panel's QA verdict against what is actually in the image.
3. **Two separate reconciliations:**
   - user credits: ledger, holds and `check-credits.py`;
   - provider costs: estimated, reserved, calculated and confirmed.

   Measured tokens per image replace the assumptions in the price table.
4. **The 12-credit discrepancy stays open** until the real `reconcile-comic.sql` output is available.
