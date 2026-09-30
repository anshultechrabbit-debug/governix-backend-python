"""keep planner statistics fresh on the small tables every search joins

Revision ID: c4e9a2d71b05
Revises: 8b3d61f0c2a7
Create Date: 2026-09-29 19:30:00.000000

Every retrieval lane joins chunks to documents, policy_versions and policies.
Those tables hold few rows, so they never reach autovacuum's default analyze
threshold (50 changed rows + 10%). Without statistics the planner estimated
one row for each, chose a nested loop over the whole chunk table and ignored
the full-text index: 0.5-0.8 s per lane instead of 10-40 ms.

Autovacuum now re-analyzes them after any change (it wakes every
autovacuum_naptime, 1 minute by default), and they are analyzed once here.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c4e9a2d71b05"
down_revision: str | Sequence[str] | None = "8b3d61f0c2a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("documents", "policies", "policy_versions", "categories", "organizations", "branches", "departments")


def upgrade() -> None:
    for table in TABLES:
        op.execute(
            f"ALTER TABLE {table} SET (autovacuum_analyze_threshold = 1, autovacuum_analyze_scale_factor = 0.0)"
        )
    op.execute(f"ANALYZE {', '.join(TABLES)}")


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} RESET (autovacuum_analyze_threshold, autovacuum_analyze_scale_factor)")
