"""HTTP request/response contracts. The frontend's TypeScript types are generated from the
OpenAPI document these produce (packages/api-types), and CI fails on drift."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from storypanel.domain.schemas import (
    BeatSheet,
    CharacterSpec,
    ClarifyingQuestion,
    ComicBrief,
    ComicScript,
    ScriptIssue,
    StoryBible,
)  # fmt: skip


class In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ---------------------------------------------------------------- errors
class ErrorField(BaseModel):
    field: str
    message: str


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str | None = None
    retryable: bool
    fields: list[ErrorField] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


# ---------------------------------------------------------------- requests
class CreateProject(In):
    prompt: str = Field(min_length=10, max_length=2000)
    pages: int | None = Field(default=None, ge=1, le=8)
    format: Literal["western_page", "strip"] | None = None
    style_preset: str | None = Field(default=None, max_length=60)
    audience: Literal["all_ages", "teen", "mature"] | None = None
    # Image provider for the whole comic (sheets, panels, repairs, edits). Default: the server's.
    image_provider: Literal["openai", "gemini", "fal"] | None = None


class Answers(In):
    answers: dict[Literal["audience", "length", "style"], str] = Field(default_factory=dict)


class ReviseCharacter(In):
    instruction: str = Field(min_length=3, max_length=400)


class ApproveCast(In):
    cast_version: int = Field(ge=1)
    # Review items (flagged or unchecked reference sheets) the user explicitly accepts; recorded.
    acknowledged: list[str] = Field(default_factory=list, max_length=50)


class ApproveScript(In):
    script_version: int = Field(ge=1)
    # Review items (uncertain or unavailable Story Bible checks) the user explicitly accepts;
    # recorded with the approval. Errors and safety/moderation refusals cannot be acknowledged.
    acknowledged: list[str] = Field(default_factory=list, max_length=50)


class ContinuityRuleIn(In):
    id: str | None = Field(default=None, max_length=40)
    text: str = Field(min_length=1, max_length=300)
    applies_to: Literal["every_panel", "character", "location"]
    target: str | None = Field(default=None, max_length=40)
    strength: Literal["must", "prefer"] = "must"
    visual: bool = True
    source: Literal["model", "world_rule", "user"] = "user"
    world_rule_index: int | None = Field(default=None, ge=0, le=7)


class PutContinuityRules(In):
    cast_version: int = Field(ge=1)
    rules: list[ContinuityRuleIn] = Field(max_length=12)


class PatchScript(In):
    base_version: int = Field(ge=1)
    script: ComicScript


BibleItem = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class PatchBible(In):
    """Whitespace is stripped first, so blank values are refused, not stored."""

    cast_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    logline: str | None = Field(default=None, min_length=1, max_length=600)
    setting: str | None = Field(default=None, min_length=1, max_length=600)
    tone: str | None = Field(default=None, min_length=1, max_length=200)
    themes: list[BibleItem] | None = Field(default=None, max_length=8)
    world_rules: list[BibleItem] | None = Field(default=None, max_length=8)


class EditPanel(In):
    instruction: str = Field(min_length=3, max_length=400)


class Geometry(In):
    x: float
    y: float
    w: float = Field(gt=10)
    h: float = Field(gt=10)
    font_size: float = Field(default=22, ge=8, le=120)
    rotation: float = Field(default=0, ge=-45, le=45)
    tail: dict[Literal["x", "y"], float] | None = None
    # Typesetting is server-owned (lettering.typeset). Clients may echo these back; they
    # are accepted and recomputed, never trusted.
    text_size: float | None = None
    lines: list[str] | None = Field(default=None, max_length=40)
    typeset: int | None = None


class PatchBalloon(In):
    version: int = Field(ge=1)
    text: str | None = Field(default=None, min_length=1, max_length=300)
    geometry: Geometry | None = None


class CreateBalloon(In):
    type: Literal["speech", "thought", "caption", "sfx"]
    text: str = Field(min_length=1, max_length=300)
    speaker_id: str | None = Field(default=None, max_length=40)


class PatchLayout(In):
    layout_template_id: str = Field(max_length=40)


class ReorderPanels(In):
    panel_ids: list[str] = Field(min_length=1, max_length=8)


class CreateExport(In):
    format: Literal["pdf", "png", "cbz"]


class Checkout(In):
    product: Literal["creator", "pro", "credit_pack"]


class DevLogin(In):
    email: str = Field(min_length=3, max_length=200, pattern=r"^[^@\s]+@[^@\s]+$")


class Feedback(In):
    rating: Literal[-1, 1]


# ---------------------------------------------------------------- responses
class Out(BaseModel):
    pass


class StyleOut(Out):
    id: str
    name: str
    blurb: str
    swatch: list[str]
    available: bool


class LayoutOut(Out):
    id: str
    panel_count: int
    page: list[int]
    rects: list[list[int]]


class ImageProviderOut(Out):
    id: str
    label: str
    model: str | None
    available: bool
    reason: str | None  # why it cannot be chosen, when unavailable
    capabilities: list[str]


class MetaOut(Out):
    image_providers: list[ImageProviderOut]
    default_image_provider: str
    identity_qa: str
    export_resampling: str
    styles: list[StyleOut]
    default_style: str
    default_pages: int
    max_pages: int
    limits: dict[str, int]
    credit_prices: dict[str, int]
    auth_mode: str
    provider_mode: str


class EstimateOut(Out):
    step: str
    credits: int
    breakdown: dict[str, int]
    balance: int
    available: int
    sufficient: bool
    note: str


class ReviewItem(Out):
    key: str
    message: str


class CharacterOut(Out):
    slug: str
    name: str
    spec: CharacterSpec
    sheet_status: str
    sheet_version: int
    revising: bool
    images: dict[str, str]
    # Review of the current sheet (NULL for sheets made before reviews existed): status
    # pending | ok | flagged | unavailable, with problems and uncertain items.
    sheet_check: dict[str, Any] | None
    review: ReviewItem | None  # what approving the cast would acknowledge for this character


class LocationOut(Out):
    slug: str
    name: str
    description: str
    image_url: str | None


class ScriptOut(Out):
    version: int
    source: str
    beats: BeatSheet
    content: ComicScript
    issues: list[ScriptIssue]
    has_errors: bool
    # Story Bible check of this version: pending | done | unavailable; NULL = written before the
    # check existed (treated as not checked).
    consistency: dict[str, Any] | None
    review_required: list[ReviewItem]  # must be acknowledged to approve this version


class BalloonOut(Out):
    id: str
    panel_id: str
    order_index: int
    type: str
    speaker_id: str | None
    text: str
    geometry: dict[str, Any]
    flags: list[str]
    user_edited: bool
    version: int


class PanelOut(Out):
    id: str
    index: int
    rect: list[float]
    status: str
    spec: dict[str, Any]
    current_version_id: str | None
    image_url: str | None
    qa_problems: list[str]
    balloons: list[BalloonOut]
    busy: bool


class PageOut(Out):
    id: str
    index: int
    layout_template_id: str
    size: list[int]
    status: str
    preview_url: str | None
    layout_options: list[LayoutOut]
    panels: list[PanelOut]


class ExportOut(Out):
    id: str
    format: str
    status: str
    created_at: datetime
    download_url: str | None
    error: dict[str, Any] | None
    edit_version: int
    print_quality: dict[str, Any] | None = None  # effective DPI per panel and warnings


class ProgressOut(Out):
    """Counts of finished units. Deliberately not a percentage or a time estimate."""

    panels_total: int
    panels_done: int
    pages_total: int
    pages_ready: int
    characters_total: int
    characters_ready: int


class ProjectSummary(Out):
    id: str
    title: str
    status: str
    format: str
    style_preset: str | None
    pages_requested: int
    created_at: datetime
    updated_at: datetime
    cover_url: str | None
    failure: dict[str, Any] | None
    image_provider: str | None  # null: created before per-comic providers (drawn with fal)
    image_model: str | None


class ProjectOut(ProjectSummary):
    prompt: str
    brief: ComicBrief | None
    questions: list[ClarifyingQuestion]
    bible: StoryBible | None
    cast_version: int
    script_version: int
    edit_version: int
    event_seq: int
    approved_cast_version: int | None
    approved_script_version: int | None
    resume_from: str | None
    characters: list[CharacterOut]
    locations: list[LocationOut]
    script: ScriptOut | None
    pages: list[PageOut]
    exports: list[ExportOut]
    progress: ProgressOut


class ProjectList(Out):
    projects: list[ProjectSummary]


class Accepted(Out):
    request_id: str
    status: str = "accepted"


class VersionOut(Out):
    id: str
    kind: str
    instruction: str | None
    qa_status: str
    qa_problems: list[str]
    attempt: int
    created_at: datetime
    image_url: str | None
    is_current: bool
    model: str


class VersionList(Out):
    versions: list[VersionOut]


class LedgerEntry(Out):
    id: int
    delta: int
    kind: str
    reason: str
    project_id: str | None
    balance_after: int
    created_at: datetime


class CreditHoldOut(Out):
    """One open hold. `history` is "unavailable" for holds made before hold events were recorded:
    their amounts are current values, not a reconstructed past."""

    id: str
    purpose: str
    label: str
    project_id: str | None
    project_title: str | None
    held: int
    charged: int
    remaining: int
    opened_at: datetime
    history: Literal["recorded", "unavailable"]


class CreditActivityOut(Out):
    """One credit movement. Charges are ledger rows (the only events that change the balance with
    spending); a charge made from a hold also lowers the held amount, so it does not change what is
    available a second time. Settlements are never listed separately."""

    at: datetime
    type: Literal["grant", "purchase", "charge", "refund", "adjust", "hold", "release"]
    reason: str
    project_id: str | None
    project_title: str | None
    balance_change: int
    held_change: int
    available_change: int
    balance_after: int | None


class CreditsOut(Out):
    plan: str
    balance: int
    reserved: int
    available: int
    subscription_status: str | None
    history: list[LedgerEntry]
    holds: list[CreditHoldOut]
    activity: list[CreditActivityOut]
    # earliest recorded hold event for this account; holds and releases before it were not recorded
    hold_history_since: datetime | None
    holds_without_history: int
    prices: dict[str, int]
    plans: dict[str, int]
    billing_enabled: bool


class UrlOut(Out):
    url: str


class MeOut(Out):
    id: str
    email: str | None
    plan: str
    balance: int
    available: int
    reserved: int  # held for work in progress (balance - available)


class TokenOut(Out):
    token: str
    expires_in: int


class HealthOut(Out):
    status: str
    checks: dict[str, bool]
    release: str
