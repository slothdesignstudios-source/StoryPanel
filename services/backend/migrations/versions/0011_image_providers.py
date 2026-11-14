"""Per-comic image provider and model; durable results and usage detail for synchronous
image calls; export print-quality report.

Revision ID: 0011
Revises: 0010
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: Union[str, Sequence[str], None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing comics keep NULL: they were drawn with fal before this change (history kept).
    op.add_column("projects", sa.Column("image_provider", sa.String(length=20), nullable=True))
    op.add_column("projects", sa.Column("image_model", sa.String(length=120), nullable=True))
    op.add_column("provider_attempts", sa.Column("result_asset_id", sa.UUID(), nullable=True))
    op.add_column("provider_attempts",
                  sa.Column("usage_detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("exports", sa.Column("print_quality", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("exports", "print_quality")
    op.drop_column("provider_attempts", "usage_detail")
    op.drop_column("provider_attempts", "result_asset_id")
    op.drop_column("projects", "image_model")
    op.drop_column("projects", "image_provider")
