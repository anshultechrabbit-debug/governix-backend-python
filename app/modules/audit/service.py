import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.core.request_context import get_request_id
from app.modules.audit.model import AuditEvent
from app.modules.auth.permissions import Principal


def record_event(
    session: Session,
    action: str,
    *,
    actor: Principal | None = None,
    organization_id: uuid.UUID | None = None,
    resource_type: str | None = None,
    resource_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    """Add an audit event to the caller's transaction (committed with the change it describes)."""
    event = AuditEvent(
        organization_id=organization_id or (actor.organization_id if actor else None),
        actor_user_id=actor.user_id if actor else None,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        request_id=get_request_id(),
        details=details or {},
    )
    session.add(event)
    return event
