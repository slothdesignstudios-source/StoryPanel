# Phase 2 completion audit (2026-11-15)

Every approved Phase 2 item, mapped to where it is implemented, what tests it, and what is still
not proven. An item counts as done only when a test exercises its behaviour; a screen that shows it
is not enough.

**Evidence levels:**
- **code:** confirmed by reading the code, with no test of the behaviour.
- **sim:** a test using simulated providers (fake model answers) checks the behaviour.
- **pixel / browser:** measured on rendered images or in a real browser (Playwright).
- **live:** checked against real providers. **No item is live-verified.**

All judgments by models (consistency, sheet review, panel QA) are simulated. The tests prove the
plumbing and the decision made from each verdict, not how accurate any real model's judgment is.

## Points the owner asked to confirm

| Point | Implementation | Tests | Level | Remaining limitation |
|---|---|---|---|---|
| Approval is bound to versions on the server | `stages_plan.stage_check_approval` re-checks at each stage transition that the approved version is still the current one; a save after approval moves the version on, so an approval of the old one is refused there. `projects.approve_cast` and `approve_script` refuse a stale version (409). Each confirmation is keyed to the item's content: version, check time and message (`projects._bound_key`). Stale or partial confirmations are refused (422). | `test_approval_binding` (5: stale tab, save racing an approval, save after the check, hold released) · `test_story_fidelity::test_acknowledgements_are_labelled_and_bound_to_the_version_and_check` · `::test_sheet_confirmation_is_bound_to_the_cast_version` · `e2e/review-dialog.spec.ts` | sim, browser | None known |
| Hold/release history is immutable; legacy holds reported honestly | Migration 0012 adds the `credit_reservation_events` table, with a `forbid_mutation` trigger on UPDATE and DELETE. `/v1/credits` (`credits.account_view`) builds activity from events. Holds without events report `history: "unavailable"` and are not rebuilt. | `test_credit_paths::test_hold_history_cannot_be_rewritten` (new: UPDATE and DELETE refused) · `::test_credit_activity_comes_from_immutable_events_without_double_counting` · `::test_holds_made_before_hold_history_are_marked_unavailable` | sim (real Postgres trigger) | The detail of holds made before 0012 cannot be recovered. The 12-credit drop stays unresolved until the `reconcile-comic.sql` output is available. TRUNCATE is not blocked (same as the ledger); it is an operator action. |
| World rules become visual requirements with their wording unchanged | `projects.put_continuity_rules` refuses a rule from `world_rule` whose text differs from `world_rules[i]` (422, "original wording"). `world_rules` itself is kept. Prompt v2 inserts the rule text verbatim. | `test_story_fidelity::test_world_rules_are_classified_verbatim_without_invented_detail` (altered text refused; panel prompts contain `Must be clearly visible: <exact text>.`) | sim | Classification (character, location or every panel) is the user's choice; nothing classifies automatically |
| Beat checks look at content, not just tags | Tags are checked in code (`validation.beat_coverage`). Then `consistency.check_script` sends each panel's action, lettering, characters and claimed beats, plus each beat's summary, to the light model. "No" or "uncertain" becomes a review item. | `::test_beat_coverage_is_declared_coverage_checked_in_code` · `::test_beat_check_judges_panel_content_not_only_the_tag` (new: a correctly tagged panel is still flagged; the request carries action, lettering and the beat summary) · `::test_consistency_verdicts_become_blocking_review_or_nothing` | sim | How accurate a real model's judgment is has not been measured |
| QA that failed to run is kept apart from real visual defects | Panels: `qa.qa_reasons` sorts results into categories (`not_checked` vs `visual_mismatch`/`missing`/…). The API reports `qa_state` (e.g. `not_checked`) separately from status. Sheets: `sheet_check.status` is `unavailable` (not reviewed) vs `flagged`. | `test_qa_reasons` (3, including "never call an error a defect" and "API separates drawn from checked") · `test_story_fidelity::test_unavailable_sheet_review_is_shown_as_not_checked` · `::test_flagged_reference_sheet_needs_a_recorded_acknowledgement` | sim | None known |
| A continuity-rule change marks older QA as stale | Sheet reviews and panel checks store `rules_sha` (`consistency.rules_fingerprint`). The API reports `sheet_rules_current` / `qa_rules_current`: false means stale, null means unknown for legacy results. A stale sheet becomes a confirmation item at cast approval. The editor marks stale panels. | `test_stale_checks` (4, new) · `test_story_fidelity::test_world_rules_are_classified_verbatim_without_invented_detail` (stale review confirmed explicitly) | sim | Nothing is re-checked automatically (a re-check would be paid). Script consistency results are not marked stale: rules cannot change while the script is under review (409). After drawing, the script check is history and is not shown. |

