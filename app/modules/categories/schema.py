import uuid
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.modules.categories.model import CategoryType
from app.modules.policies.schema import VersionRead

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,98}[a-z0-9]$"


def _clean_name(value: str) -> str:
    value = " ".join(value.split())
    if len(value) < 2:
        raise ValueError("The name must have at least 2 characters.")
    return value


# Whitespace is collapsed; names are unique per organisation ignoring case.
CategoryName = Annotated[str, Field(min_length=2, max_length=100), AfterValidator(_clean_name)]


class CategoryCreate(BaseModel):
    name: CategoryName
    category_type: CategoryType
    description: str | None = Field(default=None, max_length=2000)
    is_active: bool = True
    # Derived from the name when omitted.
    slug: str | None = Field(default=None, pattern=SLUG_PATTERN)
    # Defaults to the type's rank (see categories.defaults.TYPE_AUTHORITY_RANK).
    authority_rank: int | None = Field(default=None, ge=0, le=1000)
    keywords: list[str] = Field(default_factory=list, max_length=100)


class CategoryUpdate(BaseModel):
    name: CategoryName | None = None
    category_type: CategoryType | None = None
    description: str | None = Field(default=None, max_length=2000)  # null clears it
    authority_rank: int | None = Field(default=None, ge=0, le=1000)
    keywords: list[str] | None = Field(default=None, max_length=100)
    is_active: bool | None = None


class CategoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    description: str | None
    category_type: CategoryType
    authority_rank: int
    keywords: list[str]
    is_active: bool
    is_system: bool
    created_at: datetime
    updated_at: datetime


class CategorySummary(CategoryRead):
    """A category with what it holds, counted over the documents the caller can see."""

    document_count: int = 0
    last_updated: datetime


class CategoryVersion(VersionRead):
    """A version with the file behind it."""

    filename: str
    title: str | None
    content_type: str
    size_bytes: int
    page_count: int | None
    document_status: str
    uploaded_at: datetime
    uploaded_by_id: uuid.UUID | None
    uploaded_by_name: str | None


class CategoryDocument(BaseModel):
    """A document (business identity) in the category and its versions, in display order."""

    id: uuid.UUID
    name: str
    policy_number: str | None
    description: str | None
    status: str
    branch_id: uuid.UUID | None
    department_id: uuid.UUID | None
    display_order: int | None
    created_at: datetime
    updated_at: datetime
    version_count: int  # active versions
    current_version_id: uuid.UUID | None
    latest_upload_at: datetime | None
    versions: list[CategoryVersion]


class PendingUpload(BaseModel):
    """A file uploaded into the category that is not yet registered as a document version."""

    document_id: uuid.UUID
    filename: str
    title: str | None
    status: str
    size_bytes: int
    uploaded_at: datetime
    uploaded_by_name: str | None
    error: str | None
