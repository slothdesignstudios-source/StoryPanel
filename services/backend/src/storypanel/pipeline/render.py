"""Compositor and exporters. Pure functions over an immutable page description.

A `PageDoc` is plain data (the same shape the export snapshot stores), so rendering is
deterministic and cannot be affected by edits made while an export runs. Text is drawn
with the bundled fonts; user text is only ever drawn as glyphs, never parsed as markup.
"""

from __future__ import annotations

import io
import math
import zipfile
from dataclasses import dataclass
from typing import Any
from xml.sax.saxutils import escape

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as pdf_canvas

from storypanel.catalog import font_path
from storypanel.pipeline.lettering import LINE_HEIGHT, typeset

INK = (17, 17, 17)
PAPER = (255, 255, 255)
CAPTION_FILL = (255, 244, 200)
SUPERSAMPLE = 2


@dataclass(frozen=True)
class PanelDoc:
    rect: tuple[float, float, float, float]
    image: bytes | None  # None renders an empty frame (blocked/failed panels)
    balloons: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class PageDoc:
    size: tuple[int, int]  # page units
    panels: tuple[PanelDoc, ...]
    font: str = "comic_neue"
    sfx_font: str = "bangers"
    watermark: bool = False


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    scale = max(w / img.width, h / img.height)
    resized = img.resize((max(w, round(img.width * scale)), max(h, round(img.height * scale))),
                         Image.Resampling.LANCZOS)  # fmt: skip
    left, top = (resized.width - w) // 2, (resized.height - h) // 2
    return resized.crop((left, top, left + w, top + h))


def _font(name: str, size: float, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(font_path(name, bold)), size)


def _draw_text(d: ImageDraw.ImageDraw, g: dict[str, Any], kind: str, text: str, font: str, cx: float,
               cy: float, k: float) -> None:  # fmt: skip
    # Draw the stored typesetting (the same lines the editor shows). Geometry saved before
    # the layout contract existed is typeset here with the same function.
    if "lines" not in g:
        g = typeset(kind, text, g, font)[0]
    size, lines = float(g["text_size"]), list(g["lines"])
    f = _font(font, size * k)
    line_h = size * LINE_HEIGHT * k
    y = cy * k - len(lines) * line_h / 2
    for line in lines:
        d.text((cx * k, y + line_h / 2), line, font=f, fill=INK, anchor="mm")
        y += line_h


def _tail_polygon(g: dict[str, Any]) -> list[tuple[float, float]] | None:
    tail = g.get("tail")
    if not tail:
        return None
    cx, cy = g["x"] + g["w"] / 2, g["y"] + g["h"] / 2
    dx, dy = tail["x"] - cx, tail["y"] - cy
    dist = math.hypot(dx, dy)
    if dist < 1:
        return None
    nx, ny = -dy / dist, dx / dist
    half = min(g["w"], g["h"]) * 0.12
    # Base sits inside the ellipse so the balloon outline hides the join.
    bx, by = cx + dx / dist * min(g["w"], g["h"]) * 0.3, cy + dy / dist * min(g["w"], g["h"]) * 0.3
    return [(bx + nx * half, by + ny * half), (tail["x"], tail["y"]), (bx - nx * half, by - ny * half)]


