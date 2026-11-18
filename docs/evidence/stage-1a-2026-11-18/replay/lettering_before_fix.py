"""Letterer: places balloons, captions and sound effects as vector data.

Deterministic: the same panel rectangle, dialogue and boxes always give the same layout.
All coordinates are page units (the layout templates' coordinate system), so the editor,
the preview renderer and the exporters agree at any output resolution.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from PIL import ImageFont

from storypanel.catalog import font_path

Rect = tuple[float, float, float, float]  # x, y, w, h

FONT_MAX, FONT_MIN = 22.0, 15.0
# Smallest readable lettering, in page units. Text is never set smaller: when it cannot fit at
# this size the balloon is flagged "overflow" (shorten, move or enlarge it). What it means at the
# intended outputs (all exports scale the page to a fixed width):
#   6 x 9 in page (1000 units at 432 pt): 15 x 0.432 = 6.48 pt printed; 30 px in the 2000 px PNG.
#   strip (1600 units): 18.75 px in the 2000 px PNG; 4.05 pt if printed 6 in wide (below the
#   floor, reported as a print warning; see readable_print_pt).
# A chosen floor, not a reader study.
READABLE_MIN = FONT_MIN
PRINT_WIDTH_PT = 432.0  # = render.PDF_TRIM_WIDTH_PT (6 in), repeated here to avoid an import cycle
MIN_PRINT_PT = round(READABLE_MIN * PRINT_WIDTH_PT / 1000.0, 2)  # 6.48 pt: the floor on the page format
SFX_FONT = 54.0
LINE_HEIGHT = 1.16
TARGET_ASPECT = 1.3
# Share of a balloon's width/height usable by the text block. The largest rectangle inside an
# ellipse is 70.7% of it; 0.74 put line ends on or past the outline. Speech ellipses use 0.66
# (inside, with a margin for the outline). Thought clouds draw their clear interior as an
# ellipse at 0.88 of the box (bumps sit on its edge), so 0.88 x 0.707 = 0.62; 0.58 leaves a
# margin. Starting values, checked against rendered pixels in tests/test_balloon_text_bounds.py.
ELLIPSE_INSET = 0.66
THOUGHT_INSET = 0.58
BOX_PAD = 10.0
BORDER_BREAK = 0.12  # how far (of panel size) a balloon may cross the border
# Print safe area: all lettering stays 0.125 in inside the trim. Exports print the page 6 in wide
# (render.PDF_TRIM_WIDTH_PT = 432 pt), so the margin is 9/432 of the page width in page units.
SAFE_MARGIN_FRACTION = 9.0 / 432.0
SFX_STROKE = 3.0  # page units the SFX ink outline extends beyond the glyphs (render: 2 x 3 wide, centred)


def safe_rect(page_size: tuple[float, float]) -> Rect:
    m = page_size[0] * SAFE_MARGIN_FRACTION
    return (m, m, page_size[0] - 2 * m, page_size[1] - 2 * m)


def _sfx_half_extents(text: str, w: float, h: float, size: float, rotation: float) -> tuple[float, float]:
    """Half width/height of a rotated SFX's ink: the box or the drawn text, whichever is wider,
    rotated about its centre (as both renderers draw it), plus the outline."""
    half_w = max(w, measure(text.upper(), "bangers", size)) / 2 + SFX_STROKE
    half_h = h / 2 + SFX_STROKE
    a = math.radians(rotation)
    return (half_w * abs(math.cos(a)) + half_h * abs(math.sin(a)),
            half_w * abs(math.sin(a)) + half_h * abs(math.cos(a)))  # fmt: skip


def ink_bounds(kind: str, text: str, g: dict[str, Any]) -> Rect:
    """Everything a balloon draws (shape, text, SFX outline, tail tip), in page units."""
    x, y, w, h = float(g["x"]), float(g["y"]), float(g["w"]), float(g["h"])
    if kind == "sfx":
        ex, ey = _sfx_half_extents(
            text, w, h, float(g.get("font_size", SFX_FONT)), float(g.get("rotation", 0.0))
        )
        cx, cy = x + w / 2, y + h / 2
        return (cx - ex, cy - ey, 2 * ex, 2 * ey)
    x0, y0, x1, y1 = x, y, x + w, y + h
    if g.get("tail"):
        tx, ty = float(g["tail"]["x"]), float(g["tail"]["y"])
        x0, y0, x1, y1 = min(x0, tx), min(y0, ty), max(x1, tx), max(y1, ty)
    return (x0, y0, x1 - x0, y1 - y0)


def outside_safe_area(kind: str, text: str, g: dict[str, Any], page_size: tuple[float, float]) -> bool:
    sx, sy, sw, sh = safe_rect(page_size)
    bx, by, bw, bh = ink_bounds(kind, text, g)
    eps = 0.5
    return bx < sx - eps or by < sy - eps or bx + bw > sx + sw + eps or by + bh > sy + sh + eps


def clamp_to_safe_area(
    kind: str, text: str, g: dict[str, Any], page_size: tuple[float, float]
) -> dict[str, Any]:
    """Move (and if necessary shrink) a balloon so all of its ink is inside the safe area."""
    sx, sy, sw, sh = safe_rect(page_size)
    out = dict(g)
    out["w"], out["h"] = min(float(g["w"]), sw), min(float(g["h"]), sh)
    if kind == "sfx":
        ex, ey = _sfx_half_extents(text, out["w"], out["h"], float(g.get("font_size", SFX_FONT)),
                                   float(g.get("rotation", 0.0)))  # fmt: skip
        if 2 * ex > sw or 2 * ey > sh:  # too big even when centred: scale it down
            f = min(sw / (2 * ex), sh / (2 * ey))
            out["w"], out["h"] = out["w"] * f, out["h"] * f
            out["font_size"] = float(g.get("font_size", SFX_FONT)) * f
            ex, ey = ex * f, ey * f
        cx = min(max(float(g["x"]) + out["w"] / 2, sx + ex), sx + sw - ex)
        cy = min(max(float(g["y"]) + out["h"] / 2, sy + ey), sy + sh - ey)
        out["x"], out["y"] = cx - out["w"] / 2, cy - out["h"] / 2
        return out
    out["x"] = min(max(float(g["x"]), sx), sx + sw - out["w"])
    out["y"] = min(max(float(g["y"]), sy), sy + sh - out["h"])
    if g.get("tail"):
        out["tail"] = {"x": min(max(float(g["tail"]["x"]), sx), sx + sw),
                       "y": min(max(float(g["tail"]["y"]), sy), sy + sh)}  # fmt: skip
    return out


@lru_cache(maxsize=1)
def _supported() -> frozenset[int]:
    """Code points every bundled lettering face can draw (body regular/bold and the SFX face)."""
    from fontTools.ttLib import TTFont  # type: ignore[import-untyped]

    faces = [("comic_neue", False), ("comic_neue", True), ("bangers", False)]
    sets = [set(TTFont(str(font_path(n, b))).getBestCmap()) for n, b in faces]
    return frozenset(set.intersection(*sets))


SUPPORTED_SUMMARY = (
    "Lettering supports Western European (Latin-1) letters such as é, ñ, ü, ß, ø and Æ, plus Œ, œ, ı and "
    "common punctuation (– — ‘ ’ “ ” „ … • € ™). Other scripts and symbols, for example Ł, ő, Č, Greek, "
    "Cyrillic, Chinese, Japanese, Korean and emoji, are not supported yet."
)


def unsupported_message(missing: str) -> str:
    """One actionable message for every place that refuses text the fonts cannot draw."""
    shown = ", ".join(missing[:12]) + ("…" if len(missing) > 12 else "")
    return (
        f"These characters can't be lettered yet: {shown}. {SUPPORTED_SUMMARY} "
        "Replace or remove them (for example Ł → L), then try again. Your text is never changed for you."
    )


def unsupported_chars(text: str) -> str:
    """Characters the lettering fonts cannot draw (they would print as empty boxes)."""
    ok = _supported()
    return "".join(sorted({c for c in text if not c.isspace() and ord(c) not in ok}))


@lru_cache(maxsize=256)
def _font(name: str, size: float, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(font_path(name, bold)), size)


def measure(text: str, font: str, size: float, bold: bool = False) -> float:
    return float(_font(font, size, bold).getlength(text))


def wrap(text: str, font: str, size: float, max_width: float, bold: bool = False) -> list[str]:
    """Greedy word wrap; over-long single words are split so nothing can overflow sideways."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        while measure(word, font, size, bold) > max_width and len(word) > 1:
            cut = len(word) - 1
            while cut > 1 and measure(word[:cut] + "-", font, size, bold) > max_width:
                cut -= 1
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:cut] + "-")
            word = word[cut:]
        trial = f"{current} {word}".strip()
        if current and measure(trial, font, size, bold) > max_width:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines or [""]


