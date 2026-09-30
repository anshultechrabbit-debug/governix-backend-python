import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.pagination import Page, PageParams, page_params
from app.core.responses import ApiResponse, ok
from app.modules.audit.repository import AuditRepository
from app.modules.audit.schema import AuditEventRead
from app.modules.auth.dependencies import require
from app.modules.auth.permissions import Permission, Principal
from app.modules.auth.scope import tenant_id

router = APIRouter(prefix="/audit-events", tags=["audit"])

Auditor = Annotated[Principal, Depends(require(Permission.AUDIT_READ))]


@router.get("", response_model=ApiResponse[Page[AuditEventRead]])
def list_audit_events(
    principal: Auditor,
    db: Annotated[Session, Depends(get_db)],
    page: Annotated[PageParams, Depends(page_params)],
    action: Annotated[str | None, Query(max_length=100)] = None,
    resource_type: Annotated[str | None, Query(max_length=50)] = None,
    resource_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    since: datetime | None = None,
):
    items, total = AuditRepository(db).list(
        tenant_id(principal),
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        actor_user_id=actor_user_id,
        since=since,
        limit=page.limit,
        offset=page.offset,
    )
    return ok(Page(items=[AuditEventRead.model_validate(e) for e in items], total=total, **page.model_dump()))