def _draw_balloon(layer: Image.Image, b: dict[str, Any], doc: PageDoc, k: float) -> None:
    d = ImageDraw.Draw(layer)
    g, kind, text = b["geometry"], b["type"], str(b["text"])
    x, y, w, h = g["x"], g["y"], g["w"], g["h"]
    box = [x * k, y * k, (x + w) * k, (y + h) * k]
    stroke = max(2, round(2.6 * k))
    if kind == "sfx":
        size = float(g.get("font_size", 48))
        f = _font(doc.sfx_font, size * k)
        tile = Image.new("RGBA", (round(w * k) + 40, round(h * k) + 40), (0, 0, 0, 0))
        td = ImageDraw.Draw(tile)
        td.text((tile.width / 2, tile.height / 2), text.upper(), font=f, fill=(255, 221, 51),
                anchor="mm", stroke_width=max(2, round(3 * k)), stroke_fill=INK)  # fmt: skip
        tile = tile.rotate(-float(g.get("rotation", 0.0)), expand=True, resample=Image.Resampling.BICUBIC)
        layer.alpha_composite(tile, (max(0, round((x + w / 2) * k - tile.width / 2)),
                                     max(0, round((y + h / 2) * k - tile.height / 2))))  # fmt: skip
        return
    if kind == "caption":
        d.rectangle(box, fill=CAPTION_FILL, outline=INK, width=stroke)
    else:
        poly = _tail_polygon(g)
        if kind == "speech" and poly:
            d.polygon([(px * k, py * k) for px, py in poly], fill=PAPER, outline=INK, width=stroke)
        if kind == "thought":
            if poly:  # a trail of shrinking bubbles instead of a pointed tail
                (bx1, by1), (tx, ty), (bx2, by2) = poly
                sx, sy = (bx1 + bx2) / 2, (by1 + by2) / 2
                for n, t in enumerate((0.55, 0.78, 0.95)):
                    r = (9 - 3 * n) * k
                    qx, qy = (sx + (tx - sx) * t) * k, (sy + (ty - sy) * t) * k
                    d.ellipse([qx - r, qy - r, qx + r, qy + r], fill=PAPER, outline=INK, width=stroke)
            bumps = 14
            for n in range(bumps):
                a = 2 * math.pi * n / bumps
                bxc, byc = (x + w / 2 + math.cos(a) * w * 0.44) * k, (y + h / 2 + math.sin(a) * h * 0.44) * k
                r = min(w, h) * 0.13 * k
                d.ellipse([bxc - r, byc - r, bxc + r, byc + r], fill=PAPER, outline=INK, width=stroke)
            inset = [
                box[0] + w * 0.06 * k,
                box[1] + h * 0.06 * k,
                box[2] - w * 0.06 * k,
                box[3] - h * 0.06 * k,
            ]
            d.ellipse(inset, fill=PAPER)
        else:
            d.ellipse(box, fill=PAPER, outline=INK, width=stroke)
            if poly:  # repaint the tail's inside so the ellipse outline does not cut across it
                (bx1, by1), (tx, ty), (bx2, by2) = poly
                cx, cy = x + w / 2, y + h / 2
                shrink = [
                    (cx + (px - cx) * 0.94, cy + (py - cy) * 0.94) for px, py in ((bx1, by1), (bx2, by2))
                ]
                inner = [shrink[0], ((bx1 + bx2 + tx * 4) / 6, (by1 + by2 + ty * 4) / 6), shrink[1]]
                d.polygon([(px * k, py * k) for px, py in inner], fill=PAPER)
    _draw_text(d, g, kind, text, doc.font, x + w / 2, y + h / 2, k)


