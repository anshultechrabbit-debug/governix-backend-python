import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import BigInteger, Computed, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy import text as sql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class DocumentStatus(StrEnum):
    UPLOADED = "uploaded"
    PROCESSING = "processing"  # extraction / OCR / structure / analysis
    AWAITING_CONFIRMATION = "awaiting_confirmation"  # analysis done; a person must confirm
    INDEXING = "indexing"  # chunking / embedding / indexing
    READY = "ready"  # searchable and usable by the AI
    FAILED = "failed"
    REJECTED = "rejected"  # reviewer discarded it (e.g. confirmed duplicate)
    ARCHIVED = "archived"


class Document(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An uploaded source artifact. Its business identity is the Policy it is confirmed into."""

    __tablename__ = "documents"

    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"))
    branch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("branches.id", ondelete="RESTRICT"))
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT")
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("categories.id", ondelete="RESTRICT"))
    policy_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("policies.id", ondelete="SET NULL"))
    policy_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("policy_versions.id", ondelete="SET NULL", use_alter=True)
    )

    # Confirmed display title. Never derived from the filename.
    title: Mapped[str | None] = mapped_column(String(500))
    original_filename: Mapped[str] = mapped_column(String(500))
    storage_key: Mapped[str] = mapped_column(String(500))
    content_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    file_sha256: Mapped[str] = mapped_column(String(64))
    # SHA-256 of normalised extracted text (content duplicates) and a 64-bit
    # SimHash (near-duplicates such as re-scans). Set after extraction.
    content_hash: Mapped[str | None] = mapped_column(String(64))
    simhash: Mapped[int | None] = mapped_column(BigInteger)
    page_count: Mapped[int | None]

    status: Mapped[str] = mapped_column(String(30), default=DocumentStatus.UPLOADED)
    # Incremented on every retry/re-index so pipeline jobs get fresh idempotency keys.
    ingestion_attempt: Mapped[int] = mapped_column(default=1)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    pdf_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    duplicate_of_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
    duplicate_override_reason: Mapped[str | None] = mapped_column(Text)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # ACL + listing filters lead with the tenant.
        Index("ix_documents_scope", "organization_id", "branch_id", "department_id"),
        Index("ix_documents_org_status", "organization_id", "status"),
        Index("ix_documents_org_category", "organization_id", "category_id"),
        Index("ix_documents_org_file_sha256", "organization_id", "file_sha256"),
        Index(
            "ix_documents_org_content_hash",
            "organization_id",
            "content_hash",
            postgresql_where=sql("content_hash IS NOT NULL"),
        ),
        Index("ix_documents_policy_id", "policy_id"),
    )


class ExtractionMethod(StrEnum):
    TEXT = "text"  # native text layer
    OCR = "ocr"
    PENDING_OCR = "pending_ocr"  # no usable text layer; queued for OCR
    OCR_UNAVAILABLE = "ocr_unavailable"  # needed OCR but no engine could run
    EMPTY = "empty"  # genuinely blank page


class DocumentPage(Base):
    """Extracted text of one page. Persisted per page so huge PDFs resume where they stopped."""

    __tablename__ = "document_pages"

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    page_number: Mapped[int] = mapped_column(primary_key=True)  # 1-based
    organization_id: Mapped[uuid.UUID]
    text: Mapped[str] = mapped_column(Text, default="")
    char_count: Mapped[int] = mapped_column(default=0)
    method: Mapped[str] = mapped_column(String(20))
    width: Mapped[float | None]
    height: Mapped[float | None]
    # Layout lines: [block_no, text, font_size, is_bold, y0]. Drives structure detection.
    lines: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    ocr_engine: Mapped[str | None] = mapped_column(String(50))

    __table_args__ = (
        Index(
            "ix_document_pages_pending_ocr",
            "document_id",
            postgresql_where=sql("method = 'pending_ocr'"),
        ),
    )


class DocumentSection(UUIDPrimaryKeyMixin, Base):
    """A heading-delimited section. Sections nest via parent_id (5 → 5.2)."""

    __tablename__ = "document_sections"

    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    organization_id: Mapped[uuid.UUID]
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_sections.id", ondelete="CASCADE")
    )
    order_index: Mapped[int]
    level: Mapped[int]
    number: Mapped[str | None] = mapped_column(String(50))
    title: Mapped[str] = mapped_column(String(500))
    # "5 Loan to Value > 5.2 LTV for High Value Loans"
    path: Mapped[str] = mapped_column(Text)
    page_start: Mapped[int]
    page_end: Mapped[int]
    content: Mapped[str] = mapped_column(Text, default="")
    # [[char_offset, page_number], ...] where each page's text begins in `content`.
    page_marks: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    char_count: Mapped[int] = mapped_column(default=0)
    content_hash: Mapped[str] = mapped_column(String(64))
    # Section-level search for hierarchical retrieval (document -> section -> chunk).
    tsv = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('english', coalesce(path, '')), 'A') || "
            "setweight(to_tsvector('english', left(coalesce(content, ''), 100000)), 'C')",
            persisted=True,
        ),
    )

    __table_args__ = (
        Index("ix_document_sections_document_order", "document_id", "order_index"),
        Index("ix_document_sections_tsv", "tsv", postgresql_using="gin"),
        # ON DELETE CASCADE looks up each deleted section's children by parent_id.
        Index("ix_document_sections_parent", "parent_id"),
    )
