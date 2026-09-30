import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Stage(StrEnum):
    UPLOAD = "upload"
    VALIDATION = "validation"
    EXTRACTION = "extraction"
    OCR = "ocr"
    STRUCTURE = "structure"
    ANALYSIS = "analysis"
    CONFIRMATION = "confirmation"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    INDEXING = "indexing"
    VERIFICATION = "verification"


STAGE_ORDER = list(Stage)


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"
    WAITING = "waiting"  # blocked on a person (confirmation)


class IngestionStage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Per-document progress. Units are real counts (pages, chunks), never estimates."""

    __tablename__ = "ingestion_stages"

    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    stage: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default=StageStatus.PENDING)
    done_units: Mapped[int] = mapped_column(default=0)
    total_units: Mapped[int | None]
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        UniqueConstraint("document_id", "stage", name="uq_ingestion_stages_document_id_stage"),
    )


class Decision(StrEnum):
    NEW_POLICY = "NEW_POLICY"
    EXISTING_POLICY_NEW_VERSION = "EXISTING_POLICY_NEW_VERSION"
    EXISTING_POLICY_AMENDMENT = "EXISTING_POLICY_AMENDMENT"
    EXACT_DUPLICATE = "EXACT_DUPLICATE"
    CONTENT_DUPLICATE = "CONTENT_DUPLICATE"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    POSSIBLE_MATCH_REQUIRES_REVIEW = "POSSIBLE_MATCH_REQUIRES_REVIEW"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class DocumentAnalysis(TimestampMixin, Base):
    """What the system detected and suggests for an upload. Suggestions only, until reviewed."""

    __tablename__ = "document_analyses"

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    organization_id: Mapped[uuid.UUID]
    detected: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    suggested_name: Mapped[str | None] = mapped_column(String(500))
    name_confidence: Mapped[float] = mapped_column(default=0.0)
    suggested_category_id: Mapped[uuid.UUID | None]
    category_confidence: Mapped[float] = mapped_column(default=0.0)
    category_ranking: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    decision: Mapped[str] = mapped_column(String(40))
    confidence: Mapped[float] = mapped_column(default=0.0)
    matched_policy_id: Mapped[uuid.UUID | None]
    matched_version_id: Mapped[uuid.UUID | None]
    duplicate_of_document_id: Mapped[uuid.UUID | None]
    # [{signal, matched, weight, detail}] explaining the decision.
    signals: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    candidates: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    amendment_targets: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    conflict: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    missing_fields: Mapped[list[str]] = mapped_column(JSONB, default=list)
    review_status: Mapped[str] = mapped_column(String(20), default=ReviewStatus.PENDING)
    reviewed_by_id: Mapped[uuid.UUID | None]
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
