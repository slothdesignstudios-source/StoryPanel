# Providers: who does what, contracts, what was verified

StoryPanel runs on three providers. **fal and Voyage are disabled by default.** Their adapters are
kept in the code, but nothing constructs or calls them, and no key is needed.

| Capability | Provider | Model (configurable) | Setting |
|---|---|---|---|
| Story Bible, script, script repair | Anthropic | `claude-sonnet-5-5` | `SP_TEXT_MODEL_STORY` |
| Intake, moderation | Anthropic | `claude-haiku-5-5` | `SP_TEXT_MODEL_LIGHT` |
| Image safety, panel layout/content checks (vision) | Anthropic | `claude-haiku-5-5` | `SP_VISION_MODEL` |
| Character sheets, locations, panels, instruction edits (default) | OpenAI | `gpt-image-2.5-sunburst` | `SP_OPENAI_IMAGE_MODEL`, `SP_OPENAI_IMAGE_QUALITY` |
| Same, when a comic is created with Gemini | Google Gemini API | `gemini-3.1-flash-image` | `SP_GEMINI_IMAGE_MODEL`, `SP_GEMINI_IMAGE_SIZE` |
| Visual character-consistency QA | Google Gemini API | `gemini-3.8-flash` | `SP_GEMINI_QA_MODEL`, `SP_IDENTITY_QA=gemini_visual` |
| Print exports | none (local) | Lanczos resampling + effective-DPI report | `SP_EXPORT_UPSCALE=local` |

Model ids have **no defaults** in live mode. Startup lists every missing setting. `docs/provider-verification.md`
records where each id and price came from and what is still unverified.

## Image provider per comic
- The creator picks **OpenAI** (default, `SP_IMAGE_PROVIDER_DEFAULT`) or **Gemini** on the New comic page.
  Choices not in `SP_IMAGE_PROVIDERS` are shown disabled with the reason.
- The choice and the model id are stored on the comic (`projects.image_provider`, `image_model`) and on
  every paid attempt. Every later image for that comic uses them: sheets, panels, QA repairs, edits and
  retries, including after a restart or a configuration change. If the stored provider is later
  disabled, the comic's image work is refused with a clear message. It is never moved to the other
  provider.
- **A refusal is never routed to another provider.** It is recorded on the attempt and shown to the user.
- Comics created before this change (provider NULL) were drawn with fal. They stay readable and
  exportable; generating more images for them needs fal re-enabled.

## Contracts (from the official SDKs installed: `openai` 3.51.0, `google-genai` 3.3.0)
**OpenAI Images (`providers/openai_image.py`)**
- `images.generate` for images with no input; `images.edit` with up to 16 input images for
  reference-conditioned work. The base image (edits, repairs) goes first, then approved references,
  capped at `SP_IMAGE_MAX_REFERENCES` + 1.
- Size is an explicit `WxH`: both sides divisible by 16, aspect between 1:3 and 3:1, at most
  3840×2160. The app maps each panel aspect to the nearest valid size (`size_for`). Output is base64
  PNG. Usage is reported as text/image input tokens and image output tokens.
- The `mask` parameter is **not used**. Repairs are instruction edits of the whole panel. The app then
  keeps only the repaired region by compositing locally (`pipeline/repair.py`). This is not
  provider-side masked inpainting, and an inpaint request is refused with a clear error.
- A safety identifier is a one-way hash of the user id, never the id or email.
- `x-request-id` is recorded as the provider request id.

**Gemini image (`providers/gemini.py: GeminiImageProvider`)**
- `generate_content` with `response_modalities=["IMAGE"]`, an `image_config` aspect ratio from the
  SDK's list, and `image_size` 1K or 2K. Input images are sent inline: base image first, then
  references, with the same cap. There are no pixel masks, and repairs work as with OpenAI.
- Refusals: a blocked prompt (`prompt_feedback.block_reason`), a non-STOP finish reason, or no image
  part. Each is recorded as a refusal, not a retry.
- Usage comes from `usage_metadata` by modality. Thinking tokens are counted as text output.

**Gemini visual identity QA (`GeminiVisionProvider`, `pipeline/qa.py`)**
- `generate_content` with `response_mime_type="application/json"` and `response_json_schema` from
  `IdentityVisualReport`, plus minimal thinking and a 4,096-token output cap. The panel is sent with
  the approved portrait and front view of each expected character.
- For each character the model reports: present or missing, matches the reference (yes/no/uncertain),
  any mismatches with the aspect, expected and observed values, evidence, confidence, and an overall
  pass/repair/manual_review.
