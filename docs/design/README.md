# Website redesign: preview, screenshots and verification

**Contents:**
- design direction, tokens and components: [`brief.md`](brief.md);
- screenshots: [`screenshots/`](screenshots/);
- release blockers: [`../release-blockers.md`](../release-blockers.md).

**This redesign is presentation only.** These are unchanged: API contracts, authentication,
credits, prices, provider settings and the creation workflow. **It is not a sign of production
readiness.**

## Local preview on your computer (WSL), without touching your running app

Your running install (`~/storypanel`, cfa4534) keeps serving `http://localhost:3000`. This preview
runs the new web app from the separate checkout `~/storypanel-1a` on port **3100**.

**What works in this preview:**
- **Public pages** (`/`, `/gallery`, `/gallery/…`, `/pricing`) work completely, with live prices read
  from your running API.
- **Signed-in screens on port 3100 don't work.** Your API accepts browser requests only from
  `localhost:3000` (CORS). See "Seeing the redesigned product screens" below.

**1. Update the separate checkout to the redesign commit** (use the full SHA from the handoff
message):

```bash
git -C ~/storypanel fetch origin main && git -C ~/storypanel-1a checkout --detach <FULL-SHA>
```

**2. Install the web app's dependencies in that checkout only:**

```bash
cd ~/storypanel-1a/apps/web && pnpm install --frozen-lockfile
```

**3. Build with the sample comics shown.** Local preview only: they are labelled "not approved for
publication", and the pages are `noindex`.

```bash
cd ~/storypanel-1a/apps/web && scripts/preview-samples.sh && NEXT_PUBLIC_PREVIEW_UNAPPROVED_SAMPLES=1 NEXT_PUBLIC_API_URL=http://localhost:8000 NEXT_TELEMETRY_DISABLED=1 pnpm build
```

**4. Run it on port 3100.** It stays in the foreground; stop it with Ctrl+C.

```bash
cd ~/storypanel-1a/apps/web && cp -r .next/static .next/standalone/.next/ && cp -r public .next/standalone/ && PORT=3100 HOSTNAME=127.0.0.1 node .next/standalone/server.js
```

**5. Open it:** <http://localhost:3100> in your Windows browser.

**To see the production behaviour** (placeholders instead of unapproved art), rebuild without the
flag:

```bash
cd ~/storypanel-1a/apps/web && scripts/preview-samples.sh --remove && rm -rf .next && NEXT_PUBLIC_API_URL=http://localhost:8000 pnpm build
```

### Seeing the redesigned product screens

These are the dashboard, the creation flow, the editor and credits. They need the redesigned web app
on port 3000, which means upgrading your running install from cfa4534 to this commit. That's a
separate decision, because the upgrade also brings:
- the v4 prompts;
- the letterer changes;
- the face-check tools.

The database schema is unchanged (Alembic head 0014). I won't do it unasked. When you want it, I'll
write the exact stop, pull and restart procedure, with a rollback.

Until then, the screenshots below show every product screen. They come from a simulated stack:
fake providers, so the "art" is placeholder shapes.

## Screenshots (`screenshots/`)

**Captures:** desktop is 1440 px wide; mobile is iPhone 13, scaled to 780 px wide.

| Area | Files |
|---|---|
| Public site | `desktop-landing`, `desktop-gallery`, `desktop-sample-reader`, `desktop-pricing`, `desktop-signin`, `mobile-landing`, `mobile-hero-viewport`, `mobile-pricing`, `mobile-sample-reader` |
| Creation flow | `desktop-dashboard`, `desktop-new`, `desktop-cast`, `desktop-script`, `desktop-script-details` (panel details opened), `mobile-dashboard`, `mobile-new`, `mobile-cast`, `mobile-script` |
| Editing and reading | `desktop-editor`, `desktop-editor-balloon` (balloon selected), `desktop-reader`, `mobile-editor-balloon` |
| Account | `desktop-billing`, `mobile-billing`, `desktop-settings` |

**Things to know when reading them:**
- **Sample pages:** the screenshots show the two real sample pages because they were taken in
  local-preview mode. They are not approved for publication.
- **Phone capture artefact:** in full-page phone captures, Chromium sometimes doesn't paint the
  hero images. `mobile-hero-viewport` is a normal viewport capture showing they render.
- **Sticky bars:** the header and the script approval bar appear mid-page in full-page captures.
  In use they stay at the top and bottom of the screen.

## Verification actually run (2026-11-18)

| Check | Result |
|---|---|
| `pnpm typecheck` (tsc) | clean |
| `pnpm lint` (eslint) | 0 errors, 1 warning (pre-existing, `settings/page.tsx` `window.location.assign`) |
| `pnpm test` (vitest; includes the new `tokens.test.ts` contrast checks) | 26 passed |
| Playwright, full suite, desktop + mobile, against a local simulated stack | 23 passed, 1 skipped (`e2e/live/clerk.spec.ts`, needs live Clerk keys) |
| New `e2e/public.spec.ts` | landing text, live prices, no invented claims; axe WCAG 2.2 AA on public pages; keyboard order; robots/sitemap/noindex; sample art labelling; sign-in return path; no open redirect |
| Production-mode build (no preview flag, no sample files) | placeholders shown; `/samples/*` 404; sitemap lists only `/`, `/gallery`, `/pricing`; private pages send `X-Robots-Tag: noindex` |
| Phone width | no horizontal scrolling on any screen (measured) |
| Backend suite after the letterer fix | 616 passed |

**Not done:** a manual screen-reader audit, or testing on real phones. Automated axe rules catch only
part of what affects people.
