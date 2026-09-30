import uuid
from enum import StrEnum

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class CategoryType(StrEnum):
    """What kind of documents a category holds. Drives the default authority rank."""

    POLICY = "policy"
    CIRCULAR = "circular"
    SOP = "sop"
    GUIDELINE = "guideline"
    MANUAL = "manual"
    NOTICE = "notice"
    PROCESS_DOCUMENT = "process_document"
    FORM = "form"
    FAQ = "faq"
    REGULATORY_DOCUMENT = "regulatory_document"
    OTHER = "other"


class Category(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "categories"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    slug: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    category_type: Mapped[str] = mapped_column(String(30), default=CategoryType.OTHER, server_default="other")
    # Higher wins when sources conflict and no amendment/effective-date rule decides.
    authority_rank: Mapped[int] = mapped_column(default=50)
    # Phrases that indicate this category during classification (admin-editable).
    keywords: Mapped[list[str]] = mapped_column(ARRAY(String(100)), default=list)
    is_active: Mapped[bool] = mapped_column(default=True)
    is_system: Mapped[bool] = mapped_column(default=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "slug", name="uq_categories_organization_id_slug"),
        # Categories are organisation-wide, so a name is unique across every branch.
        Index("uq_categories_org_name", "organization_id", func.lower(name), unique=True),
    )
