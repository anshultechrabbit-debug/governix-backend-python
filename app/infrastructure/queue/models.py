import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Index, SmallInteger, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class QueueJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Durable job record backing the local (Postgres) queue."""

    __tablename__ = "queue_jobs"

    task_name: Mapped[str] = mapped_column(String(200))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.QUEUED)
    priority: Mapped[int] = mapped_column(SmallInteger, default=0)
    attempts: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int]
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True)
    # Tenant the job works for; FK is added once organizations exist (Phase 2).
    organization_id: Mapped[uuid.UUID | None]
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(200))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')", name="status"
        ),
        # Claim query: next queued job by priority, then due time.
        Index(
            "ix_queue_jobs_claimable",
            text("priority DESC"),
            "run_after",
            postgresql_where=text("status = 'queued'"),
        ),
        # Lease-expiry sweep over running jobs.
        Index(
            "ix_queue_jobs_running_locked_at",
            "locked_at",
            postgresql_where=text("status = 'running'"),
        ),
    )
