"""HTTP request/response contracts. The frontend's TypeScript types are generated from the
OpenAPI document these produce (packages/api-types), and CI fails on drift."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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


class Answers(In):
    answers: dict[Literal["audience", "length", "style"], str] = Field(default_factory=dict)


class ReviseCharacter(In):
    instruction: str = Field(min_length=3, max_length=400)


class ApproveCast(In):
    cast_version: int = Field(ge=1)


class ApproveScript(In):
    script_version: int = Field(ge=1)


class PatchScript(In):
    base_version: int = Field(ge=1)
    script: ComicScript


class PatchBible(In):
    cast_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    logline: str | None = Field(default=None, max_length=600)
    setting: str | None = Field(default=None, max_length=600)
    tone: str | None = Field(default=None, max_length=200)
    themes: list[str] | None = Field(default=None, max_length=8)
    world_rules: list[str] | None = Field(default=None, max_length=8)


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


class MetaOut(Out):
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


class CharacterOut(Out):
    slug: str
    name: str
    spec: CharacterSpec
    sheet_status: str
    sheet_version: int
    revising: bool
    images: dict[str, str]


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


class CreditsOut(Out):
    plan: str
    balance: int
    reserved: int
    available: int
    subscription_status: str | None
    history: list[LedgerEntry]
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


class TokenOut(Out):
    token: str
    expires_in: int


class HealthOut(Out):
    status: str
    checks: dict[str, bool]
    release: str
