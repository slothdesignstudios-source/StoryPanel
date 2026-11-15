# Launch checklist (separate from the smoke test)

The one-comic live smoke test ([`live-test-proposal.md`](live-test-proposal.md)) checks that the live
integrations work end to end. **It certifies nothing for production.** Everything below remains to
do before launch, whatever the smoke test shows. Account and configuration details are in
[`setup-checklist.md`](setup-checklist.md); blocker ids are in [`blockers.md`](blockers.md).

## Quality and cost (after the smoke test)
- [ ] Measured token counts per OpenAI image replace the reservation assumptions in the price table
- [ ] A sized evaluation of output quality and check accuracy (the 63-prompt suite), with its own
      approved budget derived from the smoke test's measured cost
- [ ] Readability of lettering on real generated art reviewed by a person
- [ ] Live latency per stage measured (`scripts/stage-report.py`); decisions on QA concurrency
- [ ] The unexplained 12-credit discrepancy explained from the real `reconcile-comic.sql` output

## Follow-ups recorded, not part of the smoke test
- [ ] **Print layout for strips:** the 6-inch PDF width stays the default; exports warn per page
      with the width that page needs (strips: 6.6–9.6 in). Options to decide later: a wider or
      landscape strip PDF trim, a larger lettering floor for strips, or print-specific layouts
- [ ] Reuse an image that finished before a lost workflow instead of drawing that panel again
- [ ] A browser test of the approval block while an edit is saving (the server-side refusal is tested)

## Accounts and infrastructure (B3–B9)
- [ ] Stripe test mode, then live (B3)
- [ ] Clerk instance (B4)
- [ ] Terraform providers reachable, `terraform plan` (B6)
- [ ] AWS, Temporal Cloud, Render; staging deploy per [`staging-plan.md`](staging-plan.md) (B7)
- [ ] Alert receivers (PagerDuty, Slack) (B9)

## Reviews (B8)
- [ ] Legal: terms, IP and likeness, data protection, C2PA, retention periods
- [ ] Finance and product sign-off of [`billing-policy.md`](billing-policy.md)
- [ ] Manual accessibility audit
- [ ] Security review
- [ ] Provider commercial-use and data terms for Anthropic, OpenAI and Google