def text_block(text: str, font: str, size: float, max_width: float,
               bold: bool = False) -> tuple[list[str], float, float]:  # fmt: skip
    lines = wrap(text, font, size, max_width, bold)
    width = max(measure(line, font, size, bold) for line in lines)
    return lines, width, len(lines) * size * LINE_HEIGHT


def fit_text(text: str, font: str, box_w: float, box_h: float, max_size: float,
             min_size: float = 8.0, bold: bool = False) -> tuple[float, list[str], bool]:  # fmt: skip
    """Largest font size at which the text fits the box. Returns (size, lines, fits)."""
    size = max_size
    while True:
        lines, w, h = text_block(text, font, size, box_w, bold)
        if w <= box_w + 0.5 and h <= box_h + 0.5:
            return size, lines, True
        if size <= min_size:
            return size, lines, False
        size = max(min_size, size - 1.0)


def _inset(kind: str) -> float:
    return THOUGHT_INSET if kind == "thought" else ELLIPSE_INSET


def inner_box(kind: str, w: float, h: float) -> tuple[float, float]:
    if kind in ("speech", "thought"):
        return w * _inset(kind), h * _inset(kind)
    return max(1.0, w - 2 * BOX_PAD), max(1.0, h - 2 * BOX_PAD)


def _balloon_size(kind: str, text: str, font: str, size: float, max_w: float) -> tuple[float, float]:
    """Choose the wrap width whose balloon is closest to ~1.3 : 1."""
    best: tuple[float, float, float] | None = None
    lo = max(60.0, max((measure(w, font, size) for w in text.split()), default=60.0))
    width = lo
    while width <= max(lo, max_w):
        _, tw, th = text_block(text, font, size, width)
        if kind in ("speech", "thought"):
            bw, bh = tw / _inset(kind) + 8, th / _inset(kind) + 8
        else:
            bw, bh = tw + 2 * BOX_PAD, th + 2 * BOX_PAD
        score = abs(bw / bh - TARGET_ASPECT)
        if best is None or score < best[0] - 1e-6:
            best = (score, bw, bh)
        width += 12.0
    assert best is not None
    return best[1], best[2]


