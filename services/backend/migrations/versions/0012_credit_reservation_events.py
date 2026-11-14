"""Append-only history of credit holds: hold, settle and release events.

Holds created before this migration have no events. Their past is shown as unavailable
rather than reconstructed from the mutable reservation row.

Revision ID: 0012
Revises: 0011
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, Sequence[str], None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "credit_reservation_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("reservation_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=True),
        sa.Column("purpose", sa.String(length=40), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_resv_event_amount"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_resv_events_user_created", "credit_reservation_events", ["user_id", "created_at"])
    op.create_index(op.f("ix_credit_reservation_events_user_id"), "credit_reservation_events", ["user_id"])
    # forbid_mutation() is defined in 0001 for the ledger and audit log.
    op.execute(
        "create trigger credit_reservation_events_append_only before update or delete on "
        "credit_reservation_events for each row execute function forbid_mutation()"
    )


def downgrade() -> None:
    op.execute("drop trigger if exists credit_reservation_events_append_only on credit_reservation_events")
    op.drop_index(op.f("ix_credit_reservation_events_user_id"), table_name="credit_reservation_events")
    op.drop_index("ix_resv_events_user_created", table_name="credit_reservation_events")
    op.drop_table("credit_reservation_events")
