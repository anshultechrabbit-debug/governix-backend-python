"""bulk upload batches and where each version's effective date came from

Revision ID: d81f3c6a2e94
Revises: c4e9a2d71b05
Create Date: 2026-09-30 10:00:00.000000

* policy_versions.effective_date_source: "entered" | "detected" | "upload_date" |
  "inferred". The effective date is no longer required when confirming; when it
  is not given, the date the document states is used, else the upload date, and
  versions arranged in a bulk upload are ordered as arranged. Existing versions
  were all confirmed with an entered date.
* upload_batches / upload_batch_groups / upload_batch_items: a bulk upload plan
  (policies and their ordered versions) that the server carries out after
  analysis.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d81f3c6a2e94"
down_revision: str | Sequence[str] | None = "c4e9a2d71b05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.add_column(
        "policy_versions",
        sa.Column("effective_date_source", sa.String(20), nullable=False, server_default="entered"),
    )
    op.create_table(
        "upload_batches",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("branch_id", sa.Uuid(), sa.ForeignKey("branches.id", ondelete="RESTRICT")),
        sa.Column("department_id", sa.Uuid(), sa.ForeignKey("departments.id", ondelete="RESTRICT")),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        *_timestamps(),
    )
    op.create_index("ix_upload_batches_org_created", "upload_batches", ["organization_id", "created_at"])
    op.create_table(
        "upload_batch_groups",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("batch_id", sa.Uuid(), sa.ForeignKey("upload_batches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("policy_id", sa.Uuid(), sa.ForeignKey("policies.id", ondelete="SET NULL")),
        sa.Column("new_policy_name", sa.String(500)),
        sa.Column("category_id", sa.Uuid(), sa.ForeignKey("categories.id", ondelete="RESTRICT")),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("message", sa.Text()),
        *_timestamps(),
    )
    op.create_index("ix_upload_batch_groups_batch", "upload_batch_groups", ["batch_id", "position"])
    op.create_table(
        "upload_batch_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("batch_id", sa.Uuid(), sa.ForeignKey("upload_batches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("group_id", sa.Uuid(), sa.ForeignKey("upload_batch_groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("original_filename", sa.String(500), nullable=False),
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("documents.id", ondelete="SET NULL"), unique=True),
        sa.Column("version_label", sa.String(50)),
        sa.Column("effective_from", sa.Date()),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("message", sa.Text()),
        sa.Column("policy_version_id", sa.Uuid(), sa.ForeignKey("policy_versions.id", ondelete="SET NULL")),
        *_timestamps(),
    )
    op.create_index("ix_upload_batch_items_group", "upload_batch_items", ["group_id", "position"])


def downgrade() -> None:
    op.drop_index("ix_upload_batch_items_group", table_name="upload_batch_items")
    op.drop_table("upload_batch_items")
    op.drop_index("ix_upload_batch_groups_batch", table_name="upload_batch_groups")
    op.drop_table("upload_batch_groups")
    op.drop_index("ix_upload_batches_org_created", table_name="upload_batches")
    op.drop_table("upload_batches")
    op.drop_column("policy_versions", "effective_date_source")
