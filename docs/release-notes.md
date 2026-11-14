# Release notes

## Unreleased (pre-launch build)

### Providers (2026-11-14)
- StoryPanel now runs on **Anthropic** (story, script, moderation, vision checks), **OpenAI** (default
  image provider) and **Google Gemini** (visual identity QA, and an alternative image provider chosen
  per comic). **fal and Voyage are no longer required**: their adapters are disabled by default.
- The image provider is chosen when a comic is created and kept for every image in that comic.
- Comics created earlier with fal remain readable and exportable.

### Known limitations
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
