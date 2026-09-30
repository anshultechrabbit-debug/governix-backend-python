import uuid
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_storage
from app.core.pagination import Page, PageParams, page_params
from app.core.responses import ApiResponse, ok
from app.infrastructure.storage.base import Storage
from app.modules.auth.dependencies import require
from app.modules.auth.permissions import Permission, Principal
from app.modules.tickets.model import TicketKind, TicketPriority, TicketStatus
from app.modules.tickets.service import TicketService, TicketView

router = APIRouter(prefix="/tickets", tags=["complaints and support"])

Supporter = Annotated[Principal, Depends(require(Permission.SUPPORT))]


def get_service(db: Annotated[Session, Depends(get_db)], storage: Annotated[Storage, Depends(get_storage)]) -> TicketService:
    return TicketService(db, storage)


Service = Annotated[TicketService, Depends(get_service)]


class TicketCreate(BaseModel):
    kind: TicketKind = TicketKind.SUPPORT
    subject: str = Field(min_length=3, max_length=300)
    description: str = Field(min_length=5, max_length=10_000)
    priority: TicketPriority = TicketPriority.MEDIUM
    category: str | None = Field(default=None, max_length=100)


class TicketRead(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    branch_id: uuid.UUID | None
    kind: str
    level: str
    category: str | None
    subject: str
    description: str
    priority: str
    status: str
    created_by_id: uuid.UUID | None
    created_by_name: str | None
    assigned_to_id: uuid.UUID | None
    assigned_to_name: str | None
    message_count: int
    can_handle: bool
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None
    closed_at: datetime | None


class MessageRead(BaseModel):
    id: uuid.UUID
    author_id: uuid.UUID | None
    author_name: str | None
    author_role: str | None
    body: str | None
    status_change: str | None
    created_at: datetime


class AttachmentRead(BaseModel):
    id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    uploaded_by_id: uuid.UUID | None
    created_at: datetime


class TicketDetail(TicketRead):
    messages: list[MessageRead]
    attachments: list[AttachmentRead]


class ReplyCreate(BaseModel):
    body: str = Field(min_length=1, max_length=10_000)


class StatusChange(BaseModel):
    status: TicketStatus
    note: str | None = Field(default=None, max_length=2000)


class AssignTicket(BaseModel):
    user_id: uuid.UUID | None = None


def _read(view: TicketView) -> TicketRead:
    t = view.ticket
    return TicketRead(
        id=t.id, organization_id=t.organization_id, branch_id=t.branch_id, kind=t.kind, level=t.level,
        category=t.category, subject=t.subject, description=t.description, priority=t.priority, status=t.status,
        created_by_id=t.created_by_id, created_by_name=view.created_by_name, assigned_to_id=t.assigned_to_id,
        assigned_to_name=view.assigned_to_name, message_count=view.message_count, can_handle=view.can_handle,
        created_at=t.created_at, updated_at=t.updated_at, resolved_at=t.resolved_at, closed_at=t.closed_at,
    )


def _detail(service: TicketService, principal: Principal, ticket_id: uuid.UUID) -> TicketDetail:
    view, messages, attachments = service.thread(principal, ticket_id)
    return TicketDetail(
        **_read(view).model_dump(),
        messages=[MessageRead(id=m.id, author_id=m.author_id, author_name=name, author_role=role, body=m.body,
                              status_change=m.status_change, created_at=m.created_at) for m, name, role in messages],
        attachments=[AttachmentRead(id=a.id, filename=a.filename, content_type=a.content_type, size_bytes=a.size_bytes,
                                    uploaded_by_id=a.uploaded_by_id, created_at=a.created_at) for a in attachments],
    )


@router.post("", response_model=ApiResponse[TicketDetail], status_code=201)
def create_ticket(body: TicketCreate, principal: Supporter, service: Service):
    """Raise a complaint (Users) or support ticket; it is routed to the level above the caller."""
    ticket = service.create(principal, kind=body.kind, subject=body.subject, description=body.description,
                            priority=body.priority, category=body.category)
    return ok(_detail(service, principal, ticket.id))


@router.get("", response_model=ApiResponse[Page[TicketRead]])
def list_tickets(
    principal: Supporter, service: Service, page: Annotated[PageParams, Depends(page_params)],
    box: Literal["all", "mine", "queue"] = "all",
    status: Annotated[str | None, Query(pattern="^(active|open|in_progress|waiting_for_response|resolved|closed)$")] = None,
    kind: TicketKind | None = None,
):
    """box=mine: tickets I raised; box=queue: tickets I handle."""
    views, total = service.find(principal, box=box, status=status, kind=kind, limit=page.limit, offset=page.offset)
    return ok(Page(items=[_read(v) for v in views], total=total, **page.model_dump()))


@router.get("/{ticket_id}", response_model=ApiResponse[TicketDetail])
def get_ticket(ticket_id: uuid.UUID, principal: Supporter, service: Service):
    return ok(_detail(service, principal, ticket_id))


@router.post("/{ticket_id}/messages", response_model=ApiResponse[TicketDetail], status_code=201)
def reply(ticket_id: uuid.UUID, body: ReplyCreate, principal: Supporter, service: Service):
    service.reply(principal, ticket_id, body.body)
    return ok(_detail(service, principal, ticket_id))


@router.post("/{ticket_id}/status", response_model=ApiResponse[TicketDetail])
def change_status(ticket_id: uuid.UUID, body: StatusChange, principal: Supporter, service: Service):
    service.change_status(principal, ticket_id, body.status, body.note)
    return ok(_detail(service, principal, ticket_id))


@router.post("/{ticket_id}/assign", response_model=ApiResponse[TicketDetail])
def assign(ticket_id: uuid.UUID, body: AssignTicket, principal: Supporter, service: Service):
    service.assign(principal, ticket_id, body.user_id)
    return ok(_detail(service, principal, ticket_id))


@router.post("/{ticket_id}/attachments", response_model=ApiResponse[TicketDetail], status_code=201)
def attach(ticket_id: uuid.UUID, principal: Supporter, service: Service, file: Annotated[UploadFile, File()]):
    service.attach(principal, ticket_id, file=file.file, filename=file.filename or "attachment")
    return ok(_detail(service, principal, ticket_id))


@router.get("/{ticket_id}/attachments/{attachment_id}")
def download_attachment(ticket_id: uuid.UUID, attachment_id: uuid.UUID, principal: Supporter, service: Service):
    attachment = service.attachment(principal, ticket_id, attachment_id)
    with service.storage.open(attachment.storage_key) as handle:
        content = handle.read()
    return Response(
        content, media_type=attachment.content_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(attachment.filename)}",
                 "X-Content-Type-Options": "nosniff"},
    )
