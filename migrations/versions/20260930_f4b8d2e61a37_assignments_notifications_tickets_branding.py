"""policy assignments, notifications, tickets, organisation branding, version summaries

Revision ID: f4b8d2e61a37
Revises: e3a91c07b5d2
Create Date: 2026-09-30 18:00:00.000000

* policy_assignments: Users read only the policies assigned to them. Removing an
  assignment sets removed_at; the row stays as history.
* users.ck_users_role_scope: a User needs a branch; a department is optional.
* notifications: per-person inbox, deduplicated per event.
* tickets / ticket_messages / ticket_attachments: complaints and support
  tickets routed by level (branch, organisation, platform).
* organizations: logo, colours and contact details.
* policy_versions.ai_summary*: AI summary of the version's own text.
* policy_versions.version_label_auto: the label is the registration number
  (none was given), so reordering may renumber it. Existing labels are kept as
  they are (false).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f4b8d2e61a37"
down_revision: str | Sequence[str] | None = "e3a91c07b5d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE_SCOPE_BEFORE = (
    "(role = 'master_admin' AND organization_id IS NULL AND branch_id IS NULL AND department_id IS NULL)"
    " OR (role = 'org_admin' AND organization_id IS NOT NULL AND branch_id IS NULL AND department_id IS NULL)"
    " OR (role = 'branch_manager' AND organization_id IS NOT NULL AND branch_id IS NOT NULL AND department_id IS NULL)"
    " OR (role = 'department_user' AND organization_id IS NOT NULL AND branch_id IS NOT NULL AND department_id IS NOT NULL)"
)
ROLE_SCOPE_AFTER = (
    "(role = 'master_admin' AND organization_id IS NULL AND branch_id IS NULL AND department_id IS NULL)"
    " OR (role = 'org_admin' AND organization_id IS NOT NULL AND branch_id IS NULL AND department_id IS NULL)"
    " OR (role = 'branch_manager' AND organization_id IS NOT NULL AND branch_id IS NOT NULL AND department_id IS NULL)"
    " OR (role = 'department_user' AND organization_id IS NOT NULL AND branch_id IS NOT NULL)"
)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def _org(nullable: bool = False, ondelete: str = "RESTRICT") -> sa.Column:
    return sa.Column(
        "organization_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete=ondelete), nullable=nullable
    )


def upgrade() -> None:
    op.create_table(
        "policy_assignments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _org(),
        sa.Column("policy_id", sa.Uuid(), sa.ForeignKey("policies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("assigned_by_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.Column("removed_by_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        *_timestamps(),
    )
    op.create_index(
        "uq_policy_assignments_active", "policy_assignments", ["policy_id", "user_id"], unique=True,
        postgresql_where=sa.text("removed_at IS NULL"),
    )
    op.create_index(
        "ix_policy_assignments_user_active", "policy_assignments", ["user_id"],
        postgresql_where=sa.text("removed_at IS NULL"),
    )

    op.drop_constraint(op.f("ck_users_role_scope"), "users", type_="check")
    op.create_check_constraint(op.f("ck_users_role_scope"), "users", ROLE_SCOPE_AFTER)

    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _org(nullable=True, ondelete="CASCADE"),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("type", sa.String(40), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("body", sa.Text()),
        sa.Column("link", sa.String(500)),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("dedupe_key", sa.String(200)),
        sa.Column("read_at", sa.DateTime(timezone=True)),
        *_timestamps(),
    )
    op.create_index("ix_notifications_user_created", "notifications", ["user_id", "created_at"])
    op.create_index(
        "ix_notifications_user_unread", "notifications", ["user_id"], postgresql_where=sa.text("read_at IS NULL")
    )
    op.create_index(
        "uq_notifications_dedupe", "notifications", ["user_id", "dedupe_key"], unique=True,
        postgresql_where=sa.text("dedupe_key IS NOT NULL"),
    )

    op.create_table(
        "tickets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _org(),
        sa.Column("branch_id", sa.Uuid(), sa.ForeignKey("branches.id", ondelete="RESTRICT")),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("level", sa.String(20), nullable=False),
        sa.Column("category", sa.String(100)),
        sa.Column("subject", sa.String(300), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("priority", sa.String(10), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("assigned_to_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        *_timestamps(),
    )
    op.create_index("ix_tickets_queue", "tickets", ["organization_id", "level", "status"])
    op.create_index("ix_tickets_branch_queue", "tickets", ["branch_id", "level", "status"])
    op.create_index("ix_tickets_created_by", "tickets", ["created_by_id", "created_at"])
    op.create_table(
        "ticket_messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("ticket_id", sa.Uuid(), sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
        _org(),
        sa.Column("author_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("body", sa.Text()),
        sa.Column("status_change", sa.String(30)),
        *_timestamps(),
    )
    op.create_index("ix_ticket_messages_ticket", "ticket_messages", ["ticket_id", "created_at"])
    op.create_table(
        "ticket_attachments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("ticket_id", sa.Uuid(), sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
        _org(),
        sa.Column("message_id", sa.Uuid(), sa.ForeignKey("ticket_messages.id", ondelete="CASCADE")),
        sa.Column("filename", sa.String(300), nullable=False),
        sa.Column("content_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(500), nullable=False),
        sa.Column("uploaded_by_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        *_timestamps(),
    )
    op.create_index("ix_ticket_attachments_ticket", "ticket_attachments", ["ticket_id"])

    for column in (
        sa.Column("logo_key", sa.String(500)),
        sa.Column("logo_content_type", sa.String(100)),
        sa.Column("primary_color", sa.String(7)),
        sa.Column("secondary_color", sa.String(7)),
        sa.Column("contact_email", sa.String(320)),
        sa.Column("contact_phone", sa.String(50)),
        sa.Column("website", sa.String(300)),
        sa.Column("address", sa.Text()),
    ):
        op.add_column("organizations", column)

    op.add_column(
        "policy_versions",
        sa.Column("version_label_auto", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("policy_versions", sa.Column("ai_summary", postgresql.JSONB()))
    op.add_column("policy_versions", sa.Column("ai_summary_model", sa.String(100)))
    op.add_column("policy_versions", sa.Column("ai_summary_generated_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    for column in ("ai_summary_generated_at", "ai_summary_model", "ai_summary", "version_label_auto"):
        op.drop_column("policy_versions", column)
    for column in (
        "address", "website", "contact_phone", "contact_email", "secondary_color", "primary_color",
        "logo_content_type", "logo_key",
    ):
        op.drop_column("organizations", column)
    op.drop_index("ix_ticket_attachments_ticket", table_name="ticket_attachments")
    op.drop_table("ticket_attachments")
    op.drop_index("ix_ticket_messages_ticket", table_name="ticket_messages")
    op.drop_table("ticket_messages")
    op.drop_index("ix_tickets_created_by", table_name="tickets")
    op.drop_index("ix_tickets_branch_queue", table_name="tickets")
    op.drop_index("ix_tickets_queue", table_name="tickets")
    op.drop_table("tickets")
    op.drop_index("uq_notifications_dedupe", table_name="notifications")
    op.drop_index("ix_notifications_user_unread", table_name="notifications")
    op.drop_index("ix_notifications_user_created", table_name="notifications")
    op.drop_table("notifications")
    # Users created without a department cannot satisfy the old rule. It is restored
    # NOT VALID: enforced for new and changed rows, without failing on those.
    op.drop_constraint(op.f("ck_users_role_scope"), "users", type_="check")
    op.execute(f"ALTER TABLE users ADD CONSTRAINT ck_users_role_scope CHECK ({ROLE_SCOPE_BEFORE}) NOT VALID")
    op.drop_index("ix_policy_assignments_user_active", table_name="policy_assignments")
    op.drop_index("uq_policy_assignments_active", table_name="policy_assignments")
    op.drop_table("policy_assignments")
