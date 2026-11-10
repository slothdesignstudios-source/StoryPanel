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
    qa_status: Mapped[str] = mapped_column(String(15), default="pending")  # pass|fail|blocked
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
    status: Mapped[str] = mapped_column(String(15), default="started")  # started|succeeded|failed|ambiguous
    result: Mapped[JSON | None] = mapped_column(JSONB)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    images: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 5), default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    prompt_version: Mapped[str | None] = mapped_column(String(20))
    error_code: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


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
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
