import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.auth.acl import visible_clause
from app.modules.auth.permissions import Principal
from app.modules.documents.model import Document, DocumentStatus

ACTIVE_STATUSES = frozenset(
    set(DocumentStatus) - {DocumentStatus.FAILED, DocumentStatus.REJECTED}
)


def document_visible(principal: Principal):
    return visible_clause(
        principal, Document.organization_id, Document.branch_id, Document.department_id, Document.policy_id
    )


class DocumentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, document_id: uuid.UUID) -> Document | None:
        return self.session.get(Document, document_id)

    def get_visible(self, principal: Principal, document_id: uuid.UUID) -> Document | None:
        return self.session.scalar(
            select(Document).where(Document.id == document_id, document_visible(principal))
        )

    def find_by_file_hash(
        self, organization_id: uuid.UUID, sha256: str, *, exclude_id: uuid.UUID | None = None
    ) -> Document | None:
        query = select(Document).where(
            Document.organization_id == organization_id,
            Document.file_sha256 == sha256,
            Document.status.in_(ACTIVE_STATUSES),
        )
        if exclude_id is not None:
            query = query.where(Document.id != exclude_id)
        return self.session.scalar(query.order_by(Document.created_at).limit(1))

    def list(
        self,
        principal: Principal,
        *,
        status: str | None,
        category_id: uuid.UUID | None,
        policy_id: uuid.UUID | None,
        branch_id: uuid.UUID | None,
        department_id: uuid.UUID | None,
        search: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[Document], int]:
        query = select(Document).where(document_visible(principal))
        if status:
            query = query.where(Document.status == status)
        if category_id:
            query = query.where(Document.category_id == category_id)
        if policy_id:
            query = query.where(Document.policy_id == policy_id)
        if branch_id:
            query = query.where(Document.branch_id == branch_id)
        if department_id:
            query = query.where(Document.department_id == department_id)
        if search:
            pattern = f"%{search.lower()}%"
            query = query.where(
                func.lower(func.coalesce(Document.title, "")).like(pattern)
                | func.lower(Document.original_filename).like(pattern)
            )
        total = self.session.scalar(select(func.count()).select_from(query.subquery()))
        items = self.session.scalars(
            query.order_by(Document.created_at.desc()).limit(limit).offset(offset)
        ).all()
        return list(items), total or 0