@dataclass
class Placed:
    index: int
    type: str
    speaker: str | None
    text: str
    x: float
    y: float
    w: float
    h: float
    font_size: float
    tail: tuple[float, float] | None = None
    rotation: float = 0.0
    flags: list[str] = field(default_factory=list)

    @property
    def rect(self) -> Rect:
        return (self.x, self.y, self.w, self.h)

    def geometry(self, font: str = "comic_neue") -> dict[str, Any]:
        g = {"x": round(self.x, 1), "y": round(self.y, 1), "w": round(self.w, 1),
             "h": round(self.h, 1), "font_size": self.font_size, "rotation": self.rotation,
             "tail": {"x": round(self.tail[0], 1), "y": round(self.tail[1], 1)}
             if self.tail else None}  # fmt: skip
        return typeset(self.type, self.text, g, font)[0]


def _ink_rect(p: Placed) -> Rect:
    g = {"x": p.x, "y": p.y, "w": p.w, "h": p.h, "font_size": p.font_size, "rotation": p.rotation}
    return ink_bounds(p.type, p.text, g) if p.type == "sfx" else (p.x, p.y, p.w, p.h)


def _segment_hits(a: tuple[float, float], b: tuple[float, float], r: Rect, steps: int = 24) -> bool:
    return any(r[0] < a[0] + (b[0] - a[0]) * t / steps < r[0] + r[2] and r[1] < a[1] + (b[1] - a[1]) * t / steps < r[1] + r[3]
               for t in range(1, steps))  # fmt: skip


def _dist_to(pt: tuple[float, float], r: Rect) -> float:
    dx = max(r[0] - pt[0], 0.0, pt[0] - (r[0] + r[2]))
    dy = max(r[1] - pt[1], 0.0, pt[1] - (r[1] + r[3]))
    return math.hypot(dx, dy)


def overlap(a: Rect, b: Rect) -> float:
    w = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    h = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return max(0.0, w) * max(0.0, h)


def reading_key(rect: Rect, panel: Rect) -> float:
    """Left-to-right, top-to-bottom order as one number: top dominates, then left."""
    cx = (rect[0] + rect[2] / 2 - panel[0]) / panel[2]
    cy = (rect[1] + rect[3] / 2 - panel[1]) / panel[3]
    return cy + 0.35 * cx


def _abs(box: dict[str, float] | None, panel: Rect) -> Rect | None:
    if not box:
        return None
    px, py, pw, ph = panel
    x = min(max(box["x"], 0.0), 1.0)
    y = min(max(box["y"], 0.0), 1.0)
    return (px + x * pw, py + y * ph, min(box["w"], 1 - x) * pw, min(box["h"], 1 - y) * ph)


