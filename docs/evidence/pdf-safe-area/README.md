# Print safe area and lettering collisions (2026-11-12)

Each image: PNG export (left) | PDF rasterised over the whole media box, bleed included (right, greyscale
because the check compares luminance). Red = trim, green = safe area (0.125 in inside the trim). Any
lettering ink outside the safe area would be painted magenta. Pages are deliberately hard: 25-word
balloons (the product maximum), captions, long SFX, speakers pushed into panel corners and edges so tails
aim at the page edge, and non-ASCII Latin-1 text (`¿Qué? ¡Sí! Café, naïve façade — «Ærø» … “déjà vu” ½ £5`).

| Page | Lettering pixels outside the safe area: before | after |
|---|---:|---:|
| page-long-dialogue (w4_grid) | 0 | 0 |
| page-nonascii-edges (w5_top_large) | 0 | 0 |
| strip-sfx-tails (s3_row) | 0 | 0 |
| strip-wide-last (s4_wide_last) | **95** (a border-breaking balloon 2 units from the trim) | 0 |

(`summary.json`; *before* = the lettering module at d2614c9 laid out the same pages.)

Defects found and fixed:
1. **Lettering into the trim/bleed.** Automatic placement allowed balloons within 2 page units of the trim,
   and the **editor allowed any balloon, caption, SFX or tail to be dragged to the page edge** (clamp to 0).
   Both now clamp every writer's geometry so all ink (shape, text, rotated SFX + outline, tail tip) stays in
   the safe area; oversized boxes are shrunk. Tests: `test_print_safe_area.py` (every template, editor moves,
   and the rendered PDF: zero lettering ink outside the safe area).
2. **Lettering covering lettering.** Captions went to a fixed corner and SFX to the action point regardless
   of balloons already there, hiding text (visible in the first renders of these pages). Captions now take a
   free corner, SFX search the panel (shrinking to 55 % before giving up), and any overlap that remains in an
   over-full panel is flagged `overlaps_lettering` (a tail across lettering: `tail_crosses_lettering`) and
   shown as a badge in the editor. Across every template at maximum load, 53 of 518 balloon pairs still
   overlap, all flagged; an ordinary strip has none.
3. **Characters the fonts cannot draw.** The bundled faces cover Latin-1 and typographic punctuation
   (verified from their cmaps); `Ł ő Č`, Greek, Cyrillic, CJK and emoji would print as empty boxes. Such text
   is now a script-validation *error* (the script cannot be approved, so no panel image is paid for), the
   editor refuses it with the characters named, and `typeset` flags it.

Remaining differences between PNG and PDF are anti-aliasing and font hinting only.
