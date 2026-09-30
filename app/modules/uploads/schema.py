import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field, model_validator

MAX_GROUPS = 200
MAX_ITEMS = 500


class BatchItemIn(BaseModel):
    filename: str = Field(min_length=1, max_length=500)
    version_label: str | None = Field(default=None, max_length=50)
    # Optional: without it the document's own date, else the arranged order, is used.
    effective_from: date | None = None


class BatchGroupIn(BaseModel):
    """One policy and its versions, oldest first."""

    policy_id: uuid.UUID | None = None  # add versions to this existing policy ...
    new_policy_name: str | None = Field(default=None, max_length=500)  # ... or create one (name optional)
    category_id: uuid.UUID | None = None  # for a new policy; the detected category when omitted
    items: list[BatchItemIn] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def one_target(self) -> "BatchGroupIn":
        if self.policy_id and (self.new_policy_name or self.category_id):
            raise ValueError("A group either adds versions to an existing policy or creates a new one, not both.")
        return self


class BatchCreate(BaseModel):
    branch_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    groups: list[BatchGroupIn] = Field(min_length=1, max_length=MAX_GROUPS)

    @model_validator(mode="after")
    def bounded(self) -> "BatchCreate":
        if sum(len(g.items) for g in self.groups) > MAX_ITEMS:
            raise ValueError(f"A bulk upload holds at most {MAX_ITEMS} files.")
        return self


class BatchItemRead(BaseModel):
    id: uuid.UUID
    position: int
    original_filename: str
    document_id: uuid.UUID | None
    document_status: str | None = None
    version_label: str | None
    effective_from: date | None
    status: str
    message: str | None
    policy_version_id: uuid.UUID | None


class BatchGroupRead(BaseModel):
    id: uuid.UUID
    position: int
    policy_id: uuid.UUID | None
    policy_name: str | None = None
    new_policy_name: str | None
    category_id: uuid.UUID | None
    status: str
    message: str | None
    items: list[BatchItemRead]


class BatchRead(BaseModel):
    id: uuid.UUID
    status: str
    created_at: datetime
    completed_at: datetime | None
    created_by_id: uuid.UUID | None
    counts: dict[str, int]
    groups: list[BatchGroupRead]


class BatchSummary(BaseModel):
    id: uuid.UUID
    status: str
    created_at: datetime
    completed_at: datetime | None
    counts: dict[str, int]


class ExistingPolicyMatch(BaseModel):
    """An existing policy the file most likely is a version of."""

    policy_id: uuid.UUID
    name: str
    category_id: uuid.UUID
    category_name: str | None
    current_version_label: str | None
    current_effective_from: date | None
    version_count: int
    reasons: list[str]
    # False when the policy is visible but outside the scope this person manages.
    can_add_version: bool


class IdenticalDocument(BaseModel):
    """A document already uploaded with exactly the same file."""

    document_id: uuid.UUID
    title: str | None
    original_filename: str
    policy_id: uuid.UUID | None
    policy_name: str | None


class PolicySuggestion(BaseModel):
    """What a file's opening pages say about it, read before it is uploaded for real."""

    # False for scanned or unreadable files: their name is only known after OCR.
    readable: bool
    name: str | None = None
    # What the content covers, to say why a file seems to belong elsewhere.
    about: str | None = None
    name_source: str | None = None
    name_confidence: float = 0.0
    category_id: uuid.UUID | None = None
    category_name: str | None = None
    version_label: str | None = None
    effective_date: date | None = None
    existing_policy: ExistingPolicyMatch | None = None
    identical_document: IdenticalDocument | None = None
