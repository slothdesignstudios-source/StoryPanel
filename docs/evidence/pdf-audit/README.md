# PDF visual acceptance (2026-11-12)

Representative pages rendered three ways and compared side by side: PNG export | PDF (trim box,
rasterised with `pdftoppm` at the PNG's resolution) | pixels differing by more than 80/255.

- `synth-*`: synthetic page through the current renderers: caption at the page corner, speech tails
  pointing down-left, up and to the trim edge, a thought cloud with trail, rotated SFX, multi-line text.
- `strip-*`, `page-*`: real comics from the running stack (fake providers). These PDFs were produced by
  the server **before** the fixes below (they still show the outline/tail/SFX differences).

Material differences found and fixed in `pipeline/render.py` (regression test
`test_pdf_balloon_geometry_and_sfx_fill_match_the_png_renderer`, which fails on the previous renderer
with an 18 px tail overshoot at 1600 px width):
1. Outlines were centred on the shape path (PDF) instead of inside the box (PNG): every balloon and
   caption was half a stroke larger. Shapes are now inset by half a stroke.
2. Mitred joins extended sharp tail tips past the point they aim at (toward the speaker). Round joins,
   and the tip is pulled back by half a stroke so the outline's outer edge ends on the target.
3. SFX: render mode "fill+stroke" put the ink stroke over the yellow letters; then, after splitting into
   stroke and fill passes, the fill pass inherited stroke-only mode (PDF text render mode persists and
   reportlab omits `0 Tr`), turning the letters black. Each pass now has its own graphics state;
   stroke at twice the width underneath, fill on top, as the PNG renderer does.

Remaining, harmless differences (residual differing pixels 0.8 % synthetic page, after fixes):
anti-aliasing of glyph edges and curves (pdftoppm vs Pillow at 2x supersampling) and sub-pixel font
hinting. **Correction (2026-11-12, second round):** this note previously also listed lettering that
crosses the trim edge into the bleed as harmless. It is not: such lettering is cut off in print. All
lettering is now kept inside a 0.125 in safe area; see [`../pdf-safe-area/`](../pdf-safe-area/README.md).
Text, line breaks, balloon geometry, tails, reading order (draw order
by `order_index` in both), fonts (embedded ComicNeue/Bangers subsets) and selectable text match.
Editor (Konva) vs PNG export is covered separately by `e2e/lettering.spec.ts` (per-balloon ink overlap
>= 0.997).
