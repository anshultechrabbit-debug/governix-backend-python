"""Which users may read which policies.

A User (role department_user) reads, searches and asks the AI about a policy
only while it is assigned to them, and only if the policy's branch scope also
reaches them. Managers and organisation admins see by scope alone. Removing an
assignment keeps the row (removed_at), so the history is never lost.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index
from sqlalchemy import text as sql
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PolicyAssignment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "policy_assignments"

    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"))
    policy_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("policies.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    assigned_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removed_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    __table_args__ = (
        # At most one live assignment of a policy to a user.
        Index(
            "uq_policy_assignments_active", "policy_id", "user_id", unique=True,
            postgresql_where=sql("removed_at IS NULL"),
        ),
        # Loaded on every request of a User to build their access.
        Index("ix_policy_assignments_user_active", "user_id", postgresql_where=sql("removed_at IS NULL")),
    )
