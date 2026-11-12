"""Layout validation for generated reference sheets.

Turnaround (4 x 1) and expression (2 x 2) sheets are cropped into per-view references. The
image model is *asked* for that grid but nothing guarantees it, so before cropping:

1. Background is estimated from the border; "ink" is anything clearly different from it.
2. Fixed grid: every cell must contain ink, and each crop line must run through background
   (a figure straddling a line would be cut in half).
3. Otherwise, the figures are located from the ink projection along each axis; if exactly the
   expected number of separated bands is found, crops follow them.
4. Otherwise the layout is unverified: the caller keeps the sheet for display but does not
   use its crops as identity references.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

from PIL import Image, ImageChops

WORK = 256  # analysis width in pixels
MIN_CELL_INK = 0.02  # a cell with less ink than this is empty
MAX_LINE_INK = 0.08  # share of a crop line's band that may be ink


@dataclass
class SheetLayout:
    method: str  # grid | segmented | unverified
    crops: list[bytes] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _clear_bands(mask: Image.Image) -> Image.Image:
    """Rows that are almost entirely ink are floor/backdrop bands, not figures."""
    px = mask.load()
    assert px is not None
    w, h = mask.size
    out = mask.copy()
    draw = out.load()
    assert draw is not None
    for y in range(h):
        if sum(px[x, y] == 255 for x in range(w)) > 0.85 * w:
            for x in range(w):
                draw[x, y] = 0
    return out


def _ink(img: Image.Image) -> Image.Image:
    small = img.convert("RGB")
    small.thumbnail((WORK, WORK))
    w, h = small.size
    border = [small.getpixel((x, 0)) for x in range(w)] + [small.getpixel((x, h - 1)) for x in range(w)] + \
             [small.getpixel((0, y)) for y in range(h)] + [small.getpixel((w - 1, y)) for y in range(h)]  # fmt: skip
    bg = tuple(sorted(c[i] for c in border)[len(border) // 2] for i in range(3))  # type: ignore[index]
    diff = ImageChops.difference(small, Image.new("RGB", small.size, bg)).convert("L")
    return diff.point(lambda v: 255 if v > 40 else 0)


def _share(mask: Image.Image, box: tuple[int, int, int, int]) -> float:
    region = mask.crop(box)
    hist = region.histogram()
    total = max(1, region.width * region.height)
    return hist[255] / total


def _bands(profile: list[float], n: int, min_gap: int) -> list[tuple[int, int]] | None:
    """n runs of ink separated by gaps of at least `min_gap` empty columns/rows."""
    runs: list[list[int]] = []
    for i, v in enumerate(profile):
        if v > 0.01:
            if runs and i - runs[-1][1] <= min_gap:
                runs[-1][1] = i
            else:
                runs.append([i, i])
    runs = [r for r in runs if r[1] - r[0] >= 2]
    return [(a, b + 1) for a, b in runs] if len(runs) == n else None


def _crop(img: Image.Image, box: tuple[float, float, float, float]) -> bytes:
    w, h = img.size
    out = io.BytesIO()
    img.crop((int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h))).save(out, format="PNG")
    return out.getvalue()


def analyse(data: bytes, cols: int, rows: int) -> SheetLayout:
    with Image.open(io.BytesIO(data)) as raw:
        img = raw.convert("RGB")
    mask = _clear_bands(_ink(img))
    mw, mh = mask.size
    problems: list[str] = []
    cw, ch = mw / cols, mh / rows
    for r in range(rows):
        for c in range(cols):
            if _share(mask, (int(c * cw), int(r * ch), int((c + 1) * cw), int((r + 1) * ch))) < MIN_CELL_INK:
                problems.append(f"empty_cell:{r},{c}")
    band = max(1, int(mw * 0.012))
    for c in range(1, cols):
        x = int(c * cw)
        if _share(mask, (x - band, 0, x + band, mh)) > MAX_LINE_INK:
            problems.append(f"figure_on_vertical_line:{c}")
    band = max(1, int(mh * 0.012))
    for r in range(1, rows):
        y = int(r * ch)
        if _share(mask, (0, y - band, mw, y + band)) > MAX_LINE_INK:
            problems.append(f"figure_on_horizontal_line:{r}")
    if not problems:
        boxes = [
            (c / cols, r / rows, (c + 1) / cols, (r + 1) / rows) for r in range(rows) for c in range(cols)
        ]
        return SheetLayout("grid", [_crop(img, b) for b in boxes])
    # Locate the figures instead: ink projection per axis.
    px = mask.load()
    assert px is not None
    col_profile = [sum(px[x, y] == 255 for y in range(mh)) / mh for x in range(mw)]
    row_profile = [sum(px[x, y] == 255 for x in range(mw)) / mw for y in range(mh)]
    xs = _bands(col_profile, cols, max(1, mw // 100))
    ys = _bands(row_profile, rows, max(1, mh // 100)) if rows > 1 else [(0, mh)]
    if (
        xs
        and ys
        and all(_share(mask, (x0, y0, x1, y1)) >= MIN_CELL_INK for (y0, y1) in ys for (x0, x1) in xs)
    ):
        pad = 0.02
        boxes = [(max(0.0, x0 / mw - pad), max(0.0, y0 / mh - pad), min(1.0, x1 / mw + pad), min(1.0, y1 / mh + pad))
                 for (y0, y1) in ys for (x0, x1) in xs]  # fmt: skip
        return SheetLayout("segmented", [_crop(img, b) for b in boxes], problems)
    return SheetLayout("unverified", [], problems + ["figures_not_separable"])
