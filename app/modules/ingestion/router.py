import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.responses import ApiResponse, ok
from app.modules.auth.dependencies import require
from app.modules.auth.permissions import Permission, Principal
from app.core.dependencies import get_queue
from app.infrastructure.queue.base import Queue
from app.modules.documents.schema import DocumentRead
from app.modules.ingestion.confirmation import ConfirmationService, ConfirmRequest
from app.modules.ingestion.schema import AnalysisRead
from app.modules.ingestion.service import AnalysisService

router = APIRouter(prefix="/documents", tags=["document intelligence"])

Reader = Annotated[Principal, Depends(require(Permission.DOCUMENTS_READ))]
PolicyManager = Annotated[Principal, Depends(require(Permission.POLICIES_MANAGE))]


@router.get("/{document_id}/analysis", response_model=ApiResponse[AnalysisRead])
def get_analysis(document_id: uuid.UUID, principal: Reader, db: Annotated[Session, Depends(get_db)]):
    return ok(AnalysisService(db).get(principal, document_id))


@router.post("/{document_id}/confirm", response_model=ApiResponse[DocumentRead])
def confirm_document(
    document_id: uuid.UUID,
    body: ConfirmRequest,
    principal: PolicyManager,
    db: Annotated[Session, Depends(get_db)],
    queue: Annotated[Queue, Depends(get_queue)],
):
    """Accept, adjust or reject the system's suggestion for an uploaded document."""
    return ok(DocumentRead.model_validate(ConfirmationService(db, queue).confirm(principal, document_id, body)))
