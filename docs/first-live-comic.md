# First live comic: setup handoff

Everything needed for the paid smoke test, and nothing else: no Clerk, Stripe, AWS, Render or
Temporal Cloud, and **no fal or Voyage account**. Never paste a key into chat.

> **Status (2026-11-15):**
> - The app runs on Anthropic, OpenAI and Google Gemini.
> - fal and Voyage are disabled by configuration and need no key.
> - No paid call has been made. Everything below was exercised only with simulated providers
>   (`docs/evidence/smoke/sim-fidelity-openai/`, `sim-fidelity-gemini/`).
> - Before a live run on a machine that ran an earlier version, follow `docs/upgrade-verification.md`
>   (database encoding, migration, comics whose workflow was lost).

## 1. Where the keys go

The test runs on **your own machine** (Windows + WSL 2, see [`RUN-LOCALLY.md`](RUN-LOCALLY.md)).
Put the keys in an **untracked** `.env` at the repository root inside WSL, for example
`~/storypanel/.env`. Git ignores this file. `scripts/live-env.sh` reads only these lines from it:

```bash
SP_ANTHROPIC_API_KEY=...
SP_OPENAI_API_KEY=...
SP_GEMINI_API_KEY=...
# optional: make Gemini the default image provider on the New comic page
# SP_IMAGE_PROVIDER_DEFAULT=gemini
```

| Key | Where to create it | Used for |
|---|---|---|
| `SP_ANTHROPIC_API_KEY` | console.anthropic.com → API keys | story, script, moderation, image safety, layout checks |
| `SP_OPENAI_API_KEY` | platform.openai.com → API keys (project key) | images when a comic uses OpenAI (default) |
| `SP_GEMINI_API_KEY` | aistudio.google.com → Get API key (on a billed project) | identity QA for every comic; images when a comic uses Gemini |

Keys never reach the browser bundle. The web process starts with them unset, and
`scripts/check-bundle-secrets.sh` scans the bundle for canaries. Logs and error reports redact
Anthropic, OpenAI and Google key shapes.

Keys in old `.env` files: `SP_FAL_KEY` and `SP_VOYAGE_API_KEY` are ignored while fal and Voyage
are disabled. You can delete them.

## 2. Network hosts

`api.anthropic.com`, `api.openai.com`, `generativelanguage.googleapis.com`. Preflight checks each
one without credentials and at no cost. On a home connection there is nothing to configure.

## 3. Public configuration (committed; review only)

`config/live-smoke.env`:
- **Text:** `claude-sonnet-5-5` (story), `claude-haiku-5-5` (intake, moderation, safety, vision checks).
- **Images:** OpenAI `gpt-image-2.5-sunburst` (quality `medium`) by default. Gemini
  `gemini-3.1-flash-image` (1K) when the comic is created with Gemini.
- **Identity QA:** Gemini `gemini-3.8-flash`, which compares each panel with the approved reference
  sheets.
- **Exports:** native images, local resampling, effective-DPI report (`SP_EXPORT_UPSCALE=local`).
- **Off:** `SP_FAL_ENABLED=false`, `SP_VOYAGE_ENABLED=false`.
- **Smoke-test constraints:** `SP_TEXT_REFUSAL_FALLBACK=false`, `SP_MAX_CHARACTERS=2`, repair mode
  `reference_edit`, failed requests counted as billed, image request timeout 240 s (no automatic
  resend after a timeout).

## 4. Prices: what is verified and what is not

| Model | Price used | Status |
|---|---|---|
| `claude-sonnet-5-5` | $2 in / $10 out per MTok | **Verified** 2026-11-12 (official page fetched) |
| `claude-haiku-5-5` | $0.10 in / $0.50 out per MTok | **Verified** 2026-11-12 |
| `gpt-image-2.5-sunburst` | text in $5, image in $8, image out $30 per MTok | **UNVERIFIED** (search summary of developers.openai.com) |
| `gemini-3.1-flash-image` | in $0.50, text/thinking out $3, image out $60 per MTok | **UNVERIFIED** (search summary of ai.google.dev) |
| `gemini-3.8-flash` | $1.50 in / $7.50 out per MTok (standard rate; introductory rate is half until 2026-12-31) | **UNVERIFIED** |