# ------------------------------------------------------------------ what the artwork shows
DETAIL_GRID = 32


def art_detail(image: bytes, panel_w: float, panel_h: float, n: int = DETAIL_GRID) -> list[list[float]]:
    """How busy the panel's actual artwork is, as an n x n grid of 0..1 (0 = plain sky or wall, 1 =
    the busiest part: faces, hands, props, texture). Computed locally from the stored image, cropped
    exactly as the renderer crops it into the panel, so it lines up with the page. Free and
    deterministic; a proxy for "important visual regions", not an object detector."""
    import io

    from PIL import Image, ImageFilter

    with Image.open(io.BytesIO(image)) as raw:
        img = raw.convert("L")
    w, h = n * 6, max(1, round(n * 6 * panel_h / panel_w))
    scale = max(w / img.width, h / img.height)
    img = img.resize((max(w, round(img.width * scale)), max(h, round(img.height * scale))))
    left, top = (img.width - w) // 2, (img.height - h) // 2
    edges = img.crop((left, top, left + w, top + h)).filter(ImageFilter.FIND_EDGES)
    cells = edges.resize((n, n), Image.Resampling.BOX)
    values = list(cells.getdata())
    top_v = sorted(values)[int(len(values) * 0.95)] or 1
    return [[min(1.0, values[r * n + c] / top_v) for c in range(n)] for r in range(n)]


def _detail_under(detail: list[list[float]], rect: Rect, panel: Rect) -> float:
    """Mean busyness of the artwork under a rectangle (page units)."""
    n = len(detail)
    px, py, pw, ph = panel
    c0 = max(0, int((rect[0] - px) / pw * n))
    c1 = min(n, max(c0 + 1, int((rect[0] + rect[2] - px) / pw * n + 0.999)))
    r0 = max(0, int((rect[1] - py) / ph * n))
    r1 = min(n, max(r0 + 1, int((rect[1] + rect[3] - py) / ph * n + 0.999)))
    cells = [detail[r][c] for r in range(r0, r1) for c in range(c0, c1)]
    return sum(cells) / len(cells) if cells else 0.0


def guessed_face_band(position: str, panel: Rect) -> Rect:
    """Where a face may be when the check didn't report it: the upper part of the character's column
    for its script position. Deliberately wide: a guess must not be treated as a known position."""
    px, py, pw, ph = panel
    cx = {"left": 0.22, "center": 0.5, "right": 0.78}.get(position, 0.5)
    x0 = max(0.0, cx - 0.2)
    return (px + x0 * pw, py + 0.05 * ph, (min(1.0, cx + 0.2) - x0) * pw, 0.6 * ph)


def default_head(position: str, panel: Rect) -> Rect:
    px, py, pw, ph = panel
    cx = {"left": 0.22, "center": 0.5, "right": 0.78}.get(position, 0.5)
    return (px + (cx - 0.09) * pw, py + 0.36 * ph, 0.18 * pw, 0.2 * ph)


def _tail_tip(balloon: Rect, head: Rect) -> tuple[float, float]:
    """Point at the mouth and stop just outside the head, so the tail clearly reaches its speaker.
    (It used to stop a fixed share of the head size short, which on a small, distant head left the
    tip well away from it: the final Train panel.)"""
    bx, by = balloon[0] + balloon[2] / 2, balloon[1] + balloon[3] / 2
    # aim at the head's centre line where hair meets face: a tail from above then ends on the head,
    # not beside it (Train p6, where a tail to the mouth line ended next to the sun)
    mx, my = head[0] + head[2] / 2, head[1] + head[3] * (0.3 if by < head[1] else 0.7)
    dx, dy = mx - bx, my - by
    dist = math.hypot(dx, dy) or 1.0
    gap = max(3.0, 0.08 * min(head[2], head[3]))
    # where the line from the balloon to the mouth enters the head box
    t_enter = 0.0
    for lo, hi, start, delta in ((head[0], head[0] + head[2], bx, dx), (head[1], head[1] + head[3], by, dy)):
        if abs(delta) > 1e-9:
            t0, t1 = sorted(((lo - start) / delta, (hi - start) / delta))
            t_enter = max(t_enter, t0)
    reach = max(dist * 0.45, t_enter * dist - gap) if 0.0 < t_enter < 1.0 else dist * 0.45
    return (bx + dx / dist * reach, by + dy / dist * reach)


