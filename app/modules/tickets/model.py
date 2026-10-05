"""Complaints and support tickets, routed by level.

    branch        a User raises it; their branch's managers handle it
    organization  a Branch Manager raises it; the organisation admins handle it
    platform      an Organisation Admin raises it; the master admins handle it

A complaint is always branch level (User -> Branch Manager). Every reply and
status change is kept.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TicketKind(StrEnum):
    COMPLAINT = "complaint"
    SUPPORT = "support"


class TicketLevel(StrEnum):
    BRANCH = "branch"
    ORGANIZATION = "organization"
    PLATFORM = "platform"


class TicketStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    WAITING_FOR_RESPONSE = "waiting_for_response"
    RESOLVED = "resolved"
    CLOSED = "closed"


class TicketPriority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class Ticket(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tickets"

    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"))
    branch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("branches.id", ondelete="RESTRICT"))
    kind: Mapped[str] = mapped_column(String(20))
    level: Mapped[str] = mapped_column(String(20))
    category: Mapped[str | None] = mapped_column(String(100))
    subject: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(String(10), default=TicketPriority.MEDIUM)
    status: Mapped[str] = mapped_column(String(30), default=TicketStatus.OPEN)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    assigned_to_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_tickets_queue", "organization_id", "level", "status"),
        Index("ix_tickets_branch_queue", "branch_id", "level", "status"),
        Index("ix_tickets_created_by", "created_by_id", "created_at"),
    )


class TicketMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A reply, or a recorded status change (status_change set, body optional)."""

    __tablename__ = "ticket_messages"

    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"))
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"))
    author_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    body: Mapped[str | None] = mapped_column(Text)
    status_change: Mapped[str | None] = mapped_column(String(30))

    __table_args__ = (Index("ix_ticket_messages_ticket", "ticket_id", "created_at"),)


class TicketAttachment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "ticket_attachments"

    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"))
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"))
    message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ticket_messages.id", ondelete="CASCADE"))
    filename: Mapped[str] = mapped_column(String(300))
    content_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    storage_key: Mapped[str] = mapped_column(String(500))
    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    __table_args__ = (
        Index("ix_ticket_attachments_ticket", "ticket_id"),
        Index("ix_ticket_attachments_message", "message_id"),  # ON DELETE CASCADE lookup
    )
