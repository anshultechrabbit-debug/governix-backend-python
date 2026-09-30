import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

CODE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,49}$"


class BranchCreate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    code: str = Field(pattern=CODE_PATTERN)


class BranchUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    is_active: bool | None = None


class BranchRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    code: str
    is_active: bool
    created_at: datetime
