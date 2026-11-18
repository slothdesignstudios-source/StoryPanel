# StoryPanel web design brief (2026-11 redesign)

## Purpose

**Two jobs:**
- **For a visitor:** say what StoryPanel does in one look: *turn your story idea into an editable
  comic*. Then show real pages, the creation flow, and honest prices.
- **For a creator:** get from an idea to an approved cast, an approved script, drawn pages and an
  export. The next action is always obvious, and quality problems stay visible without taking over.

**Not changed:** routes, API calls, authentication, credits, provider settings or workflows. This is a
presentation layer.

## Direction

**Editorial, refined, creative, trustworthy.**
- **Colour:** quiet ivory pages, deep ink text and a single restrained indigo for actions. The comic
  artwork carries the colour; the interface stays neutral around it.
- **Removed:** the heavy 2 px ink borders, offset "comic" shadows, bright red buttons and yellow
  badges.
- **Hierarchy:** hairline borders, soft elevation on raised surfaces, and spacing and type doing the
  work.
- **Not used:** gradients, glass effects, decorative dashboards and motion beyond small state
  transitions (all off under `prefers-reduced-motion`).

**Brand:** the Bangers wordmark stays, the recognisable part of StoryPanel. It is set in ink rather
than red and used only as the logo, never for headings or UI.

## Tokens (`apps/web/src/app/globals.css`, `@theme`)

| Token | Value | Use |
|---|---|---|
| `paper` | `#F7F6F2` | page background (warm ivory) |
| `card` | `#FFFFFF` | raised surfaces |
| `ink` | `#18202B` | primary text, icons |
| `muted` | `#596273` | secondary text |
| `line` | `#DDE1E7` | hairline borders, dividers |
| `accent` | `#4338CA` | primary actions, links, selection, focus |
| `accent-strong` | `#3730A3` | primary hover/pressed |
| `accent-soft` | `#EEF0FC` | selected rows, chips, current-step fill |
| `ok` / `warn` / `danger` | `#1E6B43` / `#8A4A00` / `#A3261C` | status text and borders, with 8% tints as backgrounds |

**Verified, not assumed:** `src/lib/tokens.test.ts` reads these values from `globals.css` and checks
contrast.
- **Normal text, 4.5:1:** ink, muted, accent and the status colours on paper, on card and on their own
  tints; white on accent.
- **Focus ring and borders, 3:1:** against paper and card.

**Status:** never colour alone. Every status has a word: "Finished", "3 panels to review",
"Needs your answers".

**Focus:** one treatment everywhere, a 2 px indigo outline with a 2 px offset (`:focus-visible`).
**Disabled:** 50% opacity plus `cursor: not-allowed`; a disabled control always has a reason shown
next to it.

## Type

- **Display:** *Bricolage Grotesque* (variable, OFL; already in the project), weight 600, tracking
  -0.02em.
- **Text:** *Inter* (variable, OFL), 15–16 px base, line height 1.55.
- **Lettering:** *Comic Neue* and *Bangers*, the exact files the server renderer uses.

No new font dependencies.

**Scale:** 13 / 15 / 16 / 18 / 22 / 28 / 36 / 48–60 px (hero). Body copy is capped near 65 characters
(`max-w-prose`).

## Layout

- **Page widths:** marketing `max-w-6xl`; product `max-w-7xl`; forms and reading `max-w-3xl`.
- **Margins:** 16 px gutters on phones, 24 px on desktop.
- **Spacing:** a 4 px base; sections 64–96 px apart on marketing pages, 24–32 px in the product.
- **Shape:** cards have 12 px radius, a `line` border and the `shadow-sm` token. Inputs have 8 px
  radius, a 1 px border, and an indigo border and ring on focus.

## Components (`src/components/ui.tsx` and `src/components/marketing.tsx`)

| Component | Variants and notes |
|---|---|
| Button | `primary` (indigo), `secondary` (white, hairline), `ghost`, `danger` (outlined red); sm/md/lg/icon |
| Card, Notice, Badge, EmptyState, Field, Input, Textarea, Select, Dialog, Spinner | restyled in place; same API |
| Disclosure | button + region, renders children only when open (fields hidden by CSS would fail the sticky-bar test); opens itself when it contains an error |
| FlowSteps | Idea → Cast → Script → Drawing → Edit & export; current step in words, separate from quality review |
| SiteHeader, SiteFooter, SampleArt | public pages |

## Public pages

| Page | Content |
|---|---|
| `/` | headline, real sample pages, one primary CTA, how it works, gallery preview, what you get, prices (live from `/v1/meta`), FAQ, footer |
| `/gallery` | sample comics at readable size |
| `/gallery/[slug]` | distraction-free reader |
| `/pricing` | credit costs and holds, from the product configuration; nothing invented |

**Sample artwork.** The only real pages available are two comics made during testing ("The Last Train
Home", "The Tulips Remember").
- **Status:** permission to show them publicly is not recorded, so both are `approved: false` in
  `src/content/samples.ts`.
- **Production build:** shows a neutral placeholder instead.
- **Local preview:** `NEXT_PUBLIC_PREVIEW_UNAPPROVED_SAMPLES=1` shows them, labelled "Local preview,
  not approved for publication", and marks the pages `noindex`.
- **Excluded:** the comic based on a personal photo is never used.

**Never on any page:** testimonials, customer logos, usage numbers, awards or promises of perfect
character consistency.

## Product screens

- **Dashboard:** comics that need you come first, each with its next action.
- **Creation flow:** one primary action per stage, and the credit hold stated beside it.
- **Script review:** the core fields stay visible, which are shot, what we see and the lettering.
  Location, size, purpose, focus, story elements, beats and rules sit under "More panel details",
  which opens itself on an error.
- **Page editor:** the artwork comes first. Tools for the selection sit beside it, and page layout,
  reading order, captions and continuity rules sit under disclosures.
- **Quality review:** shown as a separate "to review" count and a notice, never mixed into the
  workflow status.

## Discoverability

- **Per page:** a title, a description and Open Graph/Twitter metadata, with a text-only social image
  (no unapproved artwork).
- **Indexing:** `sitemap.xml` lists public pages only. `robots.txt` and an `X-Robots-Tag: noindex`
  header keep account and project pages out.
- **Images:** width, height, `srcset` and lazy loading below the fold.
- **Not added:** tracking.