def face_core(head: Rect) -> Rect:
    """The part of a head box holding the eyes and mouth: below the crown (hair, hat)."""
    return (head[0], head[1] + head[3] * 0.3, head[2], head[3] * 0.7)


def crown(head: Rect) -> Rect:
    return (head[0], head[1], head[2], head[3] * 0.3)


def image_box_to_panel(box: Any, image_size: tuple[int, int] | None,
                       panel_size: tuple[float, float]) -> tuple[dict[str, float] | None, str | None]:  # fmt: skip
    """A box from the panel check is a fraction of the generated IMAGE; the renderer crops that image
    to fill the panel (render._cover), so it must be mapped with the same crop. Returns (box as a
    fraction of the panel, or None; a problem code, or None). Never guesses a fix for a bad box."""
    try:
        x, y, w, h = (float(box[k]) for k in ("x", "y", "w", "h"))
    except (TypeError, KeyError, ValueError):
        return None, "box_malformed"
    if (
        not all(math.isfinite(v) for v in (x, y, w, h))
        or w <= 0
        or h <= 0
        or x < -0.01
        or y < -0.01
        or x + w > 1.02
        or y + h > 1.02
    ):
        return None, "box_out_of_range"  # not a fraction of the image: wrong units or a bad answer
    if not image_size or image_size[0] <= 0 or image_size[1] <= 0:
        return {"x": x, "y": y, "w": w, "h": h}, "image_size_unknown"  # mapped as if uncropped
    iw, ih = image_size
    pw, ph = panel_size
    scale = max(pw / iw, ph / ih)
    rw, rh = iw * scale, ih * scale
    left, top = (rw - pw) / 2, (rh - ph) / 2
    x0, y0 = (x * rw - left) / pw, (y * rh - top) / ph
    x1, y1 = ((x + w) * rw - left) / pw, ((y + h) * rh - top) / ph
    x0, y0, x1, y1 = max(0.0, x0), max(0.0, y0), min(1.0, x1), min(1.0, y1)
    if x1 <= x0 or y1 <= y0:
        return None, "cropped_out"  # entirely in the part of the image the panel doesn't show
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}, None


