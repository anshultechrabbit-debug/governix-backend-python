"""extensions and queue jobs

Revision ID: d7146a00302d
Revises:
Create Date: 2026-09-28 16:47:05.296701
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d7146a00302d"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# vector: embeddings (Phase 7). pg_trgm + unaccent: fuzzy normalised-title
# matching for policy identification (Phase 5).
EXTENSIONS = ("vector", "pg_trgm", "unaccent")


def upgrade() -> None:
    for extension in EXTENSIONS:
        op.execute(f"CREATE EXTENSION IF NOT EXISTS {extension}")

    op.create_table(
        "queue_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_name", sa.String(length=200), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("priority", sa.SmallInteger(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column("run_after", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(length=200), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')", name=op.f("ck_queue_jobs_status")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_queue_jobs")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_queue_jobs_idempotency_key")),
    )
    op.create_index(
        "ix_queue_jobs_claimable",
        "queue_jobs",
        [sa.literal_column("priority DESC"), "run_after"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "ix_queue_jobs_running_locked_at",
        "queue_jobs",
        ["locked_at"],
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("ix_queue_jobs_running_locked_at", table_name="queue_jobs")
    op.drop_index("ix_queue_jobs_claimable", table_name="queue_jobs")
    op.drop_table("queue_jobs")
    # Extensions are intentionally left installed: they are database-wide and
    # may pre-date this migration (vector was already installed locally).
