import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.pagination import PageParams, page_params
from app.core.responses import ApiResponse, ok
from app.modules.auth.dependencies import CurrentPrincipal
from app.modules.notifications import service

router = APIRouter(prefix="/notifications", tags=["notifications"])

DB = Annotated[Session, Depends(get_db)]


class NotificationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: str
    title: str
    body: str | None
    link: str | None
    data: dict[str, Any]
    read_at: datetime | None
    created_at: datetime


class Inbox(BaseModel):
    items: list[NotificationRead]
    total: int
    unread: int
    limit: int
    offset: int


class MarkRead(BaseModel):
    # Omit to mark everything read.
    ids: list[uuid.UUID] | None = Field(default=None, max_length=500)


@router.get("", response_model=ApiResponse[Inbox])
def my_notifications(principal: CurrentPrincipal, db: DB, page: Annotated[PageParams, Depends(page_params)], unread_only: bool = False):
    items, total, unread = service.inbox(db, principal, unread_only=unread_only, limit=page.limit, offset=page.offset)
    return ok(Inbox(items=[NotificationRead.model_validate(n) for n in items], total=total, unread=unread,
                    limit=page.limit, offset=page.offset))


@router.post("/read", response_model=ApiResponse[dict])
def mark_read(body: MarkRead, principal: CurrentPrincipal, db: DB):
    return ok({"updated": service.mark_read(db, principal, body.ids)})
