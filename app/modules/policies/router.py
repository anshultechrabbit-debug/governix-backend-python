import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_queue, get_storage
from app.infrastructure.storage.base import Storage
from app.infrastructure.queue.base import Queue
from app.core.pagination import Page, PageParams, page_params
from app.core.responses import ApiResponse, ok
from app.modules.auth.dependencies import require
from app.modules.auth.permissions import Permission, Principal
from app.modules.policies.schema import (
    DocumentMove,
    PolicyCreate,
    PolicyDetail,
    PolicyRead,
    PolicyUpdate,
    RelationshipCreate,
    RelationshipRead,
    VersionDetail,
    VersionMove,
    VersionOrder,
    VersionRead,
    WithdrawRequest,
)
from app.modules.categories.contents import versions_with_files
from app.modules.categories.schema import CategoryVersion
from app.modules.policies.service import PolicyService, today, version_read
from app.modules.versions.summary import SUMMARIZE, summary_view

router = APIRouter(tags=["policies"])

Reader = Annotated[Principal, Depends(require(Permission.POLICIES_READ))]
Manager = Annotated[Principal, Depends(require(Permission.POLICIES_MANAGE))]


def get_service(db: Annotated[Session, Depends(get_db)]) -> PolicyService:
    return PolicyService(db)


Service = Annotated[PolicyService, Depends(get_service)]


@router.get("/policies", response_model=ApiResponse[Page[PolicyRead]])
def list_policies(
    principal: Reader,
    service: Service,
    page: Annotated[PageParams, Depends(page_params)],
    search: Annotated[str | None, Query(max_length=200)] = None,
    category_id: uuid.UUID | None = None,
    status: Annotated[str | None, Query(pattern="^(active|archived)$")] = None,
    as_of: date | None = None,
):
    items, total = service.list_policies(
        principal, as_of=as_of, search=search, category_id=category_id, status=status,
        limit=page.limit, offset=page.offset,
    )
    return ok(Page(items=items, total=total, **page.model_dump()))


@router.post("/policies", response_model=ApiResponse[PolicyRead], status_code=201)
def create_policy(body: PolicyCreate, principal: Manager, service: Service):
    """Create a policy by hand (global or branch); upload its documents into it afterwards."""
    return ok(PolicyRead.model_validate(service.create(principal, body)))


@router.get("/policies/{policy_id}", response_model=ApiResponse[PolicyDetail])
def get_policy(policy_id: uuid.UUID, principal: Reader, service: Service):
    return ok(service.detail(principal, policy_id))


@router.patch("/policies/{policy_id}", response_model=ApiResponse[PolicyRead])
def update_policy(policy_id: uuid.UUID, body: PolicyUpdate, principal: Manager, service: Service):
    return ok(PolicyRead.model_validate(service.update(principal, policy_id, body)))


@router.get("/policies/{policy_id}/versions", response_model=ApiResponse[list[CategoryVersion]])
def policy_versions(policy_id: uuid.UUID, principal: Reader, service: Service):
    """Every version, newest first, with the file behind it and who uploaded it."""
    policy = service.get(principal, policy_id)
    return ok(versions_with_files(service.session, [policy.id], today()).get(policy.id, []))


@router.get("/policies/{policy_id}/versions/{version_id}", response_model=ApiResponse[VersionDetail])
def get_version(policy_id: uuid.UUID, version_id: uuid.UUID, principal: Reader, service: Service):
    version = service.version(principal, policy_id, version_id)
    detail = VersionDetail.model_validate(version)
    detail.timeline_state = version_read(version, today()).timeline_state
    return ok(detail)


@router.get("/policies/{policy_id}/compare", response_model=ApiResponse[dict])
def compare_versions(
    policy_id: uuid.UUID, base: uuid.UUID, target: uuid.UUID, principal: Reader, service: Service
):
    """Deterministic side-by-side comparison; original text of both versions is included."""
    return ok(service.compare(principal, policy_id, base, target))