## Full map

### A. Credits

| Item | Implementation | Tests | Level | Limitation |
|---|---|---|---|---|
| Stale or rejected panel-hold leak | Hold closed when approval is rejected or stale (`stages_plan`, `credits.close`) | `test_credit_paths::test_rejected_approval_releases_its_panel_hold` · `test_approval_binding::test_script_saved_after_the_approval_check_is_not_drawn_and_its_hold_is_released` | sim | none |
| Credit-path regressions | `services/credits.py` | `test_credit_paths` (10) · `test_billing_properties` | sim | Live provider costs not involved |
| Append-only hold events, history view | 0012, `credits.account_view` | see above | sim | Legacy detail unavailable |
| Header and Credits page refresh from live events | Project events over SSE invalidate the `me` and `credits` queries (`apps/web/src/lib/events.ts`) | `e2e/journey.spec.ts` (hold amounts shown) | browser | No dedicated multi-tab refresh test |
| Charging policy stated at each step | Gate dialogs, `QaNotice`, Credits page copy | `e2e/journey.spec.ts` (hold text) and screenshots | browser, screenshot | Wording only; prices unchanged |
| `scripts/sql/reconcile-comic.sql` | read-only SQL | not run against the owner's database | code | Needs the owner's output |

### B. Approvals and editing

| Item | Implementation | Tests | Level | Limitation |
|---|---|---|---|---|
| Server-side version binding | see above | see above | sim, browser | none |
| Labelled confirmations, start unchecked, reset on change (this round) | `ReviewItem` page/panel/rule/beat/character/issue, `stages.tsx useAcks` | `e2e/review-dialog.spec.ts` · `test_story_fidelity` (2) | sim, browser | none |
| UI blocks approval while edits are unsaved or saving | `stages.tsx` sticky bar state | code. The e2e covers acknowledgements, not the saving state | code | No browser test of the saving-state block. The server still refuses a stale version (tested). |
| Themes and World rules editable; limits on length and blank values | `api.PatchBible`, `projects.patch_bible` | `test_unit::test_story_bible_patch_refuses_blank_values_and_items` · `test_flow::test_story_bible_edit_invalidates_cast_approval_target` | sim | none |
| Planning input may include unprintable characters (with a warning); printed text enforced | `moderation`/`lettering.unsupported_chars` | `test_print_safe_area::test_planning_input_may_use_characters_the_fonts_cannot_letter` · `::test_characters_the_fonts_cannot_draw_are_refused_before_anything_is_paid_for` | sim | none |
| Fields and messages clear of sticky bars (this round) | `useStickyClearance`, scroll padding | `e2e/sticky.spec.ts` (desktop and Pixel 7) | browser | none |

### C. Story fidelity

