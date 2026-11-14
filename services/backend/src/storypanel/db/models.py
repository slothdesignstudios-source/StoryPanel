"""Relational model. JSONB holds validated specs; pgvector holds identity embeddings."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)  # fmt: skip
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EMBEDDING_DIM = 1024
JSON = dict[str, Any]


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Base(DeclarativeBase):
    pass


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


def pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)


def fk(target: str, *, nullable: bool = False, ondelete: str = "CASCADE") -> Mapped[Any]:
    return mapped_column(
        UUID(as_uuid=True), ForeignKey(target, ondelete=ondelete), nullable=nullable, index=True
    )


class User(Base, Timestamps):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = pk()
    auth_subject: Mapped[str] = mapped_column(String(200), unique=True)
    email: Mapped[str | None] = mapped_column(String(320))
    plan: Mapped[str] = mapped_column(String(20), default="free")
    credit_balance: Mapped[int] = mapped_column(Integer, default=0)
    credit_reserved: Mapped[int] = mapped_column(Integer, default=0)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    subscription_id: Mapped[str | None] = mapped_column(String(100))
    subscription_status: Mapped[str | None] = mapped_column(String(40))
    subscription_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_granted_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("credit_reserved >= 0", name="ck_users_reserved_nonneg"),
        CheckConstraint("credit_balance >= credit_reserved", name="ck_users_no_overspend"),
    )


class Project(Base, Timestamps):
    __tablename__ = "projects"
    id: Mapped[uuid.UUID] = pk()
    owner_id: Mapped[uuid.UUID] = fk("users.id")
    title: Mapped[str] = mapped_column(String(200), default="Untitled comic")
    status: Mapped[str] = mapped_column(String(40), default="queued", index=True)
    prompt: Mapped[str] = mapped_column(Text)
    brief: Mapped[JSON | None] = mapped_column(JSONB)
    questions: Mapped[list[Any] | None] = mapped_column(JSONB)
    answers: Mapped[JSON | None] = mapped_column(JSONB)
    format: Mapped[str] = mapped_column(String(20), default="western_page")
    style_preset: Mapped[str | None] = mapped_column(String(60))
    style_version: Mapped[str | None] = mapped_column(String(20))
    prompt_version: Mapped[str | None] = mapped_column(String(20))
    pages_requested: Mapped[int] = mapped_column(Integer, default=4)
    workflow_id: Mapped[str | None] = mapped_column(String(200))
    event_seq: Mapped[int] = mapped_column(BigInteger, default=0)
    cast_version: Mapped[int] = mapped_column(Integer, default=0)
    script_version: Mapped[int] = mapped_column(Integer, default=0)
    edit_version: Mapped[int] = mapped_column(Integer, default=0)  # bumps on any editor change
    failure: Mapped[JSON | None] = mapped_column(JSONB)
    resume_from: Mapped[str | None] = mapped_column(String(40))
    first_page_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    script_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The image provider and model chosen at creation; every image of the comic (sheets,
    # panels, repairs, edits) uses them. NULL on comics created before 0011 (drawn with fal).
    image_provider: Mapped[str | None] = mapped_column(String(20))
    image_model: Mapped[str | None] = mapped_column(String(120))
    __table_args__ = (Index("ix_projects_owner_created", "owner_id", "created_at"),)


class StoryBibleRow(Base):
    __tablename__ = "story_bibles"
    id: Mapped[uuid.UUID] = pk()
    project_id: Mapped[uuid.UUID] = fk("projects.id")
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[JSON] = mapped_column(JSONB)
    source: Mapped[str] = mapped_column(String(10), default="ai")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("project_id", "version"),)


class Character(Base, Timestamps):
    __tablename__ = "characters"
    id: Mapped[uuid.UUID] = pk()
    project_id: Mapped[uuid.UUID] = fk("projects.id")
    library_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # v2 extension point
    slug: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(120))
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    spec: Mapped[JSON] = mapped_column(JSONB)
    sheet_status: Mapped[str] = mapped_column(String(20), default="pending")
    sheet_version: Mapped[int] = mapped_column(Integer, default=0)
    lora_id: Mapped[str | None] = mapped_column(String(120))  # v2 extension point
    pending_revision: Mapped[JSON | None] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("project_id", "slug"),)


class CharacterRef(Base):
    """One stored view (portrait, turnaround crop, expression crop) with its embedding."""

    __tablename__ = "character_refs"
    id: Mapped[uuid.UUID] = pk()
    character_id: Mapped[uuid.UUID] = fk("characters.id")
    sheet_version: Mapped[int] = mapped_column(Integer)
    view: Mapped[str] = mapped_column(String(30))
    asset_id: Mapped[uuid.UUID] = fk("assets.id", ondelete="RESTRICT")
    embedding: Mapped[Any | None] = mapped_column(Vector(EMBEDDING_DIM))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("character_id", "sheet_version", "view"),
        Index("ix_character_refs_embedding", "embedding", postgresql_using="hnsw",
              postgresql_ops={"embedding": "vector_cosine_ops"}),
    )  # fmt: skip


class Location(Base, Timestamps):
    __tablename__ = "locations"
    id: Mapped[uuid.UUID] = pk()
    project_id: Mapped[uuid.UUID] = fk("projects.id")
    slug: Mapped[str] = mapped_column(String(40))
    spec: Mapped[JSON] = mapped_column(JSONB)
    reference_asset_id: Mapped[uuid.UUID | None] = fk("assets.id", nullable=True, ondelete="SET NULL")
    __table_args__ = (UniqueConstraint("project_id", "slug"),)


class Script(Base):
    __tablename__ = "scripts"
    id: Mapped[uuid.UUID] = pk()
    project_id: Mapped[uuid.UUID] = fk("projects.id")
    version: Mapped[int] = mapped_column(Integer)
    beats: Mapped[JSON] = mapped_column(JSONB)
    content: Mapped[JSON] = mapped_column(JSONB)
    issues: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    source: Mapped[str] = mapped_column(String(10), default="ai")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("project_id", "version"),)


class Approval(Base):
    """A human approval of one exact content version. Edits invalidate it."""

    __tablename__ = "approvals"
    id: Mapped[uuid.UUID] = pk()
    project_id: Mapped[uuid.UUID] = fk("projects.id")
    kind: Mapped[str] = mapped_column(String(10))  # cast | script
    content_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(15), default="approved")
    approved_by: Mapped[uuid.UUID] = fk("users.id")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("project_id", "kind", "content_version"),)


class Page(Base, Timestamps):
    __tablename__ = "pages"
    id: Mapped[uuid.UUID] = pk()
    project_id: Mapped[uuid.UUID] = fk("projects.id")
    index: Mapped[int] = mapped_column(Integer)
    layout_template_id: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    preview_asset_id: Mapped[uuid.UUID | None] = fk("assets.id", nullable=True, ondelete="SET NULL")
    lettered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("project_id", "index"),)


class Panel(Base, Timestamps):
    __tablename__ = "panels"
    id: Mapped[uuid.UUID] = pk()
    project_id: Mapped[uuid.UUID] = fk("projects.id")
    page_id: Mapped[uuid.UUID] = fk("pages.id")
    index: Mapped[int] = mapped_column(Integer)  # slot in the page layout / reading order
    spec: Mapped[JSON] = mapped_column(JSONB)
    spec_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Bumped whenever the user picks/restores a version or requests new work, so a late
    # job result can tell that it is no longer the newest intent for this panel.
    intent_seq: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (UniqueConstraint("page_id", "index", deferrable=True, initially="DEFERRED"),)


class PanelVersion(Base):
    __tablename__ = "panel_versions"
    id: Mapped[uuid.UUID] = pk()
    panel_id: Mapped[uuid.UUID] = fk("panels.id")
    image_asset_id: Mapped[uuid.UUID] = fk("assets.id", ondelete="RESTRICT")
    upscaled_asset_id: Mapped[uuid.UUID | None] = fk("assets.id", nullable=True, ondelete="SET NULL")
    kind: Mapped[str] = mapped_column(String(15))  # generate | regenerate | edit
    instruction: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(120))
    prompt_version: Mapped[str] = mapped_column(String(20))
    prompt_sha256: Mapped[str] = mapped_column(String(64))
    seed: Mapped[int | None] = mapped_column(BigInteger)
    references: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    spec_version: Mapped[int] = mapped_column(Integer)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    qa_status: Mapped[str] = mapped_column(String(15), default="pending")  # pending|pass|fail|review|blocked
    qa_scores: Mapped[JSON | None] = mapped_column(JSONB)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 5), default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    provider_request_id: Mapped[str | None] = mapped_column(String(200))
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Balloon(Base, Timestamps):
    __tablename__ = "balloons"
    id: Mapped[uuid.UUID] = pk()
    project_id: Mapped[uuid.UUID] = fk("projects.id")
    panel_id: Mapped[uuid.UUID] = fk("panels.id")
    order_index: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(10))
    speaker_id: Mapped[str | None] = mapped_column(String(40))
    text: Mapped[str] = mapped_column(Text)
    geometry: Mapped[JSON] = mapped_column(JSONB)
    style: Mapped[JSON] = mapped_column(JSONB, default=dict)
    flags: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    user_edited: Mapped[bool] = mapped_column(Boolean, default=False)
    # sha256 of the text as it last passed the full moderation classifier (the script's, for
    # generated balloons). Export re-moderates every balloon whose text no longer matches.
    moderated_sha: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)


class Asset(Base):
    __tablename__ = "assets"
    id: Mapped[uuid.UUID] = pk()
    owner_id: Mapped[uuid.UUID] = fk("users.id")
    project_id: Mapped[uuid.UUID | None] = fk("projects.id", nullable=True)
    kind: Mapped[str] = mapped_column(String(30))
    storage_key: Mapped[str] = mapped_column(String(400), unique=True)
    content_type: Mapped[str] = mapped_column(String(60))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    quarantined: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Job(Base, Timestamps):
    """Mirror of workflow progress for the UI; Temporal remains the source of truth."""

    __tablename__ = "jobs"
    id: Mapped[uuid.UUID] = pk()
    project_id: Mapped[uuid.UUID] = fk("projects.id")
    workflow_id: Mapped[str] = mapped_column(String(200), index=True)
    stage: Mapped[str] = mapped_column(String(40))
    target_id: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20), default="running")
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 5), default=0)
    timings: Mapped[JSON] = mapped_column(JSONB, default=dict)
    error: Mapped[JSON | None] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("workflow_id", "stage", "target_id"),)


class CreditReservation(Base, Timestamps):
    __tablename__ = "credit_reservations"
    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = fk("users.id")
    project_id: Mapped[uuid.UUID | None] = fk("projects.id", nullable=True, ondelete="SET NULL")
    purpose: Mapped[str] = mapped_column(String(40))
    amount: Mapped[int] = mapped_column(Integer)
    settled: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(10), default="open")  # open|closed
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    __table_args__ = (
        CheckConstraint("amount > 0 and settled >= 0 and settled <= amount", name="ck_resv_bounds"),
    )


class CreditReservationEvent(Base):
    """Append-only history of credit holds (enforced by trigger in migration 0012).

    hold: credits set aside for upcoming work; settle: part of a hold turned into a charge (the
    charge itself is the credit_ledger row, so settle events are never shown as money moving);
    release: the unused rest of a hold handed back. Holds made before this table existed have no
    events: their history is unavailable, not reconstructed."""

    __tablename__ = "credit_reservation_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    reservation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID] = fk("users.id")
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    purpose: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(10))  # hold | settle | release
    amount: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_resv_event_amount"),
        Index("ix_resv_events_user_created", "user_id", "created_at"),
    )


class CreditLedger(Base):
    """Append-only (enforced by trigger in the migration)."""

    __tablename__ = "credit_ledger"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = fk("users.id")
    delta: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20))  # grant|purchase|charge|refund|adjust
    reason: Mapped[str] = mapped_column(String(200))
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    job_ref: Mapped[str | None] = mapped_column(String(200))
    reservation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    balance_after: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (Index("ix_ledger_user_created", "user_id", "created_at"),)


class ProviderUsage(Base):
    """One row per paid provider call: actual cost, kept apart from customer credits."""

    __tablename__ = "provider_usage"
    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    stage: Mapped[str] = mapped_column(String(40))
    target_id: Mapped[str | None] = mapped_column(String(80))
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(120))
    operation: Mapped[str] = mapped_column(String(30))
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(200))
    # reserved|submitting|started|succeeded|succeeded_unused|failed|released|ambiguous|cancelled
    status: Mapped[str] = mapped_column(String(20), default="reserved", index=True)
    result: Mapped[JSON | None] = mapped_column(JSONB)
    cost_estimate_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=0)
    # what spend ceilings count: the estimate while open/unknown, the actual cost once known
    cost_counted_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=0)
    cost_basis: Mapped[str] = mapped_column(String(20), default="estimated")
    price_ref: Mapped[JSON | None] = mapped_column(JSONB)
    submissions: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    images: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    prompt_version: Mapped[str | None] = mapped_column(String(20))
    error_code: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ProviderAttempt(Base):
    """One provider submission (or synchronous call) beneath a logical operation
    (`provider_usage`). Attempts are never deleted or reopened: a retry after a failure is
    a new attempt, so every potentially billed request stays counted. A database trigger
    keeps identity, request id and settled costs immutable (migration 0003).

    cost_basis says what `cost_counted_usd` is:
      reserved      estimate held before the outcome is known
      unresolved    outcome unknown (ambiguous, cancelled, lost): estimate kept as a potential charge
      calculated    usage x operator price table; NOT a provider-confirmed charge
      provider_confirmed  a charge the provider reported (billing export / API)
      released      definitively never billed (rejected before queueing, never sent)
      assumed_unbilled    failed at the provider and the operator chose not to count failures
    """

    __tablename__ = "provider_attempts"
    __table_args__ = (UniqueConstraint("operation_id", "attempt_no"),)
    id: Mapped[uuid.UUID] = pk()
    operation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("provider_usage.id"), index=True)
    attempt_no: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    stage: Mapped[str] = mapped_column(String(40))
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(120))
    operation: Mapped[str] = mapped_column(String(30))
    provider_request_id: Mapped[str | None] = mapped_column(String(200), index=True)
    # reserved|submitting|started|succeeded|succeeded_unused|failed|released|ambiguous|cancelled|
    # cancel_confirmed|lost
    status: Mapped[str] = mapped_column(String(20), index=True)
    price_ref: Mapped[JSON] = mapped_column(JSONB)
    cost_estimate_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    cost_counted_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    cost_basis: Mapped[str] = mapped_column(String(24), default="reserved")
    cost_calculated_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    cost_confirmed_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    confirmed_source: Mapped[str | None] = mapped_column(String(120))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    images: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(40))
    # Synchronous image calls (OpenAI, Gemini) have no request to fetch again: the delivered
    # image is stored as an asset before the attempt is marked succeeded, so a crash after
    # the provider answered never leads to paying for it twice.
    result_asset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Provider-reported usage breakdown (token counts by kind), when the provider reports it.
    usage_detail: Mapped[JSON | None] = mapped_column(JSONB)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AccountDeletion(Base):
    """Durable, idempotent account-deletion job and tombstone (see services/deletion.py).

    The row outlives the account. While deletion runs it holds the identifiers that
    external cleanup still needs (auth subject, Stripe customer/subscription); each is
    cleared once its step succeeds. Afterwards only hashes and timestamps remain: the
    subject hash blocks tokens issued before the deletion from reopening the account."""

    __tablename__ = "account_deletions"
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    subject_hash: Mapped[str] = mapped_column(String(64), unique=True)
    source: Mapped[str] = mapped_column(String(20))  # user | clerk_webhook | operator
    # requested -> cancelling -> draining -> purging -> billing -> identity -> completed
    status: Mapped[str] = mapped_column(String(20), default="requested", index=True)
    auth_subject: Mapped[str | None] = mapped_column(String(200))
    stripe_customer_id: Mapped[str | None] = mapped_column(String(100))
    subscription_ids: Mapped[list[str] | None] = mapped_column(JSONB)
    steps: Mapped[JSON] = mapped_column(JSONB, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(200))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SignupGrant(Base):
    """Keyed hashes of identities (auth subject, normalised email) that already received
    the welcome grant. Kept after account deletion so deleting and re-creating an account
    does not grant again. Contains no recoverable personal data (HMAC, server-side key)."""

    __tablename__ = "signup_grants"
    identity_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PendingObjectDeletion(Base):
    """A stored object whose deletion has not been confirmed. This row is the durable
    reference to the object once its asset row is gone; maintenance retries it."""

    __tablename__ = "pending_object_deletions"
    storage_key: Mapped[str] = mapped_column(String(400), primary_key=True)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    reason: Mapped[str] = mapped_column(String(40))
    last_error: Mapped[str | None] = mapped_column(String(60))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BillingSubscription(Base):
    """Local mirror of one Stripe subscription, refreshed from Stripe's API (authoritative)
    on every subscription event and by periodic reconciliation. A user may have several."""

    __tablename__ = "billing_subscriptions"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)  # sub_...
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    customer_id: Mapped[str | None] = mapped_column(String(100), index=True)
    status: Mapped[str] = mapped_column(String(30))
    plan: Mapped[str | None] = mapped_column(String(20))
    price_id: Mapped[str | None] = mapped_column(String(100))
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(20))  # api | event
    event_created: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SubscriptionEntitlement(Base):
    """Credits granted for one subscription billing period, keyed by (subscription, period
    end) and never by invoice: several invoices touching one period cannot grant twice, an
    upgrade's proration lines (which end with the period) top the same period up whatever
    order they arrive in, and a billing-anchor reset (a new period with a new end) is a new
    entitlement. period_start is the earliest start seen for the period."""

    __tablename__ = "subscription_entitlements"
    __table_args__ = (UniqueConstraint("subscription_id", "period_end"),)
    id: Mapped[uuid.UUID] = pk()
    subscription_id: Mapped[str] = mapped_column(String(100), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    plan: Mapped[str] = mapped_column(String(20))
    credits_granted: Mapped[int] = mapped_column(Integer)
    invoices: Mapped[list[str]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CreditPurchase(Base):
    """A one-off credit-pack purchase. Refunds are matched to it by payment intent; a
    refund that matches no purchase (e.g. a subscription refund) is never clawed back
    automatically."""

    __tablename__ = "credit_purchases"
    checkout_session_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    payment_intent_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    credits: Mapped[int] = mapped_column(Integer)
    amount_total: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str | None] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SpendCap(Base):
    """A lifetime provider-spend cap for one user, replacing that user's daily ceiling. Used
    for evaluation runs: each run has its own user, so the cap is the run's budget,
    unaffected by midnight resets or other accounts' activity."""

    __tablename__ = "spend_caps"
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    cap_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    reason: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProjectEvent(Base):
    __tablename__ = "project_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[uuid.UUID] = fk("projects.id")
    seq: Mapped[int] = mapped_column(BigInteger)
    type: Mapped[str] = mapped_column(String(60))
    payload: Mapped[JSON] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    __table_args__ = (UniqueConstraint("project_id", "seq"),)


