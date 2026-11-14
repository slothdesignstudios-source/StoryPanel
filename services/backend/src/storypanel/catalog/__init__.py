"""Versioned, code-owned catalogs: style presets, page layouts and prompt templates.

Loaded from packages/ at the repo root (or SP_CATALOG_DIR in a container image).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from storypanel.domain.schemas import ComicFormat
from storypanel.errors import AppError, ErrorCode


def catalog_root() -> Path:
    env = os.environ.get("SP_CATALOG_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[5] / "packages"


# ------------------------------------------------------------------ styles
@dataclass(frozen=True)
class StylePreset:
    id: str
    name: str
    blurb: str
    token: str
    swatch: tuple[str, ...]
    font: str
    sfx_font: str


@dataclass(frozen=True)
class StyleCatalog:
    version: str
    default: str
    free_plan_presets: tuple[str, ...]
    presets: dict[str, StylePreset]

    def get(self, preset_id: str) -> StylePreset:
        try:
            return self.presets[preset_id]
        except KeyError:
            raise AppError(ErrorCode.VALIDATION, "Unknown style preset.",
                           fields=[{"field": "style_preset", "message": "Unknown preset."}]) from None  # fmt: skip

    def allowed_for_plan(self, plan: str) -> list[str]:
        return list(self.free_plan_presets) if plan == "free" else list(self.presets)


@lru_cache
def styles() -> StyleCatalog:
    raw = json.loads((catalog_root() / "styles" / "presets.json").read_text())
    presets = {
        p["id"]: StylePreset(p["id"], p["name"], p["blurb"], p["token"], tuple(p["swatch"]),
                             p["lettering"]["font"], p["lettering"]["sfx_font"])
        for p in raw["presets"]
    }  # fmt: skip
    return StyleCatalog(raw["version"], raw["default"], tuple(raw["free_plan_presets"]), presets)


# ------------------------------------------------------------------ layouts
@dataclass(frozen=True)
class LayoutTemplate:
    id: str
    format: ComicFormat
    page: tuple[int, int]
    gutter: int
    margin: int
    panel_count: int
    large_index: int | None
    rects: tuple[tuple[int, int, int, int], ...]


@lru_cache
def layouts() -> dict[str, LayoutTemplate]:
    raw = json.loads((catalog_root() / "layouts" / "templates.json").read_text())
    return {
        t["id"]: LayoutTemplate(t["id"], ComicFormat(t["format"]), tuple(t["page"]), t["gutter"],
                                t["margin"], t["panel_count"], t["large_index"],
                                tuple(tuple(r) for r in t["rects"]))
        for t in raw["templates"]
    }  # fmt: skip


@lru_cache
def layout_version() -> str:
    return str(json.loads((catalog_root() / "layouts" / "templates.json").read_text())["version"])


# ------------------------------------------------------------------ prompts
PROMPT_VERSION = "v2"  # v1 kept: earlier images record the version that drew them


@lru_cache
def _prompt_env(version: str) -> tuple[Environment, dict[str, str]]:
    root = catalog_root() / "prompts" / version
    manifest = json.loads((root / "manifest.json").read_text())
    env = Environment(
        loader=FileSystemLoader(str(root)), undefined=StrictUndefined, autoescape=False,  # noqa: S701 - plain-text prompts, never HTML
        trim_blocks=False, keep_trailing_newline=False,
    )  # fmt: skip
    return env, manifest["templates"]


def render_prompt(template: str, /, _version: str = PROMPT_VERSION, **ctx: Any) -> str:
    env, templates = _prompt_env(_version)
    return env.get_template(templates[template]).render(**ctx).strip()


def prompt_sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def font_path(font: str, bold: bool = False) -> Path:
    files = {
        ("comic_neue", False): "comic-neue-latin-400-normal.woff",
        ("comic_neue", True): "comic-neue-latin-700-normal.woff",
        ("bangers", False): "bangers-latin-400-normal.woff",
        ("bangers", True): "bangers-latin-400-normal.woff",
    }
    return catalog_root() / "fonts" / files[(font, bold)]
