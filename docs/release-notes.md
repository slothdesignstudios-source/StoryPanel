# Release notes

## Unreleased (pre-launch build)

### Known limitations
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
