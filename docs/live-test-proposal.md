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

To have me run it, connect `~/storypanel` to this session through the Claude desktop app (Settings →
Claude Code → Remote Control), if that is offered to you. Otherwise run the one command at the end and
send me `docs/evidence/smoke/live-openai-1/`. It contains no keys.

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

## The spending approval requested

| | |
|---|---|
| **Image provider and model** | OpenAI `gpt-image-2.5-sunburst`, quality `medium`, sizes from 1536×656 to 1536×1536 (long edge 1536), up to 5 reference images per call. It exercises all three keys: Anthropic `claude-sonnet-5-5` / `claude-haiku-5-5` for text and checks, Gemini `gemini-3.8-flash` for identity QA |
| **Prices** | Verified on the official pages 2026-11-15 (Anthropic 2026-11-12): image in $8, image out $30, text in $5 per MTok (2.5, standard) |
| **Expected cost** | **about USD 1.85** at the token counts OpenAI's calculator formula gives (628 output tokens per image) and 7,000 tokens per reference image. **About USD 3.97** if every image uses its full 6,500-token output reservation. OpenAI does not publish 2.5's token counts; this run measures them |
| **Reservation assumptions** | Per OpenAI image call: 7,000 tokens per input image + 6,500 output tokens + prompt. Text calls: input bound + `max_tokens`. Estimated reservation requirement: USD 3.41 (5.52 pessimistic) |
| **Proposed application cap** | **USD 8.00** (a lifetime cap on the smoke user, plus the per-comic ceiling of USD 8) |
| **Overrun beyond the cap** | Only (a) calls that use more tokens than reserved, counted after they return; and (b) unresolved calls whose real charge exceeds their reservation. Timeouts are already counted at their reservation and are not added again. Prices could also change after 2026-11-15. No provider-side hard limit is configured: set an OpenAI project budget and an Anthropic workspace limit if you want one. Details: [`smoke-test-cost.md`](smoke-test-cost.md) §4 |
| **If the budget is exhausted** | The call that would cross the cap is never sent (`spend_limit_reached`, not retried). That step fails without a credit charge, the unused hold is released, and finished panels are kept. The comic ends partly drawn or failed; the run records the error and stops. Nothing is re-sent automatically |
| **Alternative** | Gemini `gemini-3.1-flash-image` (1K): about USD 1.20, requirement 1.74, cap USD 4.00. It leaves the OpenAI key untested |

To approve, reply with the exact words: **"approved: USD 8.00, OpenAI images, run live-openai-1"**.
The command is then:

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
2. **Report.** The comic and all exports are delivered, with an assessment of:
   - prop consistency (is the lantern in every keeper panel?);
   - shot variety;
   - lettering size per page and readability on real art;
   - QA accuracy: each panel's QA verdict against what is actually in the image.
3. **Two separate reconciliations:**
   - user credits: ledger, holds and `check-credits.py`;
   - provider costs: estimated, reserved, calculated and confirmed.

   Measured tokens per image replace the assumptions in the price table.
4. **The 12-credit discrepancy stays open** until the real `reconcile-comic.sql` output is available.
