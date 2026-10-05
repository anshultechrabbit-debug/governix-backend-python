"""Bulk uploads: a plan of policies and their versions, carried out after analysis.

A batch holds groups; a group is one policy (an existing one, or a new one to
create) and its items are that policy's versions in the order the person
arranged them, oldest first. Each item's file goes through the normal pipeline;
once every file of a group has been analysed, the group is confirmed in order
(see uploads.service). Anything the analysis flags is left for manual review.
"""

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import Date, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class BatchStatus(StrEnum):
    UPLOADING = "uploading"  # files are still being sent
    PROCESSING = "processing"  # all files received; analysis / confirmation running
    COMPLETED = "completed"  # every group confirmed
    ATTENTION = "attention"  # finished, but some items need a person


class GroupStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    ATTENTION = "attention"


class ItemStatus(StrEnum):
    AWAITING_FILE = "awaiting_file"
    PROCESSING = "processing"  # in the ingestion pipeline
    CONFIRMED = "confirmed"  # registered as a version
    NEEDS_REVIEW = "needs_review"  # analysis flagged it, or confirming it was refused
    FAILED = "failed"  # the file could not be processed
    CANCELLED = "cancelled"  # the person cancelled it before the file was sent


class UploadBatch(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "upload_batches"

    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"))
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    branch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("branches.id", ondelete="RESTRICT"))
    department_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("departments.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String(20), default=BatchStatus.UPLOADING)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_upload_batches_org_created", "organization_id", "created_at"),)


class UploadBatchGroup(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "upload_batch_groups"

    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"))
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("upload_batches.id", ondelete="CASCADE"))
    position: Mapped[int]
    # The target: an existing policy, or a new one (created with the oldest version).
    policy_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("policies.id", ondelete="SET NULL"))
    new_policy_name: Mapped[str | None] = mapped_column(String(500))
    category_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("categories.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String(20), default=GroupStatus.PENDING)
    message: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_upload_batch_groups_batch", "batch_id", "position"),)


class UploadBatchItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "upload_batch_items"

    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"))
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("upload_batches.id", ondelete="CASCADE"))
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("upload_batch_groups.id", ondelete="CASCADE"))
    position: Mapped[int]  # version order within the group, oldest first
    original_filename: Mapped[str] = mapped_column(String(500))
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), unique=True
    )
    version_label: Mapped[str | None] = mapped_column(String(50))
    effective_from: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default=ItemStatus.AWAITING_FILE)
    message: Mapped[str | None] = mapped_column(Text)
    policy_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("policy_versions.id", ondelete="SET NULL")
    )

    __table_args__ = (
        Index("ix_upload_batch_items_group", "group_id", "position"),
        Index("ix_upload_batch_items_batch", "batch_id"),  # ON DELETE CASCADE lookup
    )
