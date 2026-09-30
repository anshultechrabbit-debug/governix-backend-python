import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class VersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    policy_id: uuid.UUID
    document_id: uuid.UUID
    version_number: int
    version_label: str
    revision_number: int
    effective_from: date
    # entered | detected | upload_date | inferred. Only the first two are business dates.
    effective_date_source: str = "entered"
    effective_to: date | None
    status: str
    # current | historical | scheduled | withdrawn  (derived from effective dates)
    timeline_state: str | None = None
    supersedes_version_id: uuid.UUID | None
    superseded_by_version_id: uuid.UUID | None
    content_hash: str | None
    created_by_id: uuid.UUID | None
    confirmed_at: datetime | None
    created_at: datetime
    withdrawn_reason: str | None
    version_label_auto: bool = False
    has_ai_summary: bool = False


class VersionDetail(VersionRead):
    change_summary: dict[str, Any] | None
    ai_change_summary: str | None


class PolicyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    branch_id: uuid.UUID | None
    department_id: uuid.UUID | None
    category_id: uuid.UUID
    name: str
    policy_number: str | None
    document_number: str | None
    issuer: str | None
    issuing_department: str | None
    owner: str | None
    description: str | None
    status: str
    created_at: datetime
    display_order: int | None = None
    current_version: VersionRead | None = None
    version_count: int = 0


class RelationshipRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_document_id: uuid.UUID
    source_title: str | None = None
    source_policy_id: uuid.UUID | None = None
    relation_type: str
    target_policy_id: uuid.UUID
    target_policy_name: str | None = None
    target_version_id: uuid.UUID | None
    clauses: list[str]
    evidence: str | None
    status: str
    created_at: datetime


class PolicyDetail(PolicyRead):
    versions: list[VersionRead]
    incoming_relationships: list[RelationshipRead]
    outgoing_relationships: list[RelationshipRead]


class PolicyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=500)
    category_id: uuid.UUID | None = None
    issuer: str | None = Field(default=None, max_length=200)
    issuing_department: str | None = Field(default=None, max_length=200)
    owner: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    status: str | None = Field(default=None, pattern="^(active|archived)$")


class WithdrawRequest(BaseModel):
    reason: str = Field(min_length=5, max_length=2000)


class PolicyCreate(BaseModel):
    """A policy created by hand; its documents (versions) are uploaded into it afterwards."""

    name: str = Field(min_length=2, max_length=500)
    category_id: uuid.UUID
    # global: the whole organisation (organisation admins only); branch: one branch.
    scope: Literal["global", "branch"] = "branch"
    branch_id: uuid.UUID | None = None  # branch scope; a Branch Manager's own branch when omitted
    department_id: uuid.UUID | None = None
    description: str | None = Field(default=None, max_length=5000)
    policy_number: str | None = Field(default=None, max_length=100)
    owner: str | None = Field(default=None, max_length=200)


class VersionOrder(BaseModel):
    """Every active version, newest (latest) first. The order becomes the timeline."""

    version_ids: list[uuid.UUID] = Field(min_length=1, max_length=1000)
    # Required when the order changes which version is in force (spec §18).
    confirm_latest_change: bool = False


class VersionMove(BaseModel):
    target_policy_id: uuid.UUID
    # Moving always changes which policy the document belongs to (spec §19).
    confirm: bool = False


class DocumentMove(BaseModel):
    """Place a document directly after or before another in its category; neither = first."""

    after_id: uuid.UUID | None = None
    before_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def one_anchor(self) -> "DocumentMove":
        if self.after_id and self.before_id:
            raise ValueError("Give after_id or before_id, not both.")
        return self


class RelationshipCreate(BaseModel):
    relation_type: str
    target_policy_id: uuid.UUID
    target_version_id: uuid.UUID | None = None
    clauses: list[str] = Field(default_factory=list, max_length=100)
    evidence: str | None = Field(default=None, max_length=2000)
