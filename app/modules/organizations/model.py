from enum import StrEnum

from sqlalchemy import BigInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class OrganizationStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class Organization(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    status: Mapped[str] = mapped_column(String(20), default=OrganizationStatus.ACTIVE)
    # Bumped whenever the searchable knowledge changes; part of every RAG cache key.
    knowledge_version: Mapped[int] = mapped_column(BigInteger, default=0)

    # Branding and contact details, shown to the organisation's people.
    logo_key: Mapped[str | None] = mapped_column(String(500))
    logo_content_type: Mapped[str | None] = mapped_column(String(100))
    primary_color: Mapped[str | None] = mapped_column(String(7))  # "#1e3a8a"
    secondary_color: Mapped[str | None] = mapped_column(String(7))
    contact_email: Mapped[str | None] = mapped_column(String(320))
    contact_phone: Mapped[str | None] = mapped_column(String(50))
    website: Mapped[str | None] = mapped_column(String(300))
    address: Mapped[str | None] = mapped_column(Text)
