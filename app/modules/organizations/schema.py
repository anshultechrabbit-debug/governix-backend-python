import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.modules.organizations.model import OrganizationStatus
from app.modules.users.schema import MIN_PASSWORD_LENGTH

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{1,98}[a-z0-9]$"
COLOR_PATTERN = r"^#[0-9a-fA-F]{6}$"


class Branding(BaseModel):
    primary_color: str | None = Field(default=None, pattern=COLOR_PATTERN)
    secondary_color: str | None = Field(default=None, pattern=COLOR_PATTERN)
    contact_email: EmailStr | None = None
    contact_phone: str | None = Field(default=None, max_length=50)
    website: str | None = Field(default=None, max_length=300)
    address: str | None = Field(default=None, max_length=2000)


class FirstAdmin(BaseModel):
    """The organisation's first admin, created with it (spec §7.2-7.3)."""

    email: EmailStr
    full_name: str = Field(min_length=2, max_length=200)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=1024)


class OrganizationCreate(Branding):
    name: str = Field(min_length=2, max_length=200)
    slug: str = Field(pattern=SLUG_PATTERN)
    admin: FirstAdmin | None = None


class OrganizationUpdate(Branding):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    status: OrganizationStatus | None = None


class OrganizationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    status: str
    created_at: datetime
    primary_color: str | None = None
    secondary_color: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    website: str | None = None
    address: str | None = None
    has_logo: bool = False


def organization_read(organization) -> OrganizationRead:
    read = OrganizationRead.model_validate(organization)
    read.has_logo = organization.logo_key is not None
    return read
