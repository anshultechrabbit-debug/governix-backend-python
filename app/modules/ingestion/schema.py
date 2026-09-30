import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel


class AnalysisRead(BaseModel):
    document_id: uuid.UUID
    # Everything below is DETECTED/SUGGESTED by the system, not confirmed.
    detected: dict[str, Any]
    suggested_name: str | None
    name_confidence: float
    suggested_category_id: uuid.UUID | None
    suggested_category_name: str | None
    category_confidence: float
    category_ranking: list[dict[str, Any]]
    decision: str
    confidence: float
    matched_policy: dict[str, Any] | None
    duplicate_of_document_id: uuid.UUID | None
    signals: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    amendment_targets: list[dict[str, Any]]
    conflict: dict[str, Any] | None
    missing_fields: list[str]
    review_status: str
    reviewed_by_id: uuid.UUID | None
    reviewed_at: datetime | None
    resolution: dict[str, Any] | None
    suggested_initial_version: dict[str, Any] | None


class RelationshipInput(BaseModel):
    relation_type: str
    target_policy_id: uuid.UUID
    target_version_id: uuid.UUID | None = None
    clauses: list[str] = []
    evidence: str | None = None


class PolicyInput(BaseModel):
    name: str
    category_id: uuid.UUID
    policy_number: str | None = None
    document_number: str | None = None
    issuer: str | None = None
    issuing_department: str | None = None
    owner: str | None = None
    description: str | None = None
    branch_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None


class VersionInput(BaseModel):
    version_label: str | None = None
    revision_number: int | None = None
    # Optional. When omitted: the date the document states, else the upload date.
    effective_from: date | None = None
    effective_to: date | None = None
