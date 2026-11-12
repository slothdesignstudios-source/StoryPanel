# First live comic: setup handoff

Everything needed for the paid smoke test, and nothing else (no Clerk, Stripe, AWS, Render or Temporal
Cloud). Never paste a key into chat.

## 1. Setup checklist: where the test runs and where the keys go

The test runs **in this cloud session's environment** (personal "Default" environment), not on your
Windows machine. A `.env` on your machine is never visible here. Never paste a key into chat.

1. **Allow one host without a key: `v3.fal.media`.** fal reference uploads use a short-lived storage token,
   not your key, and generated images are downloaded from this host. I propose this as an environment
   network change, and you approve or decline it in the prompt.
2. **Save each provider key as a network secret** in the secure form I open for each host. The proxy adds
   the key to requests for that host only; it never enters the container, the repository or the chat.
   Saving a secret also allows the host. The fal key is entered three times, once per fal host that
   needs it.

| Host | Key | Header the form should use | Used for |
|---|---|---|---|
| `api.anthropic.com` | Anthropic API key (Claude Console → API keys) | `x-api-key`, no prefix | story, moderation, vision QA; free Models API check |
| `queue.fal.run` | fal key (fal dashboard → Keys) | `Authorization`, prefix `Key` | image generation, edit, upscale jobs |
| `rest.fal.ai` | fal key | `Authorization`, prefix `Key` | upload token for reference images |
| `api.fal.ai` | fal key | `Authorization`, prefix `Key` | free official pricing check in preflight |
| `api.voyageai.com` | Voyage API key (Voyage dashboard → API keys) | `Authorization`, prefix `Bearer` | identity embeddings |

3. **Inside the container**, `.env` holds only placeholders for `SP_ANTHROPIC_API_KEY`, `SP_FAL_KEY` and
   `SP_VOYAGE_API_KEY`. They are needed because the app refuses to start without values, and the proxy
   replaces them with the real keys.
4. The secrets are stored on the Default environment, so they also apply to your future sessions on it.
   Remove them afterwards in the environment's settings (Network secrets) if you prefer.

Running the test on your own machine instead: put the three real keys in an untracked `.env` at the
repository root (`scripts/live-env.sh` reads only those three variables) and run the commands in §5
there.

## 2. Network hosts

`api.anthropic.com`, `queue.fal.run`, `rest.fal.ai`, `api.fal.ai`, `v3.fal.media`, `api.voyageai.com`.
Preflight checks each one without credentials and at no cost. Generated images are expected on
`v3.fal.media`. If fal serves a result from another host, downloading it fails without the job being
resubmitted: that image is already paid for, and the run stops after its retrieval retries.

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
| `fal-ai/nano-banana-2`, `/edit` | $0.08 per 1K image | **Unverified** until preflight: search summaries of https://fal.ai/models/fal-ai/nano-banana-2 and https://fal.ai/models/fal-ai/nano-banana-2/edit (fal.ai is blocked here). Preflight compares the table with fal's official pricing API (`GET https://api.fal.ai/v1/models/pricing`, free) and fails on any difference |
| `fal-ai/esrgan` | $0.00111 per compute second | **Unverified** until preflight: https://fal.ai/models/fal-ai/esrgan, checked by the same API. There is no published run time |
| `voyage-multimodal-3.5` | $0.60 per billion pixels (first 150B pixels free) | **Unverified**: search summary of https://docs.voyageai.com/docs/pricing (blocked here). Voyage has no pricing API. At about 0.02B pixels this run is roughly USD 0.01 at the listed price, or free within the allowance |

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
