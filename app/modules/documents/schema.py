import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StageProgress(BaseModel):
    stage: str
    status: str
    done_units: int
    total_units: int | None
    percent: float | None
    started_at: datetime | None
    finished_at: datetime | None
    # Measured from throughput so far; null until there is enough data. Always an estimate.
    estimated_seconds_remaining: int | None
    detail: dict[str, Any]


class Progress(BaseModel):
    overall_percent: float
    stages: list[StageProgress]


class DocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    branch_id: uuid.UUID | None
    department_id: uuid.UUID | None
    category_id: uuid.UUID | None
    policy_id: uuid.UUID | None
    policy_version_id: uuid.UUID | None
    title: str | None
    original_filename: str
    content_type: str
    size_bytes: int
    file_sha256: str
    page_count: int | None
    status: str
    error: dict[str, Any] | None
    uploaded_by_id: uuid.UUID | None
    duplicate_of_id: uuid.UUID | None
    created_at: datetime
    ready_at: datetime | None


class DocumentDetail(DocumentRead):
    pdf_metadata: dict[str, Any]
    duplicate_override_reason: str | None
    progress: Progress | None = None
    # Part of a bulk upload that will file it with the rest of its group: no one confirms it by hand.
    upload_batch_id: uuid.UUID | None = None
    filed_by_batch: bool = False


class DocumentUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    category_id: uuid.UUID | None = None
    branch_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None


class DuplicateInfo(BaseModel):
    document_id: uuid.UUID
    title: str | None
    original_filename: str
    uploaded_at: datetime
    policy_id: uuid.UUID | None
    policy_version_id: uuid.UUID | None


class UploadLimits(BaseModel):
    max_size_bytes: int
    accepted_extensions: list[str]
    accepted_types: list[str]


class DuplicateCheck(BaseModel):
    sha256: list[str] = Field(min_length=1, max_length=500)


class DuplicateResult(BaseModel):
    sha256: str
    duplicate: bool
    # The match is in a branch or department the caller cannot see: nothing about it is revealed.
    restricted: bool = False
    existing: DuplicateInfo | None = None


class PageText(BaseModel):
    page_number: int
    text: str
    method: str
    width: float | None
    height: float | None


class OutlineEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    number: str | None
    title: str
    level: int
    page_start: int
    page_end: int


class ChunkRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_id: uuid.UUID
    section_number: str | None
    section_path: str
    text: str
    page_start: int
    page_end: int