def layout_panel(
    *, panel_rect: Rect, page_size: tuple[float, float], dialogue: list[dict[str, Any]],
    characters: list[dict[str, Any]], head_boxes: dict[str, dict[str, float]] | None = None,
    action_box: dict[str, float] | None = None, font: str = "comic_neue",
    body_boxes: dict[str, dict[str, float]] | None = None,
    prop_boxes: dict[str, dict[str, float]] | None = None,
    zone: dict[str, float] | None = None,
    detail: list[list[float]] | None = None,
) -> list[Placed]:  # fmt: skip
    """Place every dialogue item of one panel. `dialogue` is in reading order.

    Balloons avoid faces strongly and, when the QA check located them, characters' bodies less
    strongly: in a crowded or narrow panel a balloon may still overlap a body, and is flagged
    ("covers_subject") so the user can see it, rather than shrunk until unreadable.

    `prop_boxes` are required story elements the QA check located (the lantern, the key): kept clear
    almost as strongly as faces ("covers_prop" when that fails). `zone` is the lettering space the
    picture was asked to leave free (art.lettering_plan): balloons prefer it, but never at the cost
    of a face, a prop or the reading order.

    `detail` is how busy the actual artwork is (art_detail): balloons prefer plain areas. It matters
    most when the check didn't locate the faces: then their positions are only guessed from the
    script ("left", "right"), and any overlap with a guessed face is reported as uncertain
    ("face_position_estimated") rather than passed as clear. Balloons never leave the panel to avoid
    a face; when no safe place exists the balloon is flagged for the user to fix."""
    px, py, pw, ph = panel_rect
    heads: dict[str, Rect] = {}
    guessed: set[str] = set()  # faces whose position the check did not report
    for ch in characters:
        found = _abs((head_boxes or {}).get(ch["id"]), panel_rect)
        if found is None:
            guessed.add(ch["id"])
        heads[ch["id"]] = found or default_head(ch.get("position", "center"), panel_rect)
    # Faces (eyes, mouth) are avoided strongly; the crown (hair, hat) lightly, so a balloon may rest on
    # hair when nothing else fits, as letterers do. Guessed heads still steer placement, as before.
    no_go: list[Rect] = [face_core(z) for z in heads.values()]
    crowns: list[Rect] = [crown(z) for z in heads.values()]
    # A guessed head is a script-based estimate: tested on real art it was often wrong, and steering by a
    # wider guess made placement worse. The wider area is used only to report possible overlaps.
    bands = [
        guessed_face_band(ch.get("position", "center"), panel_rect)
        for ch in characters
        if ch["id"] in guessed
    ]
    bodies = [r for ch in characters if (r := _abs((body_boxes or {}).get(ch["id"]), panel_rect))]
    action = _abs(action_box, panel_rect)
    props = [r for b in (prop_boxes or {}).values() if (r := _abs(b, panel_rect))]
    planned = _abs(zone, panel_rect)
    placed: list[Placed] = []
    captions = 0
    pad = min(pw, ph) * 0.03
    prev_key = -1.0
    prev_rect: Rect | None = None  # the previous speech or thought balloon

    for index, item in enumerate(dialogue):
        kind, text = item["type"], " ".join(str(item["text"]).split())
        speaker = item.get("speaker")
        if kind == "sfx":
            size = min(SFX_FONT, ph * 0.22)
            w = min(pw * 0.8, measure(text.upper(), "bangers", size) + 24)
            h = size * 1.3
            target = action or (px + pw * 0.3, py + ph * 0.55, pw * 0.4, ph * 0.3)
            tx = min(max(px + pad, target[0] + target[2] / 2 - w / 2), px + pw - w - pad)
            ty = min(max(py + pad, target[1] - h * 0.9), py + ph - h - pad)
            best_g: dict[str, Any] | None = None
            for scale in (1.0, 0.85, 0.7, 0.55):  # a crowded panel gets a smaller SFX, not one over text
                sw_, sh_, ss_ = w * scale, h * scale, size * scale
                best_cost = math.inf
                for iy in range(9):  # near the action, but never over other lettering
                    for ix in range(9):
                        cx = px + pad + (pw - sw_ - 2 * pad) * ix / 8
                        cy = py + pad + (ph - sh_ - 2 * pad) * iy / 8
                        g = clamp_to_safe_area(kind, text, {"x": cx, "y": cy, "w": sw_, "h": sh_, "font_size": ss_,
                                                            "rotation": -8.0}, page_size)  # fmt: skip
                        ink = ink_bounds(kind, text, g)
                        cost = sum(overlap(ink, _ink_rect(q)) for q in placed) / (ink[2] * ink[3]) * 40.0
                        # sound effects sit near the action but not across faces or bodies
                        cost += sum(overlap(ink, z) for z in no_go) / (ink[2] * ink[3]) * 12.0
                        cost += sum(overlap(ink, z) for z in crowns) / (ink[2] * ink[3]) * 3.0
                        cost += sum(overlap(ink, z) for z in bodies) / (ink[2] * ink[3]) * 4.0
                        if detail:
                            cost += _detail_under(detail, ink, panel_rect) * 1.5
                        cost += sum(overlap(ink, z) for z in props) / (ink[2] * ink[3]) * 6.0
                        cost += math.hypot(g["x"] - tx, g["y"] - ty) / math.hypot(pw, ph)
                        if cost < best_cost - 1e-9:
                            best_cost, best_g = cost, g
                if best_cost < 2.0:  # no overlap (distance alone costs < ~1.5)
                    break
            assert best_g is not None
            sfx = Placed(index, kind, None, text, best_g["x"], best_g["y"], best_g["w"], best_g["h"],
                         best_g["font_size"], rotation=-8.0)  # fmt: skip
            if any(overlap(_ink_rect(sfx), _ink_rect(q)) > 0 for q in placed):
                sfx.flags.append("overlaps_lettering")
            ink_r = _ink_rect(sfx)
            if any(
                overlap(ink_r, face_core(z)) > 0.15 * z[2] * z[3] * 0.7
                for k, z in heads.items()
                if k not in guessed
            ) or any(overlap(ink_r, z) > 0.3 * ink_r[2] * ink_r[3] for z in bodies):
                sfx.flags.append("sfx_covers_character")
            placed.append(sfx)
            continue

        max_w = pw * (0.92 if kind == "caption" else 0.62)
        max_h = ph * 0.46
        size, flags = FONT_MAX, []
        while True:
            w, h = _balloon_size(kind, text, font, size, max_w - (0 if kind == "caption" else 8))
            if (w <= max_w and h <= max_h) or size <= FONT_MIN:
                break
            size -= 1.0
        grow = 0.0
        if w > max_w or h > max_h:
            # Minimum font reached: let the balloon break the panel border before flagging.
            flags.append("breaks_border")
            grow = BORDER_BREAK
            if w > pw * (1 + 2 * grow) or h > ph * 0.8:
                flags.append("overflow")
                w, h = min(w, pw * (1 + 2 * grow)), min(h, ph * 0.8)

        if kind == "caption":
            # corners in the usual order (first caption top-left, next bottom-right, ...), but a
            # corner already holding lettering is skipped
            corners = [(px + pad, py + pad), (px + pw - w - pad, py + ph - h - pad),
                       (px + pw - w - pad, py + pad), (px + pad, py + ph - h - pad)]  # fmt: skip
            corners = corners[captions % 2 :] + corners[: captions % 2]
            options = []
            for n, (cx, cy) in enumerate(corners):
                g = clamp_to_safe_area(kind, text, {"x": cx, "y": cy, "w": w, "h": h}, page_size)
                r = (g["x"], g["y"], g["w"], g["h"])
                options.append((sum(overlap(r, _ink_rect(q)) for q in placed), n, g))
            hit, _, g = min(options, key=lambda o: (o[0], o[1]))
            x, y, w, h = g["x"], g["y"], g["w"], g["h"]
            if hit > 0:
                flags.append("overlaps_lettering")
            captions += 1
            placed.append(Placed(index, kind, None, text, x, y, w, h, size, flags=flags))
            prev_key = max(prev_key, reading_key((x, y, w, h), panel_rect)) if captions == 1 else prev_key
            continue

        head = heads.get(speaker or "")

        def search(
            grow: float,
            w: float,
            h: float,
            head: Rect | None = head,
            prev_key: float = prev_key,
            prev_rect: Rect | None = prev_rect,
        ) -> tuple[float, float, float]:
            x0, x1 = px + pad - grow * pw, px + pw - w - pad + grow * pw
            y0, y1 = py + pad - grow * ph, py + ph - h - pad
            sx, sy, sw, sh = safe_rect(
                page_size
            )  # never into the print margin, even when breaking the border
            x0, x1 = max(sx, x0), min(sx + sw - w, max(x0, x1))
            y0, y1 = max(sy, y0), min(sy + sh - h, max(y0, y1))
            steps = 14
            best: tuple[float, float, float] | None = None
            for iy in range(steps + 1):
                for ix in range(steps + 1):
                    cx = x0 + (x1 - x0) * ix / steps
                    cy = y0 + (y1 - y0) * iy / steps
                    rect = (cx, cy, w, h)
                    cost = sum(overlap(rect, z) for z in no_go) / (w * h) * 12.0
                    cost += sum(overlap(rect, z) for z in crowns) / (w * h) * 3.0
                    cost += sum(overlap(rect, z) for z in props) / (w * h) * 10.0
                    cost += sum(overlap(rect, z) for z in bodies) / (w * h) * 4.0
                    if planned:
                        cost += (1.0 - overlap(rect, planned) / (w * h)) * 0.8
                    if detail:
                        cost += _detail_under(detail, rect, panel_rect) * 3.0
                    if action:
                        cost += overlap(rect, action) / (w * h) * 3.0
                    cost += sum(overlap(rect, _ink_rect(p)) for p in placed) / (w * h) * 40.0
                    cost += (cy - py) / ph * 1.2  # prefer the top of the panel
                    if head:
                        hx, hy = head[0] + head[2] / 2, head[1]
                        cost += math.hypot(cx + w / 2 - hx, cy + h - hy) / math.hypot(pw, ph) * 2.0
                        if cy + h > head[1] + head[3]:
                            cost += 1.5  # balloons belong above the speaker, not across the body
                    if reading_key(rect, panel_rect) <= prev_key + 0.02:
                        cost += 100.0  # must read after the previous balloon
                    elif (
                        prev_rect is not None
                        and cx + w / 2 < prev_rect[0] + prev_rect[2] / 2
                        and cy < prev_rect[1] + prev_rect[3] * 0.6
                    ):
                        # left of the previous balloon (the speakers stand in reverse order): it must sit
                        # clearly lower, or it is read first
                        cost += 100.0
                    if best is None or cost < best[0] - 1e-9:
                        best = (cost, cx, cy)
            assert best is not None
            return best

        sx, sy, sw, sh = safe_rect(page_size)
        w, h = min(w, sw), min(h, sh)
        cost, x, y = search(grow, w, h)
        if grow == 0.0 and kind in ("speech", "thought") and cost > 0.5:
            # The usual wide shape has no good place (a tall close-up): try narrower, taller shapes at
            # the same text size, never smaller, and keep one only if it is clearly better.
            for share in (0.45, 0.34):
                nw, nh = _balloon_size(kind, text, font, size, pw * share - 8)
                if nw > pw * share or nh > ph * 0.6:
                    continue
                nc, nx, ny = search(0.0, nw, nh)
                if nc < cost - 0.3:
                    cost, x, y, w, h = nc, nx, ny, nw, nh
        if cost >= 100.0:
            flags.append("reading_order")
        rect = (x, y, w, h)
        if any(
            overlap(rect, face_core(z)) > 0.15 * z[2] * z[3] * 0.7
            for k, z in heads.items()
            if k not in guessed
        ):
            flags.append("covers_face")
        elif any(overlap(rect, z) > 0.05 * w * h for z in bands):
            # where a face really is wasn't reported: an overlap can't be ruled out
            flags.append("face_position_estimated")
        if any(overlap(rect, z) > 0.25 * z[2] * z[3] for z in props):
            flags.append("covers_prop")  # a required story element is mostly hidden (independent of faces)
        elif "covers_face" not in flags and any(
            overlap(rect, z) > 0.5 * w * h and overlap(rect, z) > 0.1 * z[2] * z[3] for z in bodies
        ):
            flags.append("covers_subject")  # most of the balloon sits on a character
        tail = _tail_tip(rect, head) if head and kind in ("speech", "thought") else None
        if tail is not None and speaker in heads:
            others = [z for k, z in heads.items() if k != speaker]
            to_speaker = _dist_to(tail, heads[speaker])
            if speaker in guessed:
                flags.append("tail_target_estimated")  # aimed at a guessed head position
            elif any(_dist_to(tail, z) < to_speaker for z in others):
                flags.append("tail_ambiguous")  # the tail ends nearer another character
        if tail is not None:  # a tail aimed at a speaker near the page edge stops inside the safe area
            sx, sy, sw, sh = safe_rect(page_size)
            tail = (min(max(tail[0], sx), sx + sw), min(max(tail[1], sy), sy + sh))
            if any(_segment_hits((x + w / 2, y + h / 2), tail, _ink_rect(q)) for q in placed):
                flags.append("tail_crosses_lettering")
        if any(overlap(rect, _ink_rect(q)) > 0 for q in placed):
            flags.append("overlaps_lettering")  # the panel is too full: shown to the user to fix
        placed.append(Placed(index, kind, speaker, text, x, y, w, h, size, tail=tail, flags=flags))
        prev_key = reading_key(rect, panel_rect)
        prev_rect = rect
    return placed


