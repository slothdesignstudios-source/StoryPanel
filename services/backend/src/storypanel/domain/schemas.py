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


class ContinuityRule(Strict):
    """A rule the pictures (or the story) must keep. `must` rules are required wherever they apply;
    `prefer` rules are stylistic and only requested where natural. `visual` rules go into image
    prompts and image checks; non-visual ones only into the script check.

    `source` says where the wording came from: the Bible writer ("model"), a classified world
    rule ("world_rule": `text` is that rule's original wording, unchanged) or the user."""

    id: str
    text: str
    applies_to: Literal["every_panel", "character", "location"]
    target: str | None = None  # character id or location id; None for every_panel
    strength: Literal["must", "prefer"] = "must"
    visual: bool = True
    source: Literal["model", "world_rule", "user"] = "model"
    world_rule_index: int | None = None

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
    # Absent from Bibles written before 2026-11-14: their world rules are offered for review and
    # classification instead (never converted silently).
    continuity_rules: list[ContinuityRule] = Field(default_factory=list)


class CharacterVisual(Strict):
    type: str
    body: str
    head: str
    hair: str
    outfit: str
    palette: dict[str, str]
    signature_features: list[str]
    # Added with prompts v3; empty for characters designed earlier (their descriptions are unchanged).
    silhouette: str = ""  # what makes the character recognisable as a solid shape
    proportions: str = ""  # height in heads and size relative to the rest of the cast


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
        shape = "; ".join(x.strip().rstrip(".") for x in (v.silhouette, v.proportions) if x.strip())
        return f"{desc}; signature features: {feats}" + (f"; shape: {shape}" if shape else "")


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


class ElementState(Strict):
    """Where a story element (a gift, a note, flowers about to bloom) stands in one panel. Separate from
    continuity rules, which are facts that hold in every panel they apply to.

    absent:    not physically in the scene yet;
    hidden:    in the scene but hidden from the reader (in a bag, under a cup, out of frame);
    unnoticed: visible to the reader but not yet noticed by the characters (deliberate foreshadowing);
    revealed:  revealed to both the reader and the characters."""

    element: str  # the same short wording in every panel, e.g. "the ticket stub"
    state: Literal["absent", "hidden", "unnoticed", "revealed"]


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
    # Beat-sheet beats this panel expresses (a panel may carry two in a 3-panel strip).
    beats: list[str] = Field(default_factory=list)
    # Continuity rules the user decided do not apply to this panel (recorded, versioned).
    rule_exceptions: list[str] = Field(default_factory=list)
    # Added with prompts v3 (empty in earlier scripts): what this panel changes for the reader, and
    # the one thing the eye should land on. Both feed the panel's visual plan.
    purpose: str = ""
    focus: str = ""
    # Added with prompts v4: the timing of story elements in this panel (empty in earlier scripts).
    element_states: list[ElementState] = Field(default_factory=list)


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
    # error: blocks approval until fixed; review: needs an explicit, recorded acknowledgement to
    # approve (uncertain or unavailable consistency checks); warning: informational.
    severity: Literal["error", "warning", "review"] = "error"

    @property
    def key(self) -> str:
        return f"{self.code}@{self.path}"


# ---------------------------------------------------------------- consistency checks
class RuleVerdict(Strict):
    rule_id: str
    verdict: Literal["satisfied", "violated", "not_applicable", "uncertain"]
    reason: str


class BeatVerdict(Strict):
    beat: str
    expressed: Literal["yes", "no", "uncertain"]
    reason: str


class PanelConsistency(Strict):
    page: int  # 1-based
    panel: int  # 1-based
    rules: list[RuleVerdict]
    beats: list[BeatVerdict]
    # Storytelling judgments (prompts v3; informational, never blocking): does the panel change
    # something for the reader, and does its lettering only restate what the picture shows?
    adds: Literal["yes", "no", "uncertain"] = "yes"
    restates_art: bool = False
    reveals_early: bool = False  # prompts v4: shows something the beat sheet reveals later
    note: str = ""


class ConsistencyConflict(Strict):
    kind: Literal["rule_vs_rule", "rule_vs_beat", "impossible"]
    rule_ids: list[str]
    description: str


class ScriptConsistencyReport(Strict):
    panels: list[PanelConsistency]
    conflicts: list[ConsistencyConflict]
    # Does the last panel pay off the premise set up at the start? (prompts v3; informational)
    payoff: Literal["yes", "weak", "no", "uncertain"] = "yes"
    payoff_reason: str = ""


class SheetCheckItem(Strict):
    item: str
    kind: Literal["signature_feature", "rule"]
    # "situational": a rule that only applies in a situation a reference sheet does not show
    # (weather, place, time of day, an action). It is listed for information and never flags the sheet.
    visible: Literal["yes", "no", "uncertain", "situational"]
    depicted_correctly: Literal["yes", "no", "uncertain"]
    note: str


class SheetCheckReport(Strict):
    items: list[SheetCheckItem]
    whole_figures: Literal["yes", "no", "uncertain"]  # turnaround: every view complete, none cut off
    notes: list[str]


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


class RequiredElement(Strict):
    rule_id: str
    visible: Literal["yes", "no", "uncertain"]
    box: Box | None = None  # where it is drawn (prompts v3), so lettering can keep it clear


class ElementObservation(Strict):
    number: int  # 1-based, as listed in the request
    visible: Literal["yes", "no", "uncertain"]  # can the reader see it in the picture?


class VisionQAReport(Strict):
    character_count: int
    characters: list[CharacterObservation]
    action_matches: bool
    visible_text: bool
    anatomy_defects: list[str]
    action_box: Box | None = None
    fix_notes: list[str]
    # one entry per required visual continuity rule listed in the request
    required_elements: list[RequiredElement] = Field(default_factory=list)
    # prompts v4: one entry per story element listed in the request, by its number
    element_checks: list[ElementObservation] = Field(default_factory=list)


# Gemini visual identity QA: a categorical judgment from comparing the panel with the approved
# reference images. Deliberately not a similarity score: it is never written to the embedding
# column and never compared with embedding thresholds.
class IdentityMismatch(Strict):
    aspect: Literal["face", "hair", "clothing", "colors", "body", "accessory", "species", "other"]
    expected: str
    observed: str


class CharacterIdentityFinding(Strict):
    id: str
    present: bool
    matches_reference: Literal["yes", "no", "uncertain"]
    mismatches: list[IdentityMismatch]
    evidence: str  # what in the panel supports the judgment (short)
    confidence: Literal["high", "medium", "low"]


class IdentityVisualReport(Strict):
    characters: list[CharacterIdentityFinding]
    outcome: Literal["pass", "repair", "manual_review"]
    notes: list[str]


class SafetyVerdict(Strict):
    allowed: bool
    categories: list[str]
    severity: Literal["none", "low", "high"]


class ModerationVerdict(Strict):
    allowed: bool
    categories: list[str]
    user_message: str
    suggested_rewrite: str | None = None
