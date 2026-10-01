import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Conversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One person's chat with the AI Assistant. Only its owner ever reads it."""

    __tablename__ = "ai_conversations"

    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200))
    # When the last question was asked: the history is listed by it.
    last_message_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_ai_conversations_user_recent", "user_id", "last_message_at"),)


class ConversationMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A question and the answer it got, exactly as shown at the time."""

    __tablename__ = "ai_conversation_messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ai_conversations.id", ondelete="CASCADE"))
    question: Mapped[str] = mapped_column(Text)
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # The complete AnswerResponse (answer or no-answer), with its sources.
    answer: Mapped[dict[str, Any]] = mapped_column(JSONB)

    __table_args__ = (Index("ix_ai_conversation_messages_conversation", "conversation_id", "created_at"),)
