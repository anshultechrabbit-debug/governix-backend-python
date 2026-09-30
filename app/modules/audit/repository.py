import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.audit.model import AuditEvent


class AuditRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list(
        self,
        organization_id: uuid.UUID | None,
        *,
        action: str | None,
        resource_type: str | None,
        resource_id: uuid.UUID | None,
        actor_user_id: uuid.UUID | None,
        since: datetime | None,
        limit: int,
        offset: int,
    ) -> tuple[list[AuditEvent], int]:
        query = select(AuditEvent).where(AuditEvent.organization_id == organization_id)
        if action:
            query = query.where(AuditEvent.action == action)
        if resource_type:
            query = query.where(AuditEvent.resource_type == resource_type)
        if resource_id:
            query = query.where(AuditEvent.resource_id == resource_id)
        if actor_user_id:
            query = query.where(AuditEvent.actor_user_id == actor_user_id)
        if since:
            query = query.where(AuditEvent.created_at >= since)
        total = self.session.scalar(select(func.count()).select_from(query.subquery()))
        items = self.session.scalars(
            query.order_by(AuditEvent.created_at.desc()).limit(limit).offset(offset)
        ).all()
        return list(items), total or 0
