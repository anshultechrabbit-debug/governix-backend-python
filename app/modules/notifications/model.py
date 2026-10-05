import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy import text as sql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class NotificationType(StrEnum):
    NEW_POLICY = "NEW_POLICY"
    POLICY_UPDATED = "POLICY_UPDATED"
    POLICY_VERSION_CHANGED = "POLICY_VERSION_CHANGED"
    POLICY_ASSIGNED = "POLICY_ASSIGNED"
    POLICY_REMOVED = "POLICY_REMOVED"
    POLICY_EXPIRING = "POLICY_EXPIRING"
    POLICY_EXPIRED = "POLICY_EXPIRED"
    TICKET_UPDATED = "TICKET_UPDATED"
    COMPLAINT_RESPONSE = "COMPLAINT_RESPONSE"
    SUPPORT_RESPONSE = "SUPPORT_RESPONSE"


class Notification(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One message to one person. Written in the same transaction as the event it reports."""

    __tablename__ = "notifications"

    organization_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str | None] = mapped_column(Text)
    # In-app route to open, e.g. "/policies/<id>".
    link: Mapped[str | None] = mapped_column(String(500))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # Identifies the event, so a retried job never notifies twice (e.g. "expiring:<version>:<user>").
    dedupe_key: Mapped[str | None] = mapped_column(String(200))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_notifications_user_created", "user_id", "created_at"),
        Index("ix_notifications_organization", "organization_id"),  # ON DELETE CASCADE lookup
        Index("ix_notifications_user_unread", "user_id", postgresql_where=sql("read_at IS NULL")),
        Index("uq_notifications_dedupe", "user_id", "dedupe_key", unique=True,
              postgresql_where=sql("dedupe_key IS NOT NULL")),
    )
