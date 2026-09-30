import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.modules.branches.schema import CODE_PATTERN


class DepartmentCreate(BaseModel):
    branch_id: uuid.UUID
    name: str = Field(min_length=2, max_length=200)
    code: str = Field(pattern=CODE_PATTERN)


class DepartmentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    is_active: bool | None = None


class DepartmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    branch_id: uuid.UUID
    name: str
    code: str
    is_active: bool
    created_at: datetime
