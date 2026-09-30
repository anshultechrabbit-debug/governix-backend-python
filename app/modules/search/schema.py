import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    mode: Literal["current", "as_of", "versions", "all"] = "current"
    as_of: date | None = None
    version_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    category_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    policy_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    document_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    branch_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    limit: int = Field(default=20, ge=1, le=100)


class Provenance(BaseModel):
    document_id: uuid.UUID
    document_title: str | None
    policy_id: uuid.UUID | None
    policy_name: str | None
    category_id: uuid.UUID | None
    version_id: uuid.UUID | None
    version_label: str | None
    effective_from: date | None
    effective_to: date | None
    section_number: str | None
    section_path: str
    page_start: int
    page_end: int


class Passage(BaseModel):
    chunk_id: uuid.UUID
    text: str
    score: float
    lanes: dict[str, int]
    source: Provenance


class PolicyHit(BaseModel):
    id: uuid.UUID
    name: str
    policy_number: str | None
    category_id: uuid.UUID


class SearchResponse(BaseModel):
    query: str
    mode: str
    as_of: date | None
    passages: list[Passage]
    policies: list[PolicyHit]
    terms: list[str]
    timings_ms: dict[str, float]
    cache_hit: bool = False
