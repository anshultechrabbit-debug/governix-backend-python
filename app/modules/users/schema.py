import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.core.security import MIN_PASSWORD_LENGTH
from app.modules.auth.permissions import Role


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=2, max_length=200)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=1024)
    role: Role
    # Only the master admin chooses an organization; tenant admins create users in their own.
    organization_id: uuid.UUID | None = None
    branch_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=200)
    is_active: bool | None = None
    role: Role | None = None
    branch_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None


class PasswordReset(BaseModel):
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=1024)


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str
    role: str
    organization_id: uuid.UUID | None
    branch_id: uuid.UUID | None
    department_id: uuid.UUID | None
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime
