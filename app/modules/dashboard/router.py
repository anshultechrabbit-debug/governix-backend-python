import uuid
from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.responses import ApiResponse, ok
from app.modules.audit.model import AuditEvent
from app.modules.auth.dependencies import require
from app.modules.auth.permissions import Permission, Principal, Role
from app.modules.documents.model import Document, DocumentStatus
from app.modules.documents.repository import document_visible
from app.modules.ingestion.model import Decision, DocumentAnalysis, ReviewStatus
from app.modules.policies.model import Policy, PolicyStatus, PolicyVersion, VersionStatus
from app.modules.policies.repository import policy_visible

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

Reader = Annotated[Principal, Depends(require(Permission.DOCUMENTS_READ))]


class RecentChange(BaseModel):
    policy_id: uuid.UUID
    policy_name: str
    version_id: uuid.UUID
    version_label: str
    effective_from: date
    effective_date_source: str = "entered"
    confirmed_at: datetime | None


class RecentQuery(BaseModel):
    id: uuid.UUID
    question: str
    status: str | None
    created_at: datetime


class Dashboard(BaseModel):
    counts: dict[str, int]
    recent_changes: list[RecentChange]
    recent_queries: list[RecentQuery]
    attention: list[dict[str, Any]]


@router.get("", response_model=ApiResponse[Dashboard])
def dashboard(principal: Reader, db: Annotated[Session, Depends(get_db)]):
    visible_docs = document_visible(principal)
    by_status = dict(db.execute(
        select(Document.status, func.count()).where(visible_docs).group_by(Document.status)
    ).all())
    processing = sum(by_status.get(s, 0) for s in (DocumentStatus.UPLOADED, DocumentStatus.PROCESSING, DocumentStatus.INDEXING))
    pending = db.execute(
        select(DocumentAnalysis.decision, func.count())
        .join(Document, Document.id == DocumentAnalysis.document_id)
        .where(visible_docs, DocumentAnalysis.review_status == ReviewStatus.PENDING,
               Document.status == DocumentStatus.AWAITING_CONFIRMATION)
        .group_by(DocumentAnalysis.decision)
    ).all()
    pending = dict(pending)
    active_policies = db.scalar(
        select(func.count()).select_from(Policy).where(policy_visible(principal), Policy.status == PolicyStatus.ACTIVE)
    ) or 0

    changes = db.execute(
        select(PolicyVersion, Policy)
        .join(Policy, Policy.id == PolicyVersion.policy_id)
        .where(policy_visible(principal), PolicyVersion.status == VersionStatus.ACTIVE)
        .order_by(PolicyVersion.created_at.desc())
        .limit(8)
    ).all()

    queries = select(AuditEvent).where(AuditEvent.action == "ai.query")
    if principal.role is Role.ORG_ADMIN:
        queries = queries.where(AuditEvent.organization_id == principal.organization_id)
    else:
        queries = queries.where(AuditEvent.actor_user_id == principal.user_id)
    recent_queries = db.scalars(queries.order_by(AuditEvent.created_at.desc()).limit(8)).all()

    attention = [
        {"id": str(d.id), "title": d.title or d.original_filename, "status": d.status,
         "reason": (d.error or {}).get("message") if d.status == DocumentStatus.FAILED else "Awaiting confirmation"}
        for d in db.scalars(
            select(Document).where(visible_docs, Document.status.in_([DocumentStatus.FAILED, DocumentStatus.AWAITING_CONFIRMATION]))
            .order_by(Document.updated_at.desc()).limit(10)
        )
    ]
    return ok(Dashboard(
        counts={
            "documents": sum(v for k, v in by_status.items() if k not in (DocumentStatus.REJECTED,)),
            "ready_documents": by_status.get(DocumentStatus.READY, 0),
            "active_policies": active_policies,
            "processing": processing,
            "awaiting_confirmation": by_status.get(DocumentStatus.AWAITING_CONFIRMATION, 0),
            "failed": by_status.get(DocumentStatus.FAILED, 0),
            "version_conflicts": pending.get(Decision.VERSION_CONFLICT, 0),
            "requires_review": sum(pending.values()),
        },
        recent_changes=[
            RecentChange(policy_id=p.id, policy_name=p.name, version_id=v.id, version_label=v.version_label,
                         effective_from=v.effective_from, effective_date_source=v.effective_date_source,
                         confirmed_at=v.confirmed_at)
            for v, p in changes
        ],
        recent_queries=[
            RecentQuery(id=e.id, question=e.details.get("question", ""), status=e.details.get("status"),
                        created_at=e.created_at)
            for e in recent_queries
        ],
        attention=attention,
    ))