- The app derives its own outcome from the findings and keeps whichever outcome is stricter.
  - Mismatches and missing characters lead to a bounded repair on the same provider. Their notes go
    into the repair prompt.
  - An uncertain result, a model error or an invalid answer leads to **manual review**: the panel is
    kept, flagged "needs attention" and not repainted. It never passes silently.
- This is categorical judgment, not a similarity score. Nothing is written to pgvector, and no Voyage
  thresholds apply.

**Anthropic** (unchanged): `messages.parse` structured outputs, refusal stop reason, a server-side
refusal fallback where the model supports it (switched off for the smoke test), and images as base64
blocks.

## Paid-request safety for synchronous image calls (`metering.generate_image_sync`)
OpenAI and Gemini answer in a single call. There is no queue id to poll later and no way to cancel.
- **Reserve before sending.** The reservation covers the prompt bytes, the input images and the price
  entry's output assumptions. It is checked against every spend ceiling in one serialised
  transaction.
- **SDK retries are off** (`max_retries=0`, `HttpRetryOptions(attempts=1)`). Every resend is a
  visible, separately reserved attempt.
- **Store before success.** The image is stored as a `provider_result` asset before the attempt is
  marked succeeded. A retried activity reuses it and never pays twice.
- **Ambiguous outcomes are never re-sent.** A timeout, a dropped connection, an unexpected exception,
  or a worker that died mid-call (attempt found `submitting` on resume) ends as `ambiguous`/`unresolved`
  at the full reservation. The user sees "retry", which starts a new operation.
- **Outcomes:**

  | Outcome | Attempt status / cost basis | What happens next |
  |---|---|---|
  | Success with complete usage | settled from usage × price | done |
  | Success with incomplete usage | stays unresolved at the reservation | needs reconciliation |
  | Refusal or bad output | `failed`, counted from usage, or by policy if there is none | no retry, no reroute |
  | 5xx | `failed`, counted by policy | a new attempt is allowed |
  | 429 or other 4xx (not processed) | `released` | none |

- **Reconciliation** never queries a synchronous provider: there is nothing to query. Open synchronous
  attempts become `ambiguous` and are resolved from the dashboard with
  `live-smoke.py reconcile --charges`, using the request id or `attempt:<id>`.
- **Account deletion** removes stored provider results and keeps the cost rows.
- **Metrics** (no prompts or images): calls, latency, refusals, rate limits, cost by basis, and
  `provider_unresolved_usd{provider}`. Also `identity_visual{outcome}` and the
  `provider_print_dpi` histogram.

## Retained, disabled: fal and Voyage
`SP_FAL_ENABLED=false` and `SP_VOYAGE_ENABLED=false` by default. While disabled:
- the registry does not construct either adapter (a test checks this in a fresh interpreter);
- startup, preflight, health checks, workers, exports and the smoke test need no fal or Voyage key;
- reconciliation leaves old fal attempts as they are (`provider_disabled`), and account deletion does
  not call fal;
- settings that would reach them are refused at startup with the variable to change:
  `SP_IMAGE_PROVIDERS` containing fal, `SP_REPAIR_MODE=inpaint`, `SP_EXPORT_UPSCALE=fal`,
  `SP_IDENTITY_QA=voyage_embedding`, and fallback models.

To use them again, set the flag, the key and the model ids. The earlier contracts are still in
`providers/fal_profiles.py`, with schema tests against `@fal-ai/client` 1.10.1. Tests of those paths
opt in with the `fal_voyage` fixture.

## Pricing and commercial terms
Prices are operator data with a source and a verification date
(`packages/pricing/pricing.2026-11-15.json`). Live mode refuses to dispatch a call without a current
price. The OpenAI and Gemini prices were verified on the official pages on 2026-11-15
(`docs/evidence/pricing-2026-11-15/`). Not published: tokens per input and output image for
`gpt-image-2.5-sunburst`; the table's reservation values for them are assumptions.

Commercial-use and data terms were **not reviewed**: confirm for each provider whether outputs may be
used commercially and whether inputs are used for training. Gemini image outputs carry Google's
SynthID watermark *(per Google's announcements; unverified here)*.

## Before the first live run
1. Put the three keys in the untracked `.env` ([`setup-checklist.md`](setup-checklist.md)).
2. Run `scripts/live-env.sh services/backend/.venv/bin/python scripts/live-smoke.py preflight`. It is
   free and checks the hosts, keys, model ids and price table.
3. Read [`smoke-test-cost.md`](smoke-test-cost.md) and approve a cap. Then follow
   [`first-live-comic.md`](first-live-comic.md).