TYPESET_VERSION = 2  # 2: smaller text box inside speech and thought balloons (new balloons only)


def typeset(
    kind: str, text: str, geometry: dict[str, Any], font: str = "comic_neue"
) -> tuple[dict[str, Any], list[str]]:
    """The shared layout contract between the editor and every export.

    Fits `text` inside the balloon's text box and returns the geometry with the result
    stored: `text_size` (the fitted size, never above `font_size`) and `lines` (the exact
    line breaks). The browser and the server renderer both draw these stored lines with the
    same font files and never re-wrap, so a balloon breaks identically on screen and on paper.
    Returns the updated geometry and its flags ("overflow" if it cannot fit at READABLE_MIN: the
    text is never shrunk below the readable floor; the user shortens, moves or resizes instead).
    """
    g = dict(geometry)
    g.setdefault("font_size", FONT_MAX)
    missing = ["unsupported_characters"] if unsupported_chars(text) else []
    if kind == "sfx":
        g.update(
            text_size=float(g["font_size"]), lines=[" ".join(text.split()).upper()], typeset=TYPESET_VERSION
        )
        return g, missing
    iw, ih = inner_box(kind, float(g["w"]), float(g["h"]))
    size, lines, fits = fit_text(
        " ".join(text.split()), font, iw, ih, max(float(g["font_size"]), READABLE_MIN), min_size=READABLE_MIN
    )
    g.update(text_size=size, lines=lines, typeset=TYPESET_VERSION)
    return g, missing + ([] if fits else ["overflow"])


def printed_pt(text_size: float, page_width: float) -> float:
    """Letter size in points when the page is printed at the PDF trim width."""
    return round(text_size * PRINT_WIDTH_PT / page_width, 2)


def below_readable(geometry: dict[str, Any]) -> bool:
    """Stored typesetting smaller than the floor (balloons set before the floor existed)."""
    size = geometry.get("text_size")
    return size is not None and float(size) < READABLE_MIN - 1e-6


def check_geometry(kind: str, text: str, geometry: dict[str, Any], font: str = "comic_neue") -> list[str]:
    return typeset(kind, text, geometry, font)[1]
