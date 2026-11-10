"""Targeted repair: repaint only the region that failed QA instead of the whole panel.

Used for local defects on one character (a missing signature feature, identity drift).
The mask is built from the character's box reported by the checker; after the provider
returns, the result is composited back through the same mask, so every pixel outside the
region is guaranteed identical to the original whatever the model did.
"""

from __future__ import annotations

import io
from typing import Any

from PIL import Image, ImageDraw, ImageFilter

LOCAL_PREFIXES = ("features:", "identity_rating:", "identity_embedding:")
PAD = 0.06


def repair_target(problems: list[str], body_boxes: dict[str, dict[str, float]]) -> dict[str, Any] | None:
    """The region to repaint, or None when the failure is not local to one character."""
    if not problems or not all(p.startswith(LOCAL_PREFIXES) for p in problems):
        return None
    ids = {p.split(":", 1)[1] for p in problems}
    if len(ids) != 1:
        return None
    (cid,) = ids
    box = body_boxes.get(cid)
    if not box or box["w"] * box["h"] > 0.6:  # most of the frame: a full redraw is the honest fix
        return None
    x, y = max(0.0, box["x"] - PAD), max(0.0, box["y"] - PAD)
    return {"character": cid, "box": {"x": x, "y": y, "w": min(1 - x, box["w"] + 2 * PAD),
                                      "h": min(1 - y, box["h"] + 2 * PAD)}}  # fmt: skip


def build_mask(image: bytes, box: dict[str, float]) -> bytes:
    """White = repaint, black = keep. Same pixel size as the image (required by the provider)."""
    with Image.open(io.BytesIO(image)) as img:
        w, h = img.size
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rectangle(
        [int(box["x"] * w), int(box["y"] * h), int((box["x"] + box["w"]) * w), int((box["y"] + box["h"]) * h)],
        fill=255)  # fmt: skip
    out = io.BytesIO()
    mask.save(out, format="PNG")
    return out.getvalue()


def composite(original: bytes, repaired: bytes, mask: bytes) -> bytes:
    """Keep the original everywhere outside the mask; feather 3px inside the edge."""
    base = Image.open(io.BytesIO(original)).convert("RGB")
    new = Image.open(io.BytesIO(repaired)).convert("RGB")
    if new.size != base.size:
        new = new.resize(base.size, Image.Resampling.LANCZOS)
    m = Image.open(io.BytesIO(mask)).convert("L")
    # erode-then-blur keeps the feather strictly inside the masked region
    soft = m.filter(ImageFilter.MinFilter(7)).filter(ImageFilter.GaussianBlur(2))
    soft = Image.composite(soft, Image.new("L", m.size, 0), m)
    base.paste(new, (0, 0), soft)
    out = io.BytesIO()
    base.save(out, format="PNG")
    return out.getvalue()
