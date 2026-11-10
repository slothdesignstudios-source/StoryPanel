# Engineering decisions and recorded conflicts

## Conflicts in the product document, and how they were resolved

| # | Conflict | Resolution |
|---|---|---|
| C1 | §3.1 step 7 and §7.12/§7.15 list webtoon export and public share links; the §3.2 release table puts webtoon and sharing in v2. | The release table wins. MVP exports are PDF, PNG, CBZ. `POST /exports` rejects `webtoon`. |
| C2 | §9.1 says the MVP is "a 1–4 page comic"; §3.4 caps the MVP at 8 pages; §7.2 defaults to 4. | Default 4, configurable maximum 8 (`SP_DEFAULT_PAGES`, `SP_MAX_PAGES`; validation refuses more than 8). |
| C3 | §7.9 says "three automated checks" and "up to two automatic retries"; the brief also asks for bounded *repair*. | One attempt plus at most two automatic regenerations (3 images). Fix notes from the checker are compiled into the retry prompt. Targeted inpainting (§7.9 last sentence) is not built: user "fix by instruction" covers local fixes. |
| C4 | §7.9 says failing panels are shown as "needs attention"; §12.1 says unsafe content is blocked at every stage. | Two separate outcomes. Quality failure → visible with a badge. Safety failure → quarantined, never signed, never listed, never restorable, regardless of retries. |
| C5 | §7.8 says idempotency keys mean "retries never double-charge". | Not claimed. We persist the provider request id before waiting and reconcile by id after an ambiguous timeout; a timeout *during submission* is recorded as `ambiguous` for manual reconciliation. |
| C6 | §6.1 names models and §10 gives prices as of "late 2026" with a "verify before building" warning. | No model id or price is hard-coded. Live mode refuses to start until every model id is set explicitly; prices default to 0 and are documented as unverified. |
| C7 | §4.3 Free plan: "2 style presets"; §3.2 MVP: "4 presets". | All four exist; the free plan can use two (`free_plan_presets`), paid plans all four. |
| C8 | §7.11 step 1 wants a face/object detector; §6.1 allows boxes from the vision LLM. | Head and body boxes come from the vision QA call (already paid for). No separate detector. |
| C9 | §6.1 "SVG rendered with resvg or Skia". | Lettering is stored as vector data and rasterised deterministically with Pillow at 2× supersampling. An SVG serialisation of the layer exists (`balloon_layer_svg`) but is not the render path. Chosen to keep one renderer with one set of font metrics. |
| C10 | §9.4 suggests `services/api` and `services/workers` as separate services with nine worker packages. | One Python distribution (`services/backend`) with two entrypoints from one image. The layering the brief asks for (handlers / services / repositories / workflows / providers / rendering) is kept as packages. |

## Decisions

**Auth: Clerk.** Named by the document; managed sessions, MFA and account recovery are not something to hand-build.
The API verifies Clerk session JWTs against the instance JWKS (issuer, expiry, `azp`). The web app uses
`@clerk/clerk-react`, so there is no server-side session and no cookie the API trusts. A dev-only email sign-in
exists for offline work; `Settings` refuses it in staging and production and the smoke test checks it is unreachable.

**CSRF.** The API accepts bearer tokens only and sets no cookies, so a cross-site request cannot be authenticated.
Defence in depth: state-changing requests with a foreign `Origin` are rejected, and CORS is an explicit allowlist without credentials.

**Payments: Stripe** (Checkout + customer portal + signed webhooks). Hosted pages keep card data out of our systems.

**Providers.** Anthropic for writing, moderation and vision QA; fal for image generation, editing and upscaling;
Voyage for image embeddings. Each sits behind a protocol in `providers/base.py`. Deterministic fakes implement the
same protocols and are refused outside local/test. See docs/providers.md for exactly what was verified.

**Orchestration.** Workflows hold no I/O and take their limits as input, so replays do not depend on live config.
A transactional outbox bridges database commits and Temporal calls. Workflow ids are deterministic; signals carry ids the workflow dedupes.

**Deployment target: Render** (web services + background worker + Postgres with pgvector + Key Value), with
Temporal Cloud and S3. It is the lowest-ops option the document lists that runs a long-lived, non-HTTP worker.
Object storage is Terraform-managed because Render has none. Path to scale: the same two images run on ECS.

**UI kit.** Components follow the shadcn/ui pattern (Radix + cva + tailwind-merge, owned in the repo). They were
written by hand rather than pulled with the shadcn CLI, whose registry was unreachable from the build environment.

**Jitter.** Temporal retry policies have no jitter setting, so activities compute exponential backoff with
jitter themselves and pass it as `next_retry_delay`.

**Reader and editor rendering.** Both use the same Konva page component and the same greedy text-fitting algorithm
as the server. Browser and Pillow font metrics differ slightly, so a line can wrap differently on screen than in an export.
Exports are authoritative. Closing this gap (server-rendered previews after each edit) is listed as a known limitation.
