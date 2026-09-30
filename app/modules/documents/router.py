import uuid
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import get_db
from app.core.dependencies import get_app_settings, get_queue, get_storage
from app.core.pagination import Page, PageParams, page_params
from app.core.responses import ApiResponse, ok
from app.infrastructure.queue.base import Queue
from app.infrastructure.storage.base import CHUNK_SIZE, Storage
from app.modules.auth.dependencies import require
from app.modules.auth.permissions import Permission, Principal
from app.core.exceptions import NotFoundError
from app.modules.audit.service import record_event
from app.modules.documents import viewer
from app.modules.documents.schema import (
    ChunkRead,
    DocumentDetail,
    DocumentRead,
    DocumentUpdate,
    DuplicateCheck,
    DuplicateResult,
    UploadLimits,
    OutlineEntry,
    PageText,
    Progress,
)
from app.modules.search.model import Chunk
from app.modules.documents.service import DocumentService
from app.modules.ingestion import progress as progress_module

router = APIRouter(prefix="/documents", tags=["documents"])

Reader = Annotated[Principal, Depends(require(Permission.DOCUMENTS_READ))]
Uploader = Annotated[Principal, Depends(require(Permission.DOCUMENTS_UPLOAD))]


def get_service(
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[Storage, Depends(get_storage)],
    queue: Annotated[Queue, Depends(get_queue)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> DocumentService:
    return DocumentService(db, storage, queue, settings)


Service = Annotated[DocumentService, Depends(get_service)]


def _detail(service: DocumentService, document) -> DocumentDetail:
    detail = DocumentDetail.model_validate(document)
    detail.progress = Progress.model_validate(progress_module.snapshot(service.session, document.id))
    return detail


@router.post("", response_model=ApiResponse[DocumentDetail], status_code=201)
def upload_document(
    principal: Uploader,
    service: Service,
    file: Annotated[UploadFile, File()],
    branch_id: Annotated[uuid.UUID | None, Form()] = None,
    department_id: Annotated[uuid.UUID | None, Form()] = None,
    category_id: Annotated[uuid.UUID | None, Form()] = None,
    allow_duplicate: Annotated[bool, Form()] = False,
    duplicate_reason: Annotated[str | None, Form(max_length=1000)] = None,
):
    document = service.upload(
        principal,
        file=file.file,
        filename=file.filename or "document.pdf",
        content_type=file.content_type,
        branch_id=branch_id,
        department_id=department_id,
        category_id=category_id,
        allow_duplicate=allow_duplicate,
        duplicate_reason=duplicate_reason,
    )
    return ok(_detail(service, document))


@router.get("/upload-limits", response_model=ApiResponse[UploadLimits])
def upload_limits(principal: Uploader, settings: Annotated[Settings, Depends(get_app_settings)]):
    """What the server accepts, so files can be checked before they are sent."""
    return ok(UploadLimits(
        max_size_bytes=settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024,
        accepted_extensions=[".pdf"], accepted_types=["application/pdf"],
    ))


@router.post("/duplicates", response_model=ApiResponse[list[DuplicateResult]])
def check_duplicates(body: DuplicateCheck, principal: Uploader, service: Service):
    """Which files (SHA-256 computed in the browser) already exist, before anything is uploaded."""
    return ok(service.check_duplicates(principal, body.sha256))


@router.get("", response_model=ApiResponse[Page[DocumentRead]])
def list_documents(
    principal: Reader,
    service: Service,
    page: Annotated[PageParams, Depends(page_params)],
    status: str | None = None,
    category_id: uuid.UUID | None = None,
    policy_id: uuid.UUID | None = None,
    branch_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
):
    items, total = service.repository.list(
        principal, status=status, category_id=category_id, policy_id=policy_id,
        branch_id=branch_id, department_id=department_id, search=search,
        limit=page.limit, offset=page.offset,
    )
    return ok(Page(items=[DocumentRead.model_validate(d) for d in items], total=total, **page.model_dump()))


@router.get("/{document_id}", response_model=ApiResponse[DocumentDetail])
def get_document(document_id: uuid.UUID, principal: Reader, service: Service):
    return ok(_detail(service, service.get(principal, document_id)))


@router.get("/{document_id}/progress", response_model=ApiResponse[Progress])
def get_progress(document_id: uuid.UUID, principal: Reader, service: Service):
    return ok(Progress.model_validate(service.progress(principal, document_id)))


@router.get("/{document_id}/file")
def download_file(document_id: uuid.UUID, principal: Reader, service: Service):
    document, handle_cm = service.open_file(principal, document_id)

    def stream():
        with handle_cm as handle:
            while chunk := handle.read(CHUNK_SIZE):
                yield chunk

    filename = quote(document.original_filename)
    return StreamingResponse(
        stream(),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{filename}",
            "Content-Length": str(document.size_bytes),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


@router.patch("/{document_id}", response_model=ApiResponse[DocumentRead])
def update_document(document_id: uuid.UUID, body: DocumentUpdate, principal: Uploader, service: Service):
    return ok(DocumentRead.model_validate(service.update(principal, document_id, body)))


@router.post("/{document_id}/archive", response_model=ApiResponse[DocumentRead])
def archive_document(document_id: uuid.UUID, principal: Uploader, service: Service):
    return ok(DocumentRead.model_validate(service.archive(principal, document_id)))


@router.post("/{document_id}/retry", response_model=ApiResponse[DocumentRead])
def retry_document(document_id: uuid.UUID, principal: Uploader, service: Service):
    return ok(DocumentRead.model_validate(service.retry(principal, document_id)))


@router.get("/{document_id}/pages/{page_number}/image")
def page_image(
    document_id: uuid.UUID,
    page_number: int,
    principal: Reader,
    service: Service,
    storage: Annotated[Storage, Depends(get_storage)],
    zoom: Annotated[float, Query()] = 1.5,
    highlight_chunk: uuid.UUID | None = None,
    audit: bool = False,
):
    """PNG of one page; `highlight_chunk` marks a cited passage on the page."""
    document = service.get(principal, document_id)
    png, highlights = viewer.render_page(
        service.session, storage, document, page_number, zoom=zoom, highlight_chunk_id=highlight_chunk
    )
    if audit:  # the viewer sets this once when a document is opened
        record_event(service.session, "document.viewed", actor=principal, resource_type="document",
                     resource_id=document.id, details={"page": page_number})
        service.session.commit()
    return Response(png, media_type="image/png", headers={
        "Cache-Control": "private, max-age=300", "X-Highlights": str(highlights),
    })


@router.get("/{document_id}/pages/{page_number}", response_model=ApiResponse[PageText])
def get_page_text(document_id: uuid.UUID, page_number: int, principal: Reader, service: Service):
    page = viewer.page_text(service.session, service.get(principal, document_id), page_number)
    return ok(PageText(page_number=page.page_number, text=page.text, method=page.method,
                       width=page.width, height=page.height))


@router.get("/{document_id}/outline", response_model=ApiResponse[list[OutlineEntry]])
def get_outline(document_id: uuid.UUID, principal: Reader, service: Service):
    document = service.get(principal, document_id)
    return ok([OutlineEntry.model_validate(s) for s in viewer.outline(service.session, document)])


@router.get("/{document_id}/chunks/{chunk_id}", response_model=ApiResponse[ChunkRead])
def get_chunk(document_id: uuid.UUID, chunk_id: uuid.UUID, principal: Reader, service: Service):
    document = service.get(principal, document_id)
    chunk = service.session.get(Chunk, chunk_id)
    if chunk is None or chunk.document_id != document.id:
        raise NotFoundError("Passage not found.")
    return ok(ChunkRead.model_validate(chunk))
