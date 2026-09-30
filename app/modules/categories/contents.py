"""What a category holds: documents (business identities), their versions, and uploads in progress.

Counts and listings only ever cover what the caller can see (branch and
department scope), so a category's numbers differ between people.
"""

import uuid
from datetime import date

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.modules.auth.acl import visible_clause
from app.modules.auth.permissions import Principal
from app.modules.categories.model import Category
from app.modules.categories.schema import (
    CategoryDocument,
    CategoryRead,
    CategorySummary,
    CategoryVersion,
    PendingUpload,
)
from app.modules.documents.model import Document, DocumentStatus
from app.modules.policies.model import Policy, PolicyVersion, VersionStatus
from app.modules.policies.repository import arranged_policy_order, policy_visible
from app.modules.policies.schema import VersionRead
from app.modules.users.model import User
from app.modules.versions.timeline import effective_on, timeline_state

VERSION_STATES = ("current", "historical", "scheduled", "withdrawn")
SORTS = ("custom", "name", "uploaded", "version")
# Uploaded into the category but not (yet) a version of a document.
PENDING_EXCLUDED = (DocumentStatus.REJECTED, DocumentStatus.ARCHIVED)
MAX_PENDING = 100


def summaries(session: Session, principal: Principal, categories: list[Category]) -> list[CategorySummary]:
    if not categories:
        return []
    rows = session.execute(
        select(
            Policy.category_id,
            func.count(func.distinct(Policy.id)),
            func.max(Policy.updated_at),
            func.max(PolicyVersion.updated_at),
        )
        .outerjoin(PolicyVersion, PolicyVersion.policy_id == Policy.id)
        .where(Policy.category_id.in_([c.id for c in categories]), policy_visible(principal))
        .group_by(Policy.category_id)
    ).all()
    stats = {row[0]: row[1:] for row in rows}
    result = []
    for category in categories:
        count, policy_activity, version_activity = stats.get(category.id, (0, None, None))
        result.append(CategorySummary(
            **CategoryRead.model_validate(category).model_dump(),
            document_count=count,
            last_updated=max(t for t in (category.updated_at, policy_activity, version_activity) if t is not None),
        ))
    return result


def _state_clause(state: str, as_of: date):
    """SQL mirror of versions.timeline.timeline_state."""
    if state == "withdrawn":
        return PolicyVersion.status == VersionStatus.WITHDRAWN
    active = PolicyVersion.status == VersionStatus.ACTIVE
    if state == "scheduled":
        return and_(active, PolicyVersion.effective_from > as_of)
    if state == "historical":
        return and_(active, PolicyVersion.effective_to.is_not(None), PolicyVersion.effective_to <= as_of)
    return and_(active, effective_on(as_of))


