"""Complaints and support tickets (spec §45-47).

Who raises what, and who handles it:

    User             complaint or support  -> Branch Managers of their branch     (branch)
    Branch Manager   support               -> Organisation Admins                 (organization)
    Org Admin        support               -> Master Admins                       (platform)

The creator always sees their own tickets; a handler sees their queue. Every
reply and status change is kept as a message, and the other side is notified.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import BinaryIO

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased

from app.core.exceptions import ConflictError, NotFoundError, PayloadTooLargeError, PermissionDeniedError, ValidationError
from app.infrastructure.storage.base import Storage
from app.modules.audit.service import record_event
from app.modules.auth.permissions import Principal, Role
from app.modules.notifications.model import NotificationType
from app.modules.notifications.service import notify
from app.modules.tickets.model import (
    Ticket,
    TicketAttachment,
    TicketKind,
    TicketLevel,
    TicketMessage,
    TicketStatus,
)
from app.modules.users.model import User

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
ATTACHMENT_TYPES = {
    ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".txt": "text/plain", ".csv": "text/csv",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
LEVEL_FOR_ROLE = {
    Role.DEPARTMENT_USER: TicketLevel.BRANCH,
    Role.BRANCH_MANAGER: TicketLevel.ORGANIZATION,
    Role.ORG_ADMIN: TicketLevel.PLATFORM,
}
# What a creator may do to their own ticket.
CREATOR_TRANSITIONS = {
    TicketStatus.RESOLVED: {TicketStatus.CLOSED, TicketStatus.OPEN},  # accept, or reopen
    TicketStatus.CLOSED: {TicketStatus.OPEN},
    TicketStatus.OPEN: {TicketStatus.CLOSED},  # withdraw
    TicketStatus.IN_PROGRESS: {TicketStatus.CLOSED},
    TicketStatus.WAITING_FOR_RESPONSE: {TicketStatus.CLOSED},
}


@dataclass
class TicketView:
    ticket: Ticket
    created_by_name: str | None
    assigned_to_name: str | None
    message_count: int
    can_handle: bool


def is_handler(principal: Principal, ticket: Ticket) -> bool:
    if ticket.level == TicketLevel.BRANCH:
        return principal.role is Role.BRANCH_MANAGER and principal.branch_id == ticket.branch_id \
            and principal.organization_id == ticket.organization_id
    if ticket.level == TicketLevel.ORGANIZATION:
        return principal.role is Role.ORG_ADMIN and principal.organization_id == ticket.organization_id
    return principal.role is Role.MASTER_ADMIN


def handler_ids(session: Session, ticket: Ticket) -> set[uuid.UUID]:
    query = select(User.id).where(User.is_active)
    if ticket.level == TicketLevel.BRANCH:
        query = query.where(User.role == Role.BRANCH_MANAGER, User.branch_id == ticket.branch_id)
    elif ticket.level == TicketLevel.ORGANIZATION:
        query = query.where(User.role == Role.ORG_ADMIN, User.organization_id == ticket.organization_id)
    else:
        query = query.where(User.role == Role.MASTER_ADMIN)
    return set(session.scalars(query))


def _visible(principal: Principal):
    """Tickets the principal raised, plus the queue they handle."""
    own = Ticket.created_by_id == principal.user_id
    if principal.role is Role.BRANCH_MANAGER:
        queue = (Ticket.level == TicketLevel.BRANCH) & (Ticket.branch_id == principal.branch_id) \
            & (Ticket.organization_id == principal.organization_id)
    elif principal.role is Role.ORG_ADMIN:
        queue = (Ticket.level == TicketLevel.ORGANIZATION) & (Ticket.organization_id == principal.organization_id)
    elif principal.role is Role.MASTER_ADMIN:
        queue = Ticket.level == TicketLevel.PLATFORM
    else:
        return own
    return or_(own, queue)


class TicketService:
    def __init__(self, session: Session, storage: Storage) -> None:
        self.session = session
        self.storage = storage

    # --- create ---------------------------------------------------------------------

    def create(self, principal: Principal, *, kind: TicketKind, subject: str, description: str,
               priority: str, category: str | None) -> Ticket:
        level = LEVEL_FOR_ROLE.get(principal.role)
        if level is None or principal.organization_id is None:
            raise PermissionDeniedError("Your role does not raise tickets.")
        if kind == TicketKind.COMPLAINT and principal.role is not Role.DEPARTMENT_USER:
            raise ValidationError("Complaints are raised by users to their branch manager; raise a support ticket instead.")
        ticket = Ticket(
            organization_id=principal.organization_id,
            branch_id=principal.branch_id if level != TicketLevel.PLATFORM else None,
            kind=kind, level=level, category=(category or "").strip() or None,
            subject=subject.strip(), description=description.strip(), priority=priority,
            status=TicketStatus.OPEN, created_by_id=principal.user_id,
        )
        self.session.add(ticket)
        self.session.flush()
        record_event(self.session, "ticket.created", actor=principal, resource_type="ticket", resource_id=ticket.id,
                     details={"kind": kind, "level": level, "subject": ticket.subject})
        creator = self.session.get(User, principal.user_id)
        notify(
            self.session, handler_ids(self.session, ticket), NotificationType.TICKET_UPDATED,
            f"New {kind}: {ticket.subject}", body=f"Raised by {creator.full_name if creator else 'a user'}.",
            organization_id=ticket.organization_id, link=f"/support/{ticket.id}",
            data={"ticket_id": str(ticket.id)}, exclude=principal.user_id,
        )
        self.session.commit()
        return ticket

    # --- read -------------------------------------------------------------------------

    def get(self, principal: Principal, ticket_id: uuid.UUID) -> Ticket:
        ticket = self.session.scalar(select(Ticket).where(Ticket.id == ticket_id, _visible(principal)))
        if ticket is None:
            raise NotFoundError("Ticket not found.")
        return ticket

    def find(self, principal: Principal, *, box: str, status: str | None, kind: str | None,
             limit: int, offset: int) -> tuple[list[TicketView], int]:
        query = select(Ticket).where(_visible(principal))
        if box == "mine":
            query = query.where(Ticket.created_by_id == principal.user_id)
        elif box == "queue":
            query = query.where(Ticket.created_by_id != principal.user_id)
        if status == "active":
            query = query.where(Ticket.status.not_in([TicketStatus.RESOLVED, TicketStatus.CLOSED]))
        elif status:
            query = query.where(Ticket.status == status)
        if kind:
            query = query.where(Ticket.kind == kind)
        total = self.session.scalar(select(func.count()).select_from(query.subquery())) or 0
        tickets = self.session.scalars(query.order_by(Ticket.updated_at.desc()).limit(limit).offset(offset)).all()
        return self.views(principal, list(tickets)), total

    def views(self, principal: Principal, tickets: list[Ticket]) -> list[TicketView]:
        if not tickets:
            return []
        people = {t.created_by_id for t in tickets} | {t.assigned_to_id for t in tickets}
        names = dict(self.session.execute(select(User.id, User.full_name).where(User.id.in_(people - {None}))).all())
        counts = dict(self.session.execute(
            select(TicketMessage.ticket_id, func.count()).where(TicketMessage.ticket_id.in_([t.id for t in tickets]))
            .group_by(TicketMessage.ticket_id)
        ).all())
        return [
            TicketView(t, names.get(t.created_by_id), names.get(t.assigned_to_id), counts.get(t.id, 0), is_handler(principal, t))
            for t in tickets
        ]

    def thread(self, principal: Principal, ticket_id: uuid.UUID) -> tuple[TicketView, list, list[TicketAttachment]]:
        ticket = self.get(principal, ticket_id)
        author = aliased(User)
        messages = self.session.execute(
            select(TicketMessage, author.full_name, author.role)
            .outerjoin(author, author.id == TicketMessage.author_id)
            .where(TicketMessage.ticket_id == ticket.id).order_by(TicketMessage.created_at)
        ).all()
        attachments = self.session.scalars(
            select(TicketAttachment).where(TicketAttachment.ticket_id == ticket.id).order_by(TicketAttachment.created_at)
        ).all()
        return self.views(principal, [ticket])[0], messages, list(attachments)

    # --- conversation -------------------------------------------------------------------

    def reply(self, principal: Principal, ticket_id: uuid.UUID, body: str) -> TicketMessage:
        ticket = self.get(principal, ticket_id)
        if ticket.status == TicketStatus.CLOSED:
            raise ConflictError("This ticket is closed. Reopen it to reply.", code="INVALID_STATE")
        handling = is_handler(principal, ticket)
        message = TicketMessage(ticket_id=ticket.id, organization_id=ticket.organization_id,
                                author_id=principal.user_id, body=body.strip())
        self.session.add(message)
        # A reply moves the ticket along: the handler has picked it up, or the creator has answered.
        if handling and ticket.status == TicketStatus.OPEN:
            self._set_status(principal, ticket, TicketStatus.IN_PROGRESS, record=False)
        elif not handling and ticket.status == TicketStatus.WAITING_FOR_RESPONSE:
            self._set_status(principal, ticket, TicketStatus.IN_PROGRESS, record=False)
        ticket.updated_at = datetime.now(UTC)
        self.session.flush()
        if handling:
            response_type = (NotificationType.COMPLAINT_RESPONSE if ticket.kind == TicketKind.COMPLAINT
                             else NotificationType.SUPPORT_RESPONSE)
            notify(self.session, [ticket.created_by_id], response_type, f"Response to: {ticket.subject}",
                   body=body.strip()[:300], organization_id=ticket.organization_id, link=f"/support/{ticket.id}",
                   data={"ticket_id": str(ticket.id)}, exclude=principal.user_id)
        else:
            recipients = [ticket.assigned_to_id] if ticket.assigned_to_id else handler_ids(self.session, ticket)
            notify(self.session, recipients, NotificationType.TICKET_UPDATED, f"New reply: {ticket.subject}",
                   body=body.strip()[:300], organization_id=ticket.organization_id, link=f"/support/{ticket.id}",
                   data={"ticket_id": str(ticket.id)}, exclude=principal.user_id)
        self.session.commit()
        return message

    def change_status(self, principal: Principal, ticket_id: uuid.UUID, status: TicketStatus, note: str | None) -> Ticket:
        ticket = self.get(principal, ticket_id)
        current = TicketStatus(ticket.status)
        if status == current:
            return ticket
        if not is_handler(principal, ticket):
            if ticket.created_by_id != principal.user_id or status not in CREATOR_TRANSITIONS.get(current, set()):
                raise PermissionDeniedError("You cannot move this ticket to that status.")
        self._set_status(principal, ticket, status, note=note)
        recipients = {ticket.created_by_id} | ({ticket.assigned_to_id} if ticket.assigned_to_id else handler_ids(self.session, ticket))
        notify(self.session, recipients, NotificationType.TICKET_UPDATED,
               f"{ticket.subject}: {status.value.replace('_', ' ')}", body=note,
               organization_id=ticket.organization_id, link=f"/support/{ticket.id}",
               data={"ticket_id": str(ticket.id), "status": status}, exclude=principal.user_id)
        self.session.commit()
        return ticket

    def _set_status(self, principal: Principal, ticket: Ticket, status: TicketStatus, *, note: str | None = None, record: bool = True) -> None:
        now = datetime.now(UTC)
        previous = ticket.status
        ticket.status = status
        ticket.resolved_at = now if status == TicketStatus.RESOLVED else (ticket.resolved_at if status == TicketStatus.CLOSED else None)
        ticket.closed_at = now if status == TicketStatus.CLOSED else None
        self.session.add(TicketMessage(ticket_id=ticket.id, organization_id=ticket.organization_id,
                                       author_id=principal.user_id, body=(note or "").strip() or None,
                                       status_change=status))
        if record:
            record_event(self.session, "ticket.status_changed", actor=principal, resource_type="ticket",
                         resource_id=ticket.id, details={"from": previous, "to": status})

    def assign(self, principal: Principal, ticket_id: uuid.UUID, user_id: uuid.UUID | None) -> Ticket:
        ticket = self.get(principal, ticket_id)
        if not is_handler(principal, ticket):
            raise PermissionDeniedError("Only the team handling this ticket can assign it.")
        if user_id is not None and user_id not in handler_ids(self.session, ticket):
            raise ValidationError("That person does not handle this ticket's queue.")
        ticket.assigned_to_id = user_id
        record_event(self.session, "ticket.assigned", actor=principal, resource_type="ticket", resource_id=ticket.id,
                     details={"assigned_to": str(user_id) if user_id else None})
        if user_id and user_id != principal.user_id:
            notify(self.session, [user_id], NotificationType.TICKET_UPDATED, f"Assigned to you: {ticket.subject}",
                   organization_id=ticket.organization_id, link=f"/support/{ticket.id}",
                   data={"ticket_id": str(ticket.id)})
        self.session.commit()
        return ticket

    # --- attachments ---------------------------------------------------------------------

    def attach(self, principal: Principal, ticket_id: uuid.UUID, *, file: BinaryIO, filename: str) -> TicketAttachment:
        ticket = self.get(principal, ticket_id)
        safe = (filename or "attachment").replace("/", "_").replace("\\", "_")[:300]
        extension = "." + safe.rsplit(".", 1)[-1].lower() if "." in safe else ""
        if extension not in ATTACHMENT_TYPES:
            raise ValidationError(f"Attach PDF, image, text, CSV, Word or Excel files ({', '.join(sorted(ATTACHMENT_TYPES))}).",
                                  code="UNSUPPORTED_FILE_TYPE")
        data = file.read(MAX_ATTACHMENT_BYTES + 1)
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise PayloadTooLargeError(f"Attachments are limited to {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB.")
        if not data:
            raise ValidationError("The file is empty.")
        attachment_id = uuid.uuid4()
        key = f"tickets/{ticket.organization_id}/{ticket.id}/{attachment_id}{extension}"
        stored = self.storage.put(key, data)
        try:
            attachment = TicketAttachment(
                id=attachment_id, ticket_id=ticket.id, organization_id=ticket.organization_id, filename=safe,
                content_type=ATTACHMENT_TYPES[extension], size_bytes=stored.size, storage_key=key,
                uploaded_by_id=principal.user_id,
            )
            self.session.add(attachment)
            ticket.updated_at = datetime.now(UTC)
            self.session.commit()
        except BaseException:
            self.session.rollback()
            self.storage.delete(key)
            raise
        return attachment

    def attachment(self, principal: Principal, ticket_id: uuid.UUID, attachment_id: uuid.UUID) -> TicketAttachment:
        ticket = self.get(principal, ticket_id)
        attachment = self.session.get(TicketAttachment, attachment_id)
        if attachment is None or attachment.ticket_id != ticket.id:
            raise NotFoundError("Attachment not found.")
        return attachment