Check the three unverified prices on the providers' pages before approving. If one differs, edit
`packages/pricing/pricing.2026-11-14.json`, then rerun `estimate`.

| Images from | Estimated clean run | Smallest cap that can finish | Suggested approval |
|---|---|---|---|
| OpenAI | about USD 3.33 | USD 4.53 | USD 8.00 |
| Gemini | about USD 1.20 | USD 1.74 | USD 4.00 |

Details, worst cases and what can be billed beyond the cap: [`smoke-test-cost.md`](smoke-test-cost.md).

## 5. Commands

Start the stack with live providers:

```bash
scripts/native-stack.sh stop; scripts/native-services.sh start && SP_STACK_LIVE=1 scripts/native-stack.sh start
```

Free checks: hosts, settings, price table, storage, the API mode, and free model lookups for each
key (Anthropic Models API, OpenAI `models.retrieve`, Gemini `models.get`):

```bash
scripts/live-env.sh services/backend/.venv/bin/python scripts/live-smoke.py preflight
```

```bash
scripts/live-env.sh services/backend/.venv/bin/python scripts/live-smoke.py estimate --image-provider openai
```

The paid run, **only after you approve a USD amount in writing**:

```bash
scripts/live-env.sh services/backend/.venv/bin/python scripts/live-smoke.py run --image-provider openai --approve-usd 8.00 --run-id smoke-openai-1
```

or with Gemini images:

```bash
scripts/live-env.sh services/backend/.venv/bin/python scripts/live-smoke.py run --image-provider gemini --approve-usd 4.00 --run-id smoke-gemini-1
```

**Scope:** one comic from the fixed prompt (a lighthouse keeper who always carries a brass lantern,
and a seagull, as a 3-panel strip):
1. Intake and moderation.
2. Story Bible, then reference sheets for two characters, with safety checks and a sheet review, and
   up to three location references.
3. **Persistent visual prop:** unless the Story Bible already has one, the run adds the continuity
   rule "The keeper carries a brass lantern" (no paid call; the sheets are not redrawn). Because the
   rule is newer than the sheet review, that review is confirmed explicitly; the confirmation is
   recorded in the evidence.
4. Script, which must have exactly 3 panels. Otherwise the run stops before any panel image is
   paid for.
5. **Script edit before approval:** the three panels get distinct camera shots (wide, medium,
   close-up) and the last panel's action changes. The edited version is re-checked against the
   Story Bible (one small text call). A blocking issue stops the run before panel images; review
   items are confirmed and recorded.
6. Three reference-conditioned panels, each with a safety check, a layout check and a Gemini
   identity check (the lantern is a required element wherever the keeper is drawn), and up to two
   repair rounds each. The run checks the drawn panels use the edited shots.
7. One instruction edit of panel 1.
8. PDF, PNG and CBZ exports. These make no paid call: local resampling, with effective-DPI and
   lettering-size reports. A 6-inch strip PDF reports lettering below 6.48 pt (expected; see
   `docs/blockers.md`).

The approved amount becomes a lifetime spending cap on the smoke user. If the next reservation would
cross it, the run stops.

Evidence and per-stage usage are written to `docs/evidence/smoke/<run-id>/result.json`. Estimated,
calculated, unresolved and provider-confirmed amounts are kept separate. Afterwards:

```bash
scripts/live-env.sh services/backend/.venv/bin/python scripts/live-smoke.py reconcile --run-id smoke-openai-1
```

That command lists every still-unresolved potential charge, for example an image call that timed
out. Import the figures from the providers' usage dashboards with `--charges charges.csv`. The
columns are `provider,request_id,usd,source`. For a call without a request id, use
`attempt:<attempt_id>` in the `request_id` column.