class Export(Base, Timestamps):
    __tablename__ = "exports"
    id: Mapped[uuid.UUID] = pk()
    project_id: Mapped[uuid.UUID] = fk("projects.id")
    owner_id: Mapped[uuid.UUID] = fk("users.id")
    format: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    revision: Mapped[JSON] = mapped_column(JSONB)  # immutable snapshot taken at request time
    revision_sha256: Mapped[str] = mapped_column(String(64))
    pages_done: Mapped[JSON] = mapped_column(JSONB, default=dict)
    asset_id: Mapped[uuid.UUID | None] = fk("assets.id", nullable=True, ondelete="SET NULL")
    error: Mapped[JSON | None] = mapped_column(JSONB)
    # Monotonic run counter, separate from error details: each retry gets its own workflow
    # id and outbox key (export-<id>-a<attempt>), so no retry can collide with an earlier one.
    attempt: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Effective print resolution per panel and any low-resolution warnings (local resampling).
    print_quality: Mapped[JSON | None] = mapped_column(JSONB)


class Outbox(Base):
    """Workflow starts/signals written in the same transaction as the state they follow."""

    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(30))  # start_workflow|signal|cancel
    payload: Mapped[JSON] = mapped_column(JSONB)
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(20))
    event_id: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(80))
    event_created: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("provider", "event_id"),)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(120), primary_key=True)
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    request_sha256: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int | None] = mapped_column(Integer)
    response: Mapped[JSON | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_ref: Mapped[str | None] = mapped_column(String(64), index=True)  # pseudonymous
    action: Mapped[str] = mapped_column(String(60))
    target_type: Mapped[str | None] = mapped_column(String(40))
    target_id: Mapped[str | None] = mapped_column(String(80))
    meta: Mapped[JSON] = mapped_column(JSONB, default=dict)
    request_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class PanelFeedback(Base):
    __tablename__ = "panel_feedback"
    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = fk("users.id")
    panel_version_id: Mapped[uuid.UUID] = fk("panel_versions.id")
    rating: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("user_id", "panel_version_id"),
        CheckConstraint("rating in (-1, 1)", name="ck_feedback_rating"),
    )


APPEND_ONLY_DDL = text(
    """
    create or replace function forbid_mutation() returns trigger as $$
    begin raise exception '% is append-only', tg_table_name; end; $$ language plpgsql;
    """
)