| Item | Implementation | Tests | Level | Limitation |
|---|---|---|---|---|
| Structured continuity rules, backwards compatible | `ContinuityRule`, 0013, `put_continuity_rules` | `test_story_fidelity` (rules tests) | sim | none |
| Verbatim classification of world rules | see above | see above | sim | none |
| Prompt v2 propagation | `art.rules_for_panel`/`rules_for_character`, `packages/prompts/v2` | `::test_must_rules_reach_every_panel_they_apply_to_and_only_those` · `::test_shot_and_climax_reach_the_prompt` · `::test_sheet_and_edit_prompts_carry_the_character_rules` | sim | Whether real image models follow the rules is unknown |
| Beat tags plus semantic check | see above | see above | sim | Judgment quality unmeasured |
| Script consistency check with recorded overrides; a violated rule blocks; moderation stays blocking | `consistency.py`, `stages_plan.stage_check_script` | `::test_violated_rule_blocks_until_edited_or_excepted` · `::test_uncertain_and_unavailable_checks_need_a_recorded_acknowledgement` · `::test_moderation_refusal_cannot_be_acknowledged` · `::test_nothing_to_check_makes_no_paid_call` | sim | Judgment quality unmeasured |
| Sheet review flags, no paid redraws | `consistency.check_sheet`, `stages_plan.stage_check_sheet` | `::test_flagged_reference_sheet_needs_a_recorded_acknowledgement` · `::test_unavailable_sheet_review_is_shown_as_not_checked` | sim | Judgment quality unmeasured |
| Required elements in panel QA | `qa.evaluate_panel(required=…)` | `::test_missing_required_element_is_repaired_uncertain_goes_to_review` | sim | Judgment quality unmeasured |
| Patch markers for in-flight workflows | `workflow.patched("sheet-check-2026-11")`, `("script-consistency-2026-11")` | Replay against pre-upgrade histories: see `docs/upgrade-verification.md` | sim (replay) | see that document |
| Stale QA after rule changes (this round) | see above | `test_stale_checks` | sim | No automatic re-check |

### D. QA wording

| Item | Implementation | Tests | Level | Limitation |
|---|---|---|---|---|
| Reason categories; "not checked" kept apart from defects | `qa.qa_reasons` | `test_qa_reasons` (3) | sim | none |
| "Drawn · N panels to review" | `projects.summary`, project header | `test_qa_reasons::test_api_separates_drawn_from_checked`, screenshots | sim, screenshot | none |

### E. Layout and lettering

| Item | Implementation | Tests | Level | Limitation |
|---|---|---|---|---|
| Top-aligned preview, unclipped selects, multi-line lettering inputs | `stages.tsx`, `editor.tsx` | `e2e/controls.spec.ts` | browser | none |
| Whole sheets on cast cards | `stages.tsx` | screenshots | screenshot | none |
| Text inside drawn balloons (insets 0.66/0.58) | `lettering.ELLIPSE_INSET`/`THOUGHT_INSET` | `test_balloon_text_bounds` (54 rendered-pixel cases) | pixel | Starting values; fixture art only |
| Body and SFX avoidance | `lettering.layout_panel` (body boxes from the layout check) | `test_lettering_placement` (4) | sim (fixtures) | Body boxes from real images not exercised |
| Minimum readable lettering; overflow instead of shrinking (this round) | `lettering.READABLE_MIN`, `stages_art.lettering_sizes` | `test_lettering_legibility` (8 tests, 14 cases) · `text.test.ts` · `e2e/legibility.spec.ts` · `e2e/lettering.spec.ts` (editor vs PNG) | sim, pixel, browser | Fixtures use flat-colour art: no readability review on real art. Strip PDFs at 6 in are below the floor (owner decision). |
| Wide strip slot stays last and is explained | `validation`, UI copy | `test_story_fidelity::test_strip_large_panel_that_is_not_last_is_explained_not_reordered` | sim | none |

### F. Performance

| Item | Implementation | Tests | Level | Limitation |
|---|---|---|---|---|
| Measurement report | `scripts/stage-report.py`, `docs/performance.md` | simulated run (`docs/evidence/perf`) | sim | No live timing |
| QA concurrency | Deferred by decision | none | n/a | Needs the identity-input contract, outcomes and cost evidence first |
