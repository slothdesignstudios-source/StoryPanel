# Providers: contracts, what was verified, and how

**No live call to any provider has been made** (no credentials, and provider hosts are outside the sandbox
allowlist). "Schema-verified" means checked offline against the provider's published machine-readable schema or
official SDK. See [`blockers.md`](blockers.md) B1–B2 for what a live run needs.

## Selected models and their contracts
| Capability | Endpoint / model | Contract source | Verified facts | Not verified |
|---|---|---|---|---|
| Story, script | `claude-sonnet-5-5` | `anthropic` SDK 2.7 (`messages.parse`, `output_format`, `output_config.effort`, `fallbacks`), Anthropic model table | Model id exists; SDK accepts our call shape; refusal → `stop_reason: "refusal"`; server-side refusal fallback (`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`) is enabled for this model and the *served* model is the one priced | A real request; schema limits with our Pydantic schemas |
| Intake, moderation, vision QA, image safety | `claude-haiku-5-5` | same | Model id exists; accepts images as base64 blocks; no server-side fallback on Haiku (so none is sent) | Judgement quality; bounding-box accuracy |
| Text-to-image | `fal-ai/nano-banana-2` | `@fal-ai/client` 1.10.1 `endpoints.d.ts` (`NanoBanana2Input` → `NanoBananaOutput`) | Fields `prompt`, `aspect_ratio` (15 values), `resolution`, `num_images`, `output_format`, `seed`, `safety_tolerance`, `limit_generations`; result `images[].url` | Behaviour |
| Multi-reference edit (panels, sheets, fixes) | `fal-ai/nano-banana-2/edit` | same (`NanoBanana2EditInput`) | As above plus required `image_urls: string[]` | Reference limit of 14 comes from fal's comparison page and a third-party listing, not the schema. We send at most 4 |
| Fallback edit / text-to-image | `fal-ai/bytedance/seedream/v5/lite/edit`, `…/text-to-image` | same (`SeedDream50LiteEditInput`, `SeedDream50LiteT2IInput` → `images[].url`) | "up to 10 image inputs"; `image_size` as width/height with total pixels between 2560×1440 and 3072×3072; no seed field | Behaviour; that it keeps a comparable style |
| Region repair (inpainting) | `fal-ai/flux-pro/v1/fill` | same (`FluxProFillInput` → `FluxProNewOutput`) | `image_url`, `mask_url` (must match image size), `prompt`, `seed`, `safety_tolerance`, `output_format`; `has_nsfw_concepts[]` is honoured as a block | Mask polarity (we send white = repaint) and seam quality |
| Upscaling | `fal-ai/esrgan` | same (`esrganInput` → `image.url`) | `image_url`, `scale` (default 2), `model` (we use `RealESRGAN_x4plus_anime_6B`), `output_format` | Output quality on comic art |
| Identity embeddings | `voyage-multimodal-3.5` | `voyageai` SDK 0.5 (`multimodal_embed`, `output_dimension`); AWS Marketplace listing and model directories for dimensions | Images are accepted only by multimodal models; output dimension 256/512/1024/2048, default 1024; billed per pixel | Voyage's own docs (unreachable here); similarity behaviour on drawn characters |

The contract table lives in `providers/fal_profiles.py`. `tests/test_providers.py` builds every request body for every
endpoint × 6 aspect ratios × 3 audiences and checks field names, required fields and enum values against
`tests/fixtures/fal_schema_fields.json`, which is extracted by script from fal's package. Live mode refuses to start
with any fal endpoint that has no entry, and with an embedding model that is not on the image-capable list.

## Transport behaviour (fal queue API, from the official Python client)
- `submit` returns a request id immediately; we persist it before waiting.
- `status` returns `Queued`, `InProgress` or `Completed`; a failed job is `Completed` with `error`/`error_type`
  and is treated as a definitive failure (content-policy errors become a moderation block, not a retry).
- HTTP 404 on status means the id is unknown: nothing was produced, a new submission is safe.
- `cancel` is a `PUT` to the request's cancel URL; we call it when a workflow is cancelled mid-request.
- Result URLs are downloaded with an SSRF guard (HTTPS, public addresses only, no redirects, 30 MB cap), then
  decoded, bounds-checked and re-encoded.

## Fallback behaviour
- **Image**: five retryable provider errors within 60 s open a circuit breaker for the primary model for 120 s.
  While open, *new* submissions go to the configured fallback; requests already accepted stay where they are.
  Content refusals never count. Fallback output may differ in look; panels made on it are recorded with its model id.
- **Text**: refusal fallback is server-side and automatic where supported. There is no second text provider.

## Pricing and commercial terms
Prices are operator data with a source and a verification date (`packages/pricing/pricing.example.json`).
Only the three Anthropic rows are taken from a first-party table; **every fal and Voyage figure is an unverified
estimate with an empty `verified_at`, so the example file is rejected as-is**. Live mode will not start, and no
billable call is dispatched, until each configured model has a non-zero (or explicitly free), current price.

Commercial-use and data terms were **not reviewed**: confirm for each provider whether outputs may be used
commercially, whether inputs are used for training (and opt out), and sign a data-processing agreement. Google-model
outputs via fal may carry an invisible SynthID watermark; that is the provider's, not ours.

## Before the first live run
1. Provide keys and model ids (B1) and a verified price file (B5).
2. `uv run python -m storypanel.evals.harness --subset fast --live --max-usd 5` and read the comics.
3. Check: inpaint mask polarity, reference fidelity with 2–3 characters, upscale quality, embedding similarity
   spread. Calibrate `SP_IDENTITY_SIMILARITY_THRESHOLD` (0.55 is a placeholder).
4. Run the bake-off (`evals/rubrics.json`) to confirm primary and fallback.
