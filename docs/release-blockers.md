# Release blockers (current: 2026-11-18)

**The redesigned website is not evidence that StoryPanel is ready for production.** These must be
resolved, or explicitly accepted, before any public launch.
[`blockers.md`](blockers.md) and [`launch-checklist.md`](launch-checklist.md) hold the older,
infrastructure-level detail.

## Content and legal

1. **Sample artwork isn't approved for publication.**
   - **Status:** "The Last Train Home" and "The Tulips Remember" are `approved: false`
     (`apps/web/src/content/samples.ts`). A production build shows placeholders.
   - **Needs:** the comic owner's written OK, then set `approved: true`.
   - **Known gap:** no art style is recorded for either comic, so none is shown.
   - **Excluded:** the comic made from a personal photo must not be used.
2. **No Terms of Service, Privacy Policy, cookie or data notice, or contact and support channel.**
   - **Status:** the footer links to none, because none exist.
   - **Needs:** these, especially for paid plans, accounts and stored uploads. The product rule "no
     real people or existing franchises" must be in the Terms.
3. **Currency prices aren't published.**
   - **Status:** credit costs per step are live from `/v1/meta`. Plan allowances (12/80/240 credits a
     month) appear only inside the account, and the money price only at Stripe checkout.
   - **Needs:** a decision on publishing plan prices. Consumer rules usually require prices before
     purchase.
4. **Stripe isn't configured** in any environment tested here, so purchases are switched off.

## Product quality (not proven)

5. **Comic quality isn't production-proven.**
   - **Face detection** in stage 1A assigned 10 of 10 measured faces correctly.
   - **Automatic lettering** placed only 4 of 7 panels cleanly.
   - **The letterer fix** is verified only in a replay on stored head boxes (no body boxes) for two
     comics. See `docs/evidence/stage-1a-2026-11-18/replay/README.md`.
   - **Train panel 3** remains unresolved (flagged).
6. **Character consistency isn't guaranteed,** and the site says so. v4 prompt improvements remain
   **unverified with real generation**: the stage 1B comparison hasn't been approved or run.
7. **The running install is still cfa4534.** It places balloons from guessed face positions, which
   covered faces in the umbrella comic. Neither automatic face location nor the letterer fix is
   deployed.

## Accounts, billing and operations

8. **Production sign-in (Clerk) hasn't been exercised end to end;** its browser test is skipped
   without live keys.
9. **Not deployed anywhere.**
   - **Needs:** a domain, HTTPS, `NEXT_PUBLIC_SITE_URL`, a production database and object storage,
     backups and monitoring (`docs/staging-plan.md`).
   - **Untested:** the sitemap and canonical URLs, which currently point at `http://localhost:3000`.
10. **Unresolved from earlier:**
    - the 12-credit discrepancy (needs the `reconcile-comic.sql` output);
    - the strip print-size follow-up.
11. **Accessibility:**
    - **Done:** automated axe checks only.
    - **Still needed:** a manual screen-reader and keyboard audit, and testing on real phones.

## Not blockers, but decisions

- **Analytics:** none added. If wanted, propose a privacy-respecting option and say so in the
  privacy notice.
- **SEO:** the technical foundations are in place (titles, descriptions, Open Graph, sitemap, robots,
  noindex on private pages). No ranking outcome is promised.