def _contains(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def documents(
    session: Session,
    principal: Principal,
    category: Category,
    *,
    as_of: date,
    search: str | None,
    status: str | None,
    version_state: str | None,
    sort: str,
    limit: int,
    offset: int,
) -> tuple[list[CategoryDocument], int]:
    query = select(Policy).where(Policy.category_id == category.id, policy_visible(principal))
    if status:
        query = query.where(Policy.status == status)
    if search and search.strip():
        pattern = _contains(search.strip())
        file_matches = (
            select(PolicyVersion.id)
            .join(Document, Document.id == PolicyVersion.document_id)
            .where(
                PolicyVersion.policy_id == Policy.id,
                or_(Document.original_filename.ilike(pattern, escape="\\"), Document.title.ilike(pattern, escape="\\")),
            )
            .exists()
        )
        query = query.where(or_(
            Policy.name.ilike(pattern, escape="\\"), Policy.policy_number.ilike(pattern, escape="\\"), file_matches,
        ))
    if version_state:
        query = query.where(
            select(PolicyVersion.id).where(PolicyVersion.policy_id == Policy.id, _state_clause(version_state, as_of)).exists()
        )
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0

    latest_upload = (
        select(func.max(Document.created_at))
        .join(PolicyVersion, PolicyVersion.document_id == Document.id)
        .where(PolicyVersion.policy_id == Policy.id)
        .scalar_subquery()
    )
    highest_version = select(func.max(PolicyVersion.version_number)).where(PolicyVersion.policy_id == Policy.id).scalar_subquery()
    order = {
        "custom": arranged_policy_order(),
        "name": (func.lower(Policy.name), Policy.id),
        "uploaded": (latest_upload.desc().nulls_last(), Policy.id),
        "version": (highest_version.desc().nulls_last(), func.lower(Policy.name), Policy.id),
    }[sort]
    policies = session.scalars(query.order_by(*order).limit(limit).offset(offset)).all()
    if not policies:
        return [], total
    by_policy = versions_with_files(session, [p.id for p in policies], as_of)
    return _policy_items(policies, by_policy, sort, version_state), total


def versions_with_files(session: Session, policy_ids: list[uuid.UUID], as_of: date) -> dict[uuid.UUID, list[CategoryVersion]]:
    """Every version of these policies (newest first) with the file behind it and who uploaded it."""
    rows = session.execute(
        select(PolicyVersion, Document, User.full_name)
        .join(Document, Document.id == PolicyVersion.document_id)
        .outerjoin(User, User.id == Document.uploaded_by_id)
        .where(PolicyVersion.policy_id.in_(policy_ids))
        .order_by(PolicyVersion.policy_id, PolicyVersion.effective_from.desc(), PolicyVersion.version_number.desc())
    ).all()
    by_policy: dict[uuid.UUID, list[CategoryVersion]] = {}
    for version, document, uploader in rows:
        read = CategoryVersion(
            **VersionRead.model_validate(version).model_dump(exclude={"timeline_state"}),
            timeline_state=timeline_state(version, as_of),
            filename=document.original_filename, title=document.title, content_type=document.content_type,
            size_bytes=document.size_bytes, page_count=document.page_count, document_status=document.status,
            uploaded_at=document.created_at, uploaded_by_id=document.uploaded_by_id, uploaded_by_name=uploader,
        )
        by_policy.setdefault(version.policy_id, []).append(read)
    return by_policy


def _policy_items(policies, by_policy, sort: str, version_state: str | None) -> list[CategoryDocument]:
    items = []
    for policy in policies:
        versions = by_policy.get(policy.id, [])
        if sort == "uploaded":
            versions.sort(key=lambda v: v.uploaded_at, reverse=True)
        elif sort == "version":
            versions.sort(key=lambda v: v.version_number, reverse=True)
        items.append(CategoryDocument(
            id=policy.id, name=policy.name, policy_number=policy.policy_number, description=policy.description,
            status=policy.status, branch_id=policy.branch_id, department_id=policy.department_id,
            display_order=policy.display_order, created_at=policy.created_at, updated_at=policy.updated_at,
            version_count=sum(1 for v in versions if v.status == VersionStatus.ACTIVE),
            current_version_id=next((v.id for v in versions if v.timeline_state == "current"), None),
            latest_upload_at=max((v.uploaded_at for v in versions), default=None),
            # With a version filter, show only the versions that match it.
            versions=[v for v in versions if version_state is None or v.timeline_state == version_state],
        ))
    return items


def pending_uploads(session: Session, principal: Principal, category: Category) -> list[PendingUpload]:
    rows = session.execute(
        select(Document, User.full_name)
        .outerjoin(User, User.id == Document.uploaded_by_id)
        .where(
            Document.category_id == category.id,
            Document.policy_id.is_(None),
            Document.status.not_in(PENDING_EXCLUDED),
            visible_clause(principal, Document.organization_id, Document.branch_id, Document.department_id, Document.policy_id),
        )
        .order_by(Document.created_at.desc())
        .limit(MAX_PENDING)
    ).all()
    return [
        PendingUpload(
            document_id=document.id, filename=document.original_filename, title=document.title,
            status=document.status, size_bytes=document.size_bytes, uploaded_at=document.created_at,
            uploaded_by_name=uploader, error=(document.error or {}).get("message"),
        )
        for document, uploader in rows
    ]