def render_page(doc: PageDoc, width_px: int) -> bytes:
    """Render one page to PNG at the requested pixel width."""
    k_out = width_px / doc.size[0]
    k = k_out * SUPERSAMPLE
    W, H = round(doc.size[0] * k), round(doc.size[1] * k)
    page = Image.new("RGBA", (W, H), PAPER + (255,))
    d = ImageDraw.Draw(page)
    border = max(2, round(3 * k))
    for p in doc.panels:
        x, y, w, h = (round(v * k) for v in p.rect)
        if p.image:
            with Image.open(io.BytesIO(p.image)) as img:
                page.paste(_cover(img.convert("RGB"), w, h), (x, y))
        else:
            d.rectangle([x, y, x + w, y + h], fill=(232, 232, 232))
            d.line([x, y, x + w, y + h], fill=(190, 190, 190), width=border)
            d.line([x + w, y, x, y + h], fill=(190, 190, 190), width=border)
        d.rectangle([x, y, x + w, y + h], outline=INK, width=border)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    for p in doc.panels:
        for b in sorted(p.balloons, key=lambda b: b.get("order_index", 0)):
            _draw_balloon(layer, b, doc, k)
    page.alpha_composite(layer)
    if doc.watermark:
        f = _font(doc.font, 13 * k, bold=True)
        ImageDraw.Draw(page).text((W - 14 * k, H - 8 * k), "made with StoryPanel", font=f,
                                  fill=(120, 120, 120), anchor="rs")  # fmt: skip
    out_w, out_h = round(doc.size[0] * k_out), round(doc.size[1] * k_out)
    final = page.convert("RGB").resize((out_w, out_h), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    final.save(buf, format="PNG")
    return buf.getvalue()


# ------------------------------------------------------------------ provenance
# IPTC's Digital Source Type vocabulary is the interoperable way to say "made by a
# generative model"; it is what C2PA assertions and major platforms read. This is a
# metadata label, not a signed C2PA manifest (see docs/decisions.md, provenance).
DIGITAL_SOURCE_TYPE = "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"


def provenance_xmp(title: str) -> str:
    return (
        '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:Iptc4xmpExt="http://iptc.org/std/Iptc4xmpExt/2008-02-29/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:xmp="http://ns.adobe.com/xap/1.0/">'
        f"<Iptc4xmpExt:DigitalSourceType>{DIGITAL_SOURCE_TYPE}</Iptc4xmpExt:DigitalSourceType>"
        "<xmp:CreatorTool>StoryPanel</xmp:CreatorTool>"
        f'<dc:title><rdf:Alt><rdf:li xml:lang="x-default">{escape(title[:200])}</rdf:li></rdf:Alt></dc:title>'
        '<dc:description><rdf:Alt><rdf:li xml:lang="x-default">AI-generated comic. Artwork produced by '
        "generative image models; lettering composed by StoryPanel.</rdf:li></rdf:Alt></dc:description>"
        '</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>'
    )


def label_png(data: bytes, title: str) -> bytes:
    """Re-save a rendered page with AI-generation metadata (pixels unchanged)."""
    from PIL.PngImagePlugin import PngInfo

    info = PngInfo()
    info.add_itxt("XML:com.adobe.xmp", provenance_xmp(title))
    info.add_text("Software", "StoryPanel")
    info.add_text("Comment", "AI-generated image")
    with Image.open(io.BytesIO(data)) as img:
        out = io.BytesIO()
        img.save(out, format="PNG", pnginfo=info)
    return out.getvalue()


# ------------------------------------------------------------------ exporters
def build_pdf(pages: list[bytes], title: str, page_units: tuple[int, int]) -> bytes:
    """One PDF page per comic page, 6 inches wide, with a 0.125 in bleed margin for print."""
    bleed = 9.0
    pw = 432.0
    ph = pw * page_units[1] / page_units[0]
    buf = io.BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=(pw + 2 * bleed, ph + 2 * bleed), pageCompression=1)
    c.setTitle(title[:200])
    c.setCreator("StoryPanel")
    c.setSubject("AI-generated comic")
    c.setKeywords(f"AI-generated; DigitalSourceType={DIGITAL_SOURCE_TYPE}")
    for data in pages:
        c.drawImage(ImageReader(io.BytesIO(data)), bleed, bleed, width=pw, height=ph)
        c.showPage()
    c.save()
    return buf.getvalue()


def build_cbz(pages: list[bytes], title: str) -> bytes:
    buf = io.BytesIO()
    info = ('<?xml version="1.0" encoding="utf-8"?>\n<ComicInfo>'
            f"<Title>{escape(title[:200])}</Title><PageCount>{len(pages)}</PageCount>"
            f"<Notes>AI-generated with StoryPanel. DigitalSourceType: {DIGITAL_SOURCE_TYPE}</Notes></ComicInfo>")  # fmt: skip
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for n, data in enumerate(pages, start=1):
            z.writestr(f"page_{n:03d}.png", data)  # zero-padded so readers sort correctly
        z.writestr("ComicInfo.xml", info)
    return buf.getvalue()


def build_png_bundle(pages: list[bytes]) -> tuple[bytes, str, str]:
    """A single PNG for one page; otherwise a zip of numbered PNG pages."""
    if len(pages) == 1:
        return pages[0], "image/png", "png"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for n, data in enumerate(pages, start=1):
            z.writestr(f"page_{n:03d}.png", data)
    return buf.getvalue(), "application/zip", "zip"


def balloon_layer_svg(doc: PageDoc) -> str:
    """Vector lettering layer as SVG (for debugging and future vector exports).
    Every user string is XML-escaped; no user value is ever placed in an attribute name,
    URL or script context."""
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {doc.size[0]} {doc.size[1]}">']
    for p in doc.panels:
        for b in p.balloons:
            g = b["geometry"]
            x, y, w, h = (float(g[key]) for key in ("x", "y", "w", "h"))
            if b["type"] == "caption":
                parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="#fff4c8" stroke="#111"/>')
            elif b["type"] != "sfx":
                parts.append(f'<ellipse cx="{x + w / 2}" cy="{y + h / 2}" rx="{w / 2}" ry="{h / 2}" '
                             'fill="#fff" stroke="#111"/>')  # fmt: skip
            parts.append(f'<text x="{x + w / 2}" y="{y + h / 2}" text-anchor="middle" '
                         f'font-size="{float(g.get("font_size", 22))}">{escape(str(b["text"]))}</text>')  # fmt: skip
    parts.append("</svg>")
    return "".join(parts)
