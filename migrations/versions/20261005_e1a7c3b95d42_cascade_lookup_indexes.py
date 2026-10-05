"""Indexes for the lookups ON DELETE CASCADE makes

Revision ID: e1a7c3b95d42
Revises: c4e8a1d27f90
Create Date: 2026-10-05 14:00:00.000000

Deleting a row runs, for every foreign key that points at it, a lookup of the rows
referencing it. Two of those columns had no index, so each lookup scanned the whole
table:

* document_sections.parent_id: deleting a document deletes its sections, and each
  deleted section looked for its children by scanning every section of every
  document (a 2,500-page upload has ~7,000 sections: minutes per delete);
* chunks.policy_id: deleting a policy scanned the whole chunk table;
* the others: deleting an organization, a conversation's ticket message or a bulk
  upload, the same way, on tables that grow without bound.

Created CONCURRENTLY so that building them takes no write lock; re-runnable.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e1a7c3b95d42"
down_revision: str | Sequence[str] | None = "c4e8a1d27f90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEXES = {
    "ix_document_sections_parent": "document_sections (parent_id)",
    "ix_chunks_policy": "chunks (policy_id)",
    "ix_ai_conversations_organization": "ai_conversations (organization_id)",
    "ix_notifications_organization": "notifications (organization_id)",
    "ix_ticket_attachments_message": "ticket_attachments (message_id)",
    "ix_upload_batch_items_batch": "upload_batch_items (batch_id)",
}


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name, target in INDEXES.items():
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {target}")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name in INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
