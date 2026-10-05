"""Shared tokens-per-minute budget for AI provider calls

Revision ID: c4e8a1d27f90
Revises: b2d6f8a41c73
Create Date: 2026-10-05 12:00:00.000000

* rate_buckets: one token bucket per provider model, shared by every worker so
  embedding batches stay under the account's tokens-per-minute limit.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4e8a1d27f90"
down_revision: str | Sequence[str] | None = "b2d6f8a41c73"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rate_buckets",
        sa.Column("key", sa.String(120), primary_key=True),
        sa.Column("tokens", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("rate_buckets")
