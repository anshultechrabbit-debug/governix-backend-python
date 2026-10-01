"""AI provider usage, for the dashboard's usage and credit view

Revision ID: b2d6f8a41c73
Revises: a7c3e5f19b42
Create Date: 2026-10-01 13:00:00.000000

* ai_usage: one row per call to the AI provider (service, model, tokens), and
  calls it refused (error code, e.g. insufficient_quota).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2d6f8a41c73"
down_revision: str | Sequence[str] | None = "a7c3e5f19b42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_usage",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("service", sa.String(40), nullable=False),
        sa.Column("model", sa.String(120), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(80)),
    )
    op.create_index("ix_ai_usage_created", "ai_usage", ["created_at"])


def downgrade() -> None:
    op.drop_table("ai_usage")
