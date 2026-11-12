# Provider, model and API-version verification — 2026-11-12

**Second pass, same day:** `platform.claude.com` is now reachable from the sandbox. The Anthropic rows
below were re-checked against the official pages, fetched directly; copies are in
`evidence/pricing-2026-11-12/`. `fal.ai`, `docs.fal.ai`, `docs.voyageai.com` and `docs.stripe.com` are
still blocked by the network policy, which was not changed. Their values come only from web-search
results restricted to each provider's own domain, and are **unverified** until someone opens the page.

Verified from official Anthropic pages (fetched 2026-11-12):
- **Prices:** Sonnet 5.5 $2 in / $10 out per MTok. Haiku 5.5 $0.10 / $0.50 (prompts ≤ 100K tokens).
  Sonnet 5 (Sonnet 5.5's only server-side fallback target) $2 / $10. `inference_geo: "us"` costs ×1.1
  (the app does not set it).
- **Output cap:** `max_tokens` is "a hard cap on total output for the request, thinking and response text
  combined" (adaptive thinking page). Thinking is billed as output tokens.
- **Images:** an image costs ⌈w/28⌉ × ⌈h/28⌉ visual tokens, at most 4,784 on Claude 4.7+ models (vision page).
- **Refusals:** refused requests are billed for tokens generated. With server-side fallback, the refusal
  is billed in addition to the fallback request when it arrives mid-stream or is in a billed category.
  Haiku 5.5 has no server-side fallback (refusals-and-fallback page).
- **Token counting** is free (token-counting page).
- **Spend limits:** organisations and workspaces can set monthly spend limits in the Console. Requests then
  return HTTP 400 (rate-limits page). Nothing there says requests already in flight are stopped.

## Models and capabilities

| Role | Model | Verified facts | Source |
|---|---|---|---|
| story (bible, script, repair) | `claude-sonnet-5-5` | current Sonnet; $2 / $10 per MTok; 1M context, 128K output; structured outputs via `messages.parse`; accepts the server-side refusal fallback beta `server-side-fallback-2026-07-01` with `fallbacks: "default"`; forced `tool_choice` and non-default sampling return 400 (the adapter sends neither) | [platform.claude.com pricing](https://platform.claude.com/docs/en/about-claude/pricing), [Sonnet 5.5 overview](https://platform.claude.com/docs/en/models/sonnet-5-5/overview), claude-api reference |
| light (intake, moderation), vision (safety, QA) | `claude-haiku-5-5` | current Haiku; $0.10 / $0.50 per MTok for prompts ≤ 100K tokens ($0.50 / $2.50 above; this app's prompts stay far below); image input; **no server-side refusal fallback** (the adapter only sends the beta to models that accept it) | [pricing](https://platform.claude.com/docs/en/about-claude/pricing), [Haiku 5.5 overview](https://platform.claude.com/docs/en/models/haiku-5-5/overview) |
| generate / edit | `fal-ai/nano-banana-2`, `fal-ai/nano-banana-2/edit` | $0.08 per image at 1K (2K ×1.5, 4K ×2); the app fixes `resolution: "1K"`. A successor `nano-banana-2.1` (token-billed) exists; **not adopted** | [model page](https://fal.ai/models/fal-ai/nano-banana-2/edit), [fal guide](https://fal.ai/learn/tools/nano-banana-api) |
| upscale (every export) | `fal-ai/esrgan` | **billed per compute second, $0.00111/s** — the earlier $0.01/image placeholder was wrong. New pricing unit `per_compute_second`: reserved at rate × wait budget (150 s → $0.1665) and kept *unresolved* until a provider charge is imported | [model page](https://fal.ai/models/fal-ai/esrgan) |
| embeddings | `voyage-multimodal-3.5` | $0.12 per M text tokens, $0.60 per billion pixels, first 200M tokens / 150B pixels free; accepts images | [pricing](https://docs.voyageai.com/docs/pricing), [launch post](https://blog.voyageai.com/2026/01/15/voyage-multimodal-3-5/) |
| inpaint (off for the smoke test) | `fal-ai/flux-pro/v1/fill` | $0.05 per megapixel, rounded up; search flagged the page as old — re-check before enabling | [model page](https://fal.ai/models/fal-ai/flux-pro/v1/fill) |
| fallbacks (off for the smoke test) | Seedream 5.0 Lite edit / text-to-image | $0.035 per image | [fal guide](https://fal.ai/learn/tools/how-to-use-seedream-5-lite) |

Request contracts for every fal endpoint were already checked against `@fal-ai/client` 1.10.1's
published types (`tests/fixtures/fal_schema_fields.json`); live behaviour is unverified until the smoke test.

## Stripe API version

`2026-10-28.endive` is Stripe's current version per its [versioning docs](https://docs.stripe.com/api/versioning)
and [changelog](https://docs.stripe.com/changelog/endive); it is also the version of the installed SDK
(stripe 16.1.0). The app pins it (`SP_STRIPE_API_VERSION`), sends it on every request, and refuses webhook
events in any other version — **set the webhook endpoint (or `stripe listen`) to the same version**.

## Network hosts the live run needs

Anthropic `api.anthropic.com`; fal `queue.fal.run`, `rest.fal.ai`, `auth.fal.ai`, `v3.fal.media` (result
and upload storage; the SDK's hosts); Voyage `api.voyageai.com`; Stripe `api.stripe.com`; Clerk
`api.clerk.com` and your instance's Frontend API host. In this cloud environment these are added in the
environment's network settings (not changed by this session).
