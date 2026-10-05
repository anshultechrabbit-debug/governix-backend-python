import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy.orm import Session

from app.infrastructure.queue.registry import JobContext, RetryLater
from app.modules.documents.model import Document, DocumentStatus
from app.modules.documents.service import mark_failed
from app.modules.ingestion import progress
from app.modules.ingestion.model import Stage, StageStatus

logger = logging.getLogger(__name__)

_TERMINAL = {DocumentStatus.ARCHIVED, DocumentStatus.REJECTED, DocumentStatus.FAILED}


class PipelineError(Exception):
    """A permanent, user-explainable processing failure (no retry)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def load_document(session: Session, payload: dict[str, Any], *, lock: bool = False) -> Document | None:
    """The document for this job, or None if the job is stale (retried/archived since)."""
    document = session.get(Document, uuid.UUID(payload["document_id"]), with_for_update=lock)
    if document is None or document.status in _TERMINAL:
        return None
    if document.ingestion_attempt != payload.get("attempt"):
        return None
    return document


@contextmanager
def failure_guard(
    session: Session, document_id: uuid.UUID, stage: Stage, ctx: JobContext
) -> Iterator[None]:
    """Turn permanent errors (and the last retry) into a FAILED document with a clear reason."""
    try:
        yield
    except PipelineError as exc:
        session.rollback()
        _fail(session, document_id, stage, exc.code, exc.message)
    except RetryLater:
        session.rollback()
        raise
    except Exception:
        if ctx.is_final_attempt:
            session.rollback()
            _fail(
                session, document_id, stage, "PROCESSING_FAILED",
                f"Processing failed at the {stage} stage after {ctx.attempt} attempts.",
            )
        raise


def _fail(session: Session, document_id: uuid.UUID, stage: Stage, code: str, message: str) -> None:
    document = session.get(Document, document_id)
    if document is None:
        return
    if document.status == DocumentStatus.READY:
        # Embedding finishes after the document is searchable by keyword; it stays so, and only
        # the stage fails (resumed by the recovery sweep, see ingestion.recovery).
        logger.warning("Document %s: %s stage failed: %s", document_id, stage, code)
        progress.finish(session, document_id, stage, status=StageStatus.FAILED,
                        detail={"error": message, "code": code})
        session.commit()
        return
    logger.warning("Document %s failed at %s: %s", document_id, stage, code)
    mark_failed(session, document, stage, code, message)
    session.commit()
    from app.modules.uploads.hooks import document_settled
    from app.workers.runtime import get_runtime

    runtime = get_runtime()
    document_settled(runtime.session_factory, runtime.queue, document_id)