@router.post("/policies/{policy_id}/versions/{version_id}/withdraw", response_model=ApiResponse[VersionDetail])
def withdraw_version(
    policy_id: uuid.UUID, version_id: uuid.UUID, body: WithdrawRequest, principal: Manager, service: Service
):
    return ok(VersionDetail.model_validate(service.withdraw(principal, policy_id, version_id, body.reason)))


@router.post("/policies/{policy_id}/versions/{version_id}/restore", response_model=ApiResponse[VersionDetail])
def restore_version(policy_id: uuid.UUID, version_id: uuid.UUID, principal: Manager, service: Service):
    """Activate an archived version: it rejoins the effective-date timeline."""
    version = service.restore(principal, policy_id, version_id)
    detail = VersionDetail.model_validate(version)
    detail.timeline_state = version_read(version, today()).timeline_state
    return ok(detail)


@router.put("/policies/{policy_id}/versions/order", response_model=ApiResponse[PolicyDetail])
def reorder_versions(policy_id: uuid.UUID, body: VersionOrder, principal: Manager, service: Service):
    """Drag-and-drop version order, newest first: it becomes the timeline and the first is the latest.

    When the version in force changes, the request must confirm it
    (409 LATEST_CHANGE_REQUIRES_CONFIRMATION otherwise). Dates stated in a
    document or entered by a person are never moved (409 VERSION_ORDER_CONFLICT).
    """
    service.reorder(principal, policy_id, body.version_ids, confirm_latest_change=body.confirm_latest_change)
    return ok(service.detail(principal, policy_id))


@router.post("/policies/{policy_id}/versions/{version_id}/move", response_model=ApiResponse[VersionDetail])
def move_version(policy_id: uuid.UUID, version_id: uuid.UUID, body: VersionMove, principal: Manager, service: Service):
    """Move a document to another policy's version group (requires confirm=true)."""
    version = service.move_version(principal, policy_id, version_id, body.target_policy_id, confirm=body.confirm)
    detail = VersionDetail.model_validate(version)
    detail.timeline_state = version_read(version, today()).timeline_state
    return ok(detail)


@router.get("/policies/{policy_id}/versions/{version_id}/summary", response_model=ApiResponse[dict])
def version_summary(policy_id: uuid.UUID, version_id: uuid.UUID, principal: Reader, service: Service):
    """AI summary of the version (labelled AI-generated) with dates and changes from the database."""
    return ok(summary_view(service.version(principal, policy_id, version_id)))


@router.post("/policies/{policy_id}/versions/{version_id}/summary", response_model=ApiResponse[dict], status_code=202)
def regenerate_summary(
    policy_id: uuid.UUID, version_id: uuid.UUID, principal: Manager, service: Service,
    queue: Annotated[Queue, Depends(get_queue)],
):
    version = service.version(principal, policy_id, version_id)
    queue.enqueue(SUMMARIZE, {"version_id": str(version.id)}, organization_id=version.organization_id)
    return ok(summary_view(version))


@router.delete("/policies/{policy_id}", response_model=ApiResponse[dict])
def delete_policy(
    policy_id: uuid.UUID, principal: Manager, service: Service, storage: Annotated[Storage, Depends(get_storage)],
):
    """Delete a policy permanently, with every version, document and stored file."""
    return ok(service.delete(principal, policy_id, storage))


@router.post("/policies/{policy_id}/move", response_model=ApiResponse[PolicyRead])
def move_policy(policy_id: uuid.UUID, body: DocumentMove, principal: Manager, service: Service):
    """Arrange a document within its category."""
    return ok(PolicyRead.model_validate(service.move(principal, policy_id, after_id=body.after_id, before_id=body.before_id)))


@router.post("/documents/{document_id}/relationships", response_model=ApiResponse[RelationshipRead], status_code=201)
def add_relationship(document_id: uuid.UUID, body: RelationshipCreate, principal: Manager, service: Service):
    return ok(RelationshipRead.model_validate(service.add_relationship(principal, document_id, body)))
