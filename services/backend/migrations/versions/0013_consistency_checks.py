"""Reference-sheet review results, script consistency status, recorded approval overrides.

All columns are nullable and additive: existing rows keep NULL (not checked).

Revision ID: 0013
Revises: 0012
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: Union[str, Sequence[str], None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("characters", sa.Column("sheet_check", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("scripts", sa.Column("consistency", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("approvals", sa.Column("overrides", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("approvals", "overrides")
    op.drop_column("scripts", "consistency")
    op.drop_column("characters", "sheet_check")
