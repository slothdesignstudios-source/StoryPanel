"""Structured contracts for every pipeline stage (the Story Bible is the source of truth).

These models double as LLM output schemas, so they stay free of constraints that
structured-output grammars cannot express; limits are enforced in domain/validation.py.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgeRating(StrEnum):
    ALL_AGES = "all_ages"
    TEEN = "teen"
    MATURE = "mature"


class ComicFormat(StrEnum):
    WESTERN_PAGE = "western_page"
    STRIP = "strip"


SLUG = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def slugify(value: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    if not s or not s[0].isalpha():
        s = "x_" + s
    return s[:40]


# ---------------------------------------------------------------- intake
class ComicBrief(Strict):
    summary: str
    genre: list[str]
    tone: str
    audience: AgeRating
    pages: int
    format: ComicFormat
    style_preset: str
    named_characters: list[str]
    setting: str
    must_haves: list[str]
    # strip only: the panel count the user asked for (3 or 4), else null
    panels: int | None = None


class ClarifyingQuestion(Strict):
    id: Literal["audience", "length", "style"]
    question: str
    options: list[str]
    default: str


class IntakeResult(Strict):
    brief: ComicBrief
    questions: list[ClarifyingQuestion]


# ---------------------------------------------------------------- bible & cast
class LocationSpec(Strict):
    id: str
    name: str
    description: str

    @field_validator("id")
    @classmethod
    def _slug(cls, v: str) -> str:
        return v if SLUG.match(v) else slugify(v)


class FormatSpec(Strict):
    type: ComicFormat
    pages: int
    panels_per_page: str
    panels: int | None = None  # strip: exact panel count requested in the brief


class StoryBible(Strict):
    title: str
    logline: str
    audience: AgeRating
    genre: list[str]
    format: FormatSpec
    style_preset: str
    setting: str
    tone: str
    themes: list[str]
    world_rules: list[str]
    palette: list[str]
    locations: list[LocationSpec]
    cast: list[str]


class CharacterVisual(Strict):
    type: str
    body: str
    head: str
    hair: str
    outfit: str
    palette: dict[str, str]
    signature_features: list[str]


class CharacterSpec(Strict):
    id: str
    name: str
    role: str
    personality: str
    voice: str
    visual: CharacterVisual

    @field_validator("id")
    @classmethod
    def _slug(cls, v: str) -> str:
        return v if SLUG.match(v) else slugify(v)

    def canonical_description(self) -> str:
        """Identical wording in every prompt (consistency layer 2). Deterministic."""
        v = self.visual
        parts = [v.type, v.body, v.head]
        if v.hair and v.hair.lower() not in ("none", "n/a", ""):
            parts.append(v.hair)
        parts.append(v.outfit)
        desc = ", ".join(p.strip().rstrip(".") for p in parts if p.strip())
        feats = ", ".join(f.strip() for f in v.signature_features)
        return f"{desc}; signature features: {feats}"


class CastDesign(Strict):
    characters: list[CharacterSpec]


class BibleAndCast(Strict):
    bible: StoryBible
    characters: list[CharacterSpec]


# ---------------------------------------------------------------- script
class Beat(Strict):
    name: str
    summary: str
    pages: list[int]


class BeatSheet(Strict):
    structure: Literal["kishotenketsu", "three_act"]
    beats: list[Beat]


class DialogueItem(Strict):
    type: Literal["speech", "thought", "caption", "sfx"]
    speaker: str | None = None
    text: str


class PanelCharacter(Strict):
    id: str
    position: Literal["left", "center", "right"]
    pose: str
    expression: str


class PanelSpec(Strict):
    shot: Literal["wide", "medium", "close_up"]
    angle: str
    location_id: str
    time_of_day: str
    characters: list[PanelCharacter]
    action: str
    mood: str
    emphasis: Literal["normal", "large"] = "normal"
    dialogue: list[DialogueItem] = Field(default_factory=list)


class PageSpec(Strict):
    panels: list[PanelSpec]


class ComicScript(Strict):
    pages: list[PageSpec]


class ScriptDraft(Strict):
    beat_sheet: BeatSheet
    script: ComicScript


class ScriptIssue(Strict):
    code: str
    path: str
    message: str
    severity: Literal["error", "warning"] = "error"


# ---------------------------------------------------------------- QA
class Box(Strict):
    """Normalised [0,1] box within a panel image."""

    x: float
    y: float
    w: float
    h: float


class CharacterObservation(Strict):
    id: str
    present: bool
    head_box: Box | None = None
    body_box: Box | None = None
    missing_features: list[str]
    identity_rating: int  # 1..5 against the supplied reference


class VisionQAReport(Strict):
    character_count: int
    characters: list[CharacterObservation]
    action_matches: bool
    visible_text: bool
    anatomy_defects: list[str]
    action_box: Box | None = None
    fix_notes: list[str]


class SafetyVerdict(Strict):
    allowed: bool
    categories: list[str]
    severity: Literal["none", "low", "high"]


class ModerationVerdict(Strict):
    allowed: bool
    categories: list[str]
    user_message: str
    suggested_rewrite: str | None = None
