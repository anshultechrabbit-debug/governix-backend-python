import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import get_db
from app.core.dependencies import get_app_settings, get_queue, get_storage
from app.core.responses import ApiResponse, ok
from app.infrastructure.queue.base import Queue
from app.infrastructure.storage.base import Storage
from app.modules.auth.dependencies import require
from app.modules.auth.permissions import Permission, Principal
from app.modules.uploads.schema import BatchCreate, BatchItemRead, BatchRead, BatchSummary, PolicySuggestion
from app.modules.uploads.service import UploadBatchService
from app.modules.uploads.suggest import suggest_policy
from app.workers.runtime import get_runtime

router = APIRouter(prefix="/uploads", tags=["bulk upload"])

Uploader = Annotated[Principal, Depends(require(Permission.DOCUMENTS_UPLOAD))]


def get_service(
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[Storage, Depends(get_storage)],
    queue: Annotated[Queue, Depends(get_queue)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> UploadBatchService:
    return UploadBatchService(db, storage, queue, settings)


Service = Annotated[UploadBatchService, Depends(get_service)]


@router.post("/suggestions", response_model=ApiResponse[PolicySuggestion])
def suggest(
    principal: Uploader, db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)], file: Annotated[UploadFile, File()],
    policy_id: Annotated[uuid.UUID | None, Form()] = None,
):
    """Read a file's opening pages while uploads are being arranged: the policy name the
    document states, its category, and an existing policy it most likely is a version of.

    `policy_id` is the policy the file is meant for: when the file matches it, it is the
    one reported, even if another policy matches too. Nothing is stored; the file is
    uploaded for real once the arrangement is sent.
    """
    runtime = get_runtime()
    return ok(suggest_policy(
        db, principal, settings, file.file, intended_policy_id=policy_id, llm_factory=lambda: runtime.llm,
    ))


@router.post("/batches", response_model=ApiResponse[BatchRead], status_code=201)
def create_batch(body: BatchCreate, principal: Uploader, service: Service):
    """Plan a bulk upload: policies (new or existing) and each one's files in version order, oldest first."""
    batch = service.create(principal, body)
    return ok(service.get(principal, batch.id))


@router.post("/batches/{batch_id}/items/{item_id}/file", response_model=ApiResponse[BatchItemRead])
def upload_item(
    batch_id: uuid.UUID, item_id: uuid.UUID, principal: Uploader, service: Service,
    file: Annotated[UploadFile, File()],
    allow_duplicate: Annotated[bool, Form()] = False,
    duplicate_reason: Annotated[str | None, Form(max_length=1000)] = None,
):
    """Send one planned file. A file that cannot be accepted is marked failed; the upload continues.

    A file identical to an existing document is refused unless `allow_duplicate`
    is set with a reason (recorded in the audit log).
    """
    item = service.upload_item(
        principal, batch_id, item_id, file=file.file, filename=file.filename or "", content_type=file.content_type,
        allow_duplicate=allow_duplicate, duplicate_reason=duplicate_reason,
    )
    return ok(_item_read(service, principal, batch_id, item.id))


@router.post("/batches/{batch_id}/items/{item_id}/cancel", response_model=ApiResponse[BatchItemRead])
def cancel_item(batch_id: uuid.UUID, item_id: uuid.UUID, request: Request, principal: Uploader, service: Service):
    """Cancel a planned file that has not been sent yet."""
    item = service.cancel_item(principal, batch_id, item_id, request.app.state.session_factory)
    return ok(_item_read(service, principal, batch_id, item.id))


def _item_read(service: UploadBatchService, principal: Principal, batch_id: uuid.UUID, item_id: uuid.UUID) -> BatchItemRead:
    return next(i for g in service.get(principal, batch_id).groups for i in g.items if i.id == item_id)


@router.post("/batches/{batch_id}/start", response_model=ApiResponse[BatchRead])
def start_batch(batch_id: uuid.UUID, request: Request, principal: Uploader, service: Service):
    """All files sent: stop waiting for missing ones and confirm every group whose files are analysed."""
    service.start(principal, batch_id, request.app.state.session_factory)
    return ok(service.get(principal, batch_id))


@router.get("/batches", response_model=ApiResponse[list[BatchSummary]])
def list_batches(principal: Uploader, service: Service):
    return ok(service.list(principal))


@router.get("/batches/{batch_id}", response_model=ApiResponse[BatchRead])
def get_batch(batch_id: uuid.UUID, principal: Uploader, service: Service):
    return ok(service.get(principal, batch_id))
