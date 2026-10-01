"""AI Assistant chat history

Revision ID: a7c3e5f19b42
Revises: f4b8d2e61a37
Create Date: 2026-10-01 12:00:00.000000

* ai_conversations: one person's chat with the AI Assistant, listed by its last question.
* ai_conversation_messages: each question with the complete answer it got.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7c3e5f19b42"
down_revision: str | Sequence[str] | None = "f4b8d2e61a37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "ai_conversations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        *_timestamps(),
    )
    op.create_index("ix_ai_conversations_user_recent", "ai_conversations", ["user_id", "last_message_at"])
    op.create_table(
        "ai_conversation_messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("conversation_id", sa.Uuid(), sa.ForeignKey("ai_conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("options", postgresql.JSONB(), nullable=False),
        sa.Column("answer", postgresql.JSONB(), nullable=False),
        *_timestamps(),
    )
    op.create_index(
        "ix_ai_conversation_messages_conversation", "ai_conversation_messages", ["conversation_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("ai_conversation_messages")
    op.drop_table("ai_conversations")
