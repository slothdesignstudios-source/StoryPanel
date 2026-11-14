# Release notes

## Unreleased (pre-launch build)

### Providers (2026-11-14)
- StoryPanel now runs on **Anthropic** (story, script, moderation, vision checks), **OpenAI** (default
  image provider) and **Google Gemini** (visual identity QA, and an alternative image provider chosen
  per comic). **fal and Voyage are no longer required**: their adapters are disabled by default.
- The image provider is chosen when a comic is created and kept for every image in that comic.
- Comics created earlier with fal remain readable and exportable.

### Credits, approvals and story fidelity (2026-11-14)
- **Credits page:** lists holds in progress and every hold, charge and release, recorded as
  append-only events. Holds made before this version show as "history unavailable"; nothing is
  reconstructed. The header shows "N credits · M held" and refreshes as work finishes in the
  background.
- **Charging policy, stated in full before each step:** the cast is charged when its sheets are
  drawn and the script when it is written; each delivered panel is charged, including panels
  flagged for review; failed and safety-blocked panels are not. Prices are unchanged.
- **Fixed:** an approved script that failed the content check kept its illustration hold, so the next
  approval held the same credits again.
- **Approvals are bound to the saved version** on the server: a save that lands while an approval
  is being processed sends the comic back for approval. Approve is disabled while Story Bible or
  rule edits are unsaved.
- **Continuity rules:** Must and Prefer rules go into the prompts for sheets, panels and edits, and
  are checked in the script and in each panel. Existing world rules can be classified into rules,
  keeping their wording.
- **Script check:** each script is checked against the Story Bible and its beat sheet before
  approval. Uncertain or unavailable checks need an explicit, recorded confirmation; content-safety
  refusals cannot be overridden.
- **Reference-sheet review:** flags sheets that miss a feature or rule, or have a cropped view. The
  user confirms before approving. Nothing is redrawn automatically.
- **Panel review reasons:** each flagged panel says why: problems found, couldn't confirm, or not
  checked. A finished comic with flagged panels reads "Drawn · N panels to review".
- **Lettering:** text sits further inside speech and thought balloons. Balloons and sound effects
  avoid characters where the check located them, and the overlap is flagged where they can't.
  Applies to new and edited balloons.
- **Planning text** (the idea) may use characters the lettering fonts can't print, with a warning.
  Printed text is still checked.

### Known limitations
- **Checks are model judgments.** The script check, the sheet review and the panel checks can be
  wrong in both directions. Prompts make continuity rules likelier, not certain. Their accuracy has
  not been measured on live models.
- **Lettering placement** is verified on fixtures and rendered test pages, not yet on real generated
  art.
- **Repairs and edits are instruction edits, not masked inpainting.** OpenAI and Gemini repaint the
  whole panel from an instruction. StoryPanel then keeps only the repaired region by compositing locally.
  Small changes outside the region can be lost by design, and fine detail inside it can drift.
- **Character consistency is judged, not measured.** Gemini compares each panel with the approved
  reference sheets and reports matches, mismatches and uncertainty per character. It is not a
  similarity score. An uncertain answer or a failed check flags the panel for manual review instead of
  passing it.
- **No AI upscaling.** Exports use the generated images with local resampling, which adds no detail.
  Each export reports its effective print DPI (at a 6-inch page width). It warns below 300 DPI and
  marks below 150 DPI as low.
- **Unverified prices.** OpenAI and Gemini prices come from search summaries of the official pages.
  Check them before relying on cost estimates.
- **Lettering character coverage.** Balloons, captions and sound effects can only use the characters the
  bundled lettering fonts contain:
  - **Supported:** Western European (Latin-1) letters such as é, ñ, ü, ß, ø and Æ; Œ, œ and ı; digits;
    and common punctuation and symbols (– — ‘ ’ “ ” „ … • € ™ £ ½ « »).
  - **Not supported yet:** other scripts and symbols, for example Ł, ő, Č, ğ, Greek, Cyrillic, Chinese,
    Japanese, Korean and emoji. This is **not** full Unicode support.
  - What happens:
    - A story idea containing unsupported characters is refused before anything is generated or charged.
    - An edited balloon containing them is not saved.
    - A generated script that contains them cannot be approved.

  Each case shows the same message, naming the characters and the supported set: *"These characters
  can't be lettered yet: Ł. Lettering supports Western European (Latin-1) letters … Replace or remove
  them (for example Ł → L), then try again."* Text is never changed automatically: the user decides
  what to write instead. The exact set is generated from the font files
  (`apps/web/src/lib/lettering-charset.ts`, `scripts/gen-lettering-charset.py`), and the browser and
  server check the same set.
- **Overfull panels.** When a panel has more lettering than fits, overlapping balloons are flagged in the
  editor ("Overlaps other lettering", "Tail crosses other lettering") rather than hidden.
