# First live comic: setup handoff

Everything needed for the paid smoke test, and nothing else (no Clerk, Stripe, AWS, Render or Temporal
Cloud). Never paste a key into chat.

## 1. Secrets (three)

| Variable | Provider page | How the app sends it |
|---|---|---|
| `SP_ANTHROPIC_API_KEY` | Claude Console → API keys | `x-api-key` to `api.anthropic.com` |
| `SP_FAL_KEY` | fal dashboard → Keys | `Authorization: Key …` to `queue.fal.run`, `rest.fal.ai`, `v3.fal.media` |
| `SP_VOYAGE_API_KEY` | Voyage AI dashboard → API keys | `Authorization: Bearer …` to `api.voyageai.com` |

Configure them in **one** of these ways:
- **Your own machine:** an untracked `.env` at the repository root, with the three lines `SP_ANTHROPIC_API_KEY=…`,
  `SP_FAL_KEY=…` and `SP_VOYAGE_API_KEY=…` (`.env` is git-ignored). `scripts/live-env.sh` reads only these
  three variables from it, parsing the file rather than executing it.
- **This cloud session:** the environment's secure network-secrets form, one secret per host with the
  header above. The proxy injects the real value, so `.env` then holds placeholders. I can open the
  form when you say so, and its approval prompt shows exactly what gets saved.

## 2. Network hosts

`api.anthropic.com`, `queue.fal.run`, `rest.fal.ai`, `v3.fal.media`, `api.voyageai.com`. On your machine
nothing needs doing. In this cloud environment they are currently blocked. Saving a network secret for a
host also allows it, which covers all five because each one carries a key.

## 3. Public configuration (already committed; review only)

`config/live-smoke.env`:
- **Models:** `claude-sonnet-5-5` (story), `claude-haiku-5-5` (intake, moderation, safety, vision QA),
  `fal-ai/nano-banana-2` and `fal-ai/nano-banana-2/edit` (1K, 1 image, web search off, minimal thinking),
  `fal-ai/esrgan` (upscale), `voyage-multimodal-3.5` (identity).
- **Smoke-test constraints:** `SP_TEXT_REFUSAL_FALLBACK=false`, `SP_MAX_CHARACTERS=2`, repair mode
  `reference_edit`, failed requests counted as billed.

No provider or model was substituted.

## 4. Prices: what is verified and what is not

| Model | Price used | Status (2026-11-12) |
|---|---|---|
| `claude-sonnet-5-5` | $2 in / $10 out per MTok | **Verified**: official pricing page fetched (`evidence/pricing-2026-11-12/`) |
| `claude-haiku-5-5` | $0.10 in / $0.50 out per MTok (prompts ≤ 100K tokens) | **Verified**: same page |
| `fal-ai/nano-banana-2`, `/edit` | $0.08 per 1K image | **Unverified**: search summaries of fal's model pages; fal.ai is blocked here |
| `fal-ai/esrgan` | $0.00111 per compute second | **Unverified**: same; there is no published run time either |
| `voyage-multimodal-3.5` | $0.60 per billion pixels (first 150B free) | **Unverified**: search summary of Voyage's pricing page |

Other remaining uncertainty:
- Whether fal bills a *running* job after it is cancelled.
- Whether `thinking_level: "minimal"` carries fal's thinking add-on.
- How large the input the API adds on its side is (assumed ≤ 4,000 tokens per call).

Details: [`smoke-test-cost.md`](smoke-test-cost.md).

## 5. Commands

The free check makes no paid calls. It validates settings and prices, looks up the Anthropic models
(Models API), round-trips storage, and confirms the API is running live providers:

```bash
scripts/native-stack.sh stop; scripts/native-services.sh start && SP_STACK_LIVE=1 scripts/native-stack.sh start
```

```bash
scripts/live-env.sh services/backend/.venv/bin/python scripts/live-smoke.py preflight
```

```bash
scripts/live-env.sh services/backend/.venv/bin/python scripts/live-smoke.py estimate
```

The paid run, only after a written approval of a USD amount:

```bash
scripts/live-env.sh services/backend/.venv/bin/python scripts/live-smoke.py run --approve-usd 5.00 --run-id smoke-1
```

**Scope:** one comic from the fixed prompt (a lighthouse keeper and a seagull, a 3-panel strip):
1. Intake and moderation.
2. Story Bible, then two characters' reference sheets with safety checks and identity embeddings, and up to three location references.
3. Script, which must have exactly 3 panels; the run stops before any panel image otherwise.
4. Three reference-conditioned panels with safety and identity QA, up to two repair rounds each.
5. One instruction edit of panel 1.
6. PDF, PNG and CBZ exports, which upscale the three final panels.

The approved amount becomes a lifetime spending cap on the smoke user. If the next reservation would cross it, the run stops.

Evidence and per-stage usage are written to `docs/evidence/smoke/smoke-1/result.json`, with estimated,
calculated, unresolved and provider-confirmed amounts kept separate.
