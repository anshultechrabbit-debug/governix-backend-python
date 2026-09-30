import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Date, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy import text as sql
from sqlalchemy.dialects.postgresql import ExcludeConstraint, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PolicyStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class Policy(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Stable business identity (e.g. "Home Loan Credit Policy") across all its versions.

    Circulars, SOPs, manuals etc. are identities too; the category says which kind.
    """

    __tablename__ = "policies"

    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"))
    branch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("branches.id", ondelete="RESTRICT"))
    department_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("departments.id", ondelete="RESTRICT"))
    category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("categories.id", ondelete="RESTRICT"))
    name: Mapped[str] = mapped_column(String(500))
    normalized_name: Mapped[str] = mapped_column(String(500))
    policy_number: Mapped[str | None] = mapped_column(String(100))
    document_number: Mapped[str | None] = mapped_column(String(100))
    issuer: Mapped[str | None] = mapped_column(String(200))
    issuing_department: Mapped[str | None] = mapped_column(String(200))
    owner: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default=PolicyStatus.ACTIVE)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # Position a person arranged within the category (drag and drop). NULL until
    # arranged; unarranged documents are listed first, newest first.
    display_order: Mapped[int | None]

    __table_args__ = (
        Index("ix_policies_scope", "organization_id", "branch_id", "department_id"),
        Index("ix_policies_org_category", "organization_id", "category_id"),
        Index("ix_policies_category_display_order", "category_id", "display_order"),
        # One identity per policy number within an organisation.
        Index(
            "uq_policies_org_policy_number",
            "organization_id",
            func.upper(policy_number),
            unique=True,
            postgresql_where=sql("policy_number IS NOT NULL"),
        ),
        Index("ix_policies_org_document_number", "organization_id", "document_number"),
        Index(
            "ix_policies_normalized_name_trgm",
            "normalized_name",
            postgresql_using="gin",
            postgresql_ops={"normalized_name": "gin_trgm_ops"},
        ),
    )


class VersionStatus(StrEnum):
    ACTIVE = "active"  # part of the effective-date timeline (current, historical or scheduled)
    WITHDRAWN = "withdrawn"  # removed from the timeline; kept for history, never deleted


class DateSource(StrEnum):
    ENTERED = "entered"  # a person entered the effective date
    DETECTED = "detected"  # the document states it
    UPLOAD_DATE = "upload_date"  # not stated: the day the file was uploaded
    INFERRED = "inferred"  # not stated: placed just before the next version, in the order given


# Placeholder dates: they order the timeline but are not business dates, so they
# may be moved to make room for a newer version.
AUTO_DATE_SOURCES = frozenset({DateSource.UPLOAD_DATE, DateSource.INFERRED})


class PolicyVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One revision of a policy, backed by exactly one source document.

    Currency is decided by business effective dates ([effective_from, effective_to)),
    never by upload order. Versions are never overwritten.
    """

    __tablename__ = "policy_versions"

    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"))
    policy_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("policies.id", ondelete="RESTRICT"))
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"), unique=True)
    # Immutable registration sequence within the policy (1, 2, 3 ...).
    version_number: Mapped[int]
    # Business label printed on the document ("4", "4.1"); what users see as "v4".
    version_label: Mapped[str] = mapped_column(String(50))
    # True when no label was given and the registration number was used. Only such
    # labels are renumbered when a person reorders the versions; a printed label never is.
    version_label_auto: Mapped[bool] = mapped_column(default=False, server_default=sql("false"))
    revision_number: Mapped[int] = mapped_column(default=0)
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)  # exclusive
    # Where effective_from came from. Only "entered" and "detected" are business
    # dates; the other two keep the timeline ordered when no date was given and
    # may be moved to make room for a later version (see versions.timeline).
    effective_date_source: Mapped[str] = mapped_column(String(20), default="entered", server_default="entered")
    status: Mapped[str] = mapped_column(String(20), default=VersionStatus.ACTIVE)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    supersedes_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("policy_versions.id", ondelete="SET NULL")
    )
    superseded_by_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("policy_versions.id", ondelete="SET NULL")
    )
    # Deterministic diff against the preceding version (never LLM-derived).
    change_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Optional readable summary generated from change_summary; always labelled AI-generated.
    ai_change_summary: Mapped[str | None] = mapped_column(Text)
    # AI summary of this version's own text (overview, key rules, actions ...),
    # generated in the background after indexing; always labelled AI-generated.
    ai_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ai_summary_model: Mapped[str | None] = mapped_column(String(100))
    ai_summary_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    withdrawn_reason: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("policy_id", "version_number", name="uq_policy_versions_policy_id_version_number"),
        Index("ix_policy_versions_policy_effective", "policy_id", "effective_from"),
        Index("ix_policy_versions_org_effective", "organization_id", "effective_from", "effective_to"),
        # The database itself guarantees no two active versions overlap in time.
        ExcludeConstraint(
            ("policy_id", "="),
            (sql("daterange(effective_from, effective_to, '[)')"), "&&"),
            where=sql("status = 'active'"),
            using="gist",
            name="ex_policy_versions_no_overlap",
        ),
    )


class RelationType(StrEnum):
    AMENDS = "AMENDS"
    SUPERSEDES = "SUPERSEDES"
    REFERENCES = "REFERENCES"
    CLARIFIES = "CLARIFIES"
    REPLACES = "REPLACES"
    DERIVED_FROM = "DERIVED_FROM"


class RelationStatus(StrEnum):
    SUGGESTED = "suggested"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class DocumentRelationship(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Circular 2026/45 --AMENDS--> Home Loan Credit Policy v4 (clauses 5.2)."""

    __tablename__ = "document_relationships"

    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"))
    source_document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    relation_type: Mapped[str] = mapped_column(String(20))
    target_policy_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("policies.id", ondelete="CASCADE"))
    target_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("policy_versions.id", ondelete="SET NULL")
    )
    clauses: Mapped[list[str]] = mapped_column(JSONB, default=list)
    evidence: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default=RelationStatus.SUGGESTED)
    confidence: Mapped[float | None]
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    __table_args__ = (
        Index("ix_document_relationships_source", "source_document_id"),
        Index("ix_document_relationships_target", "target_policy_id", "status"),
    )
