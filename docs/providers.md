# Providers: what is implemented and what was verified

Documentation sites for fal and Voyage were unreachable from the build environment, and no credentials were
available. Verification therefore came from the installed official SDKs. **No live call to any provider has been made.**

| Capability | Adapter | SDK (pinned in `uv.lock`) | Verified how | Not verified |
|---|---|---|---|---|
| Story, intake, moderation, vision QA, image safety | `providers/anthropic_text.py` | `anthropic` 2.7 | `messages.parse(output_format=…, output_config=…)` signature and error classes checked against the SDK; model ids `claude-sonnet-5-5` / `claude-haiku-5-5` taken from Anthropic's published model table | a real request; structured-output schema limits with our schemas; refusal handling in practice |
| Image generation and editing | `providers/fal_image.py` | `fal-client` 1.0 | queue API (`submit`, `status`, `result`, `upload`) and error types checked against the SDK; endpoint `fal-ai/nano-banana-2/edit` exists with `prompt` + `image_urls` per a web search of fal's pages | the full input schema (aspect ratio enum, seed, output format), reference-count limit, result shape, pricing |
| Upscaling | `providers/fal_image.py` | same | same SDK surface | which upscale endpoint to use and its field names (`UpscaleProfile` is a guess) |
| Identity embeddings | `providers/voyage_embed.py` | `voyageai` 0.5 | `multimodal_embed(inputs, model)` signature checked against the SDK | model name, output dimension (must equal `SP_EMBEDDING_DIMENSIONS`, default 1024), pricing |

## Before the first live run
1. Set every `SP_*_MODEL*` value after checking it on the provider's model list. There are no defaults.
2. Open the fal schema page for the chosen model and correct `ModelProfile` in `fal_image.py` if field names differ.
   The same for `UpscaleProfile`.
3. Confirm the embedding dimension; changing it needs a migration of `character_refs.embedding`.
4. Fill in `SP_PRICE_*` from published rates. Until then cost dashboards and spend limits read zero.
5. Check each provider's terms for commercial use, training opt-out and a data-processing agreement.
6. Run `python -m storypanel.evals.harness --subset fast --live --max-usd 5`, read the comics, then calibrate
   `SP_IDENTITY_SIMILARITY_THRESHOLD` on the golden set (the default 0.55 is a placeholder).
7. Run the bake-off (rubrics.json → `bakeoff`) to choose primary and fallback image models.

## Adding or swapping a provider
Implement the protocol in `providers/base.py`, register it in `providers/registry.py`, add its error mapping to the
`AppError` codes, and run the eval. Pipeline code does not change.

## Data sent to providers
Prompts, scripts and generated images go to the text and image providers; character crops go to the embedding provider.
No account identifiers are sent. Trust, safety and privacy commitments in the product document (DPAs, training
opt-outs, C2PA labelling) are **not implemented in code**: exports carry "AI-generated" metadata only.
