"""Ingestion pipeline wiring: which job runs after which.

    inspect ──► extract_range × N (parallel page ranges) ──► [ocr_range × K] ──► structure ──► analyze
        ──► [person confirms] ──► chunk ──► embed_batch × M (parallel) ──► verify ──► READY

Every job is idempotent and keyed by (document, attempt, step), so duplicate
enqueues collapse and retries after a crash are safe.
"""

from sqlalchemy.orm import Session

from app.infrastructure.queue.base import Queue
from app.modules.documents.model import Document
from app.modules.ingestion.model import Stage

INSPECT = "ingestion.inspect"
EXTRACT_RANGE = "ingestion.extract_range"
OCR_RANGE = "ingestion.ocr_range"
STRUCTURE = "ingestion.structure"
ANALYZE = "ingestion.analyze"
CHUNK = "ingestion.chunk"
EMBED_BATCH = "ingestion.embed_batch"
VERIFY = "ingestion.verify"

_RESUME_TASK = {
    Stage.UPLOAD: INSPECT,
    Stage.VALIDATION: INSPECT,
    Stage.EXTRACTION: INSPECT,
    Stage.OCR: INSPECT,
    Stage.STRUCTURE: STRUCTURE,
    Stage.ANALYSIS: ANALYZE,
    Stage.CONFIRMATION: ANALYZE,
    Stage.CHUNKING: CHUNK,
    Stage.EMBEDDING: CHUNK,
    Stage.INDEXING: CHUNK,
    Stage.VERIFICATION: VERIFY,
}


def job_key(document: Document, step: str) -> str:
    return f"doc:{document.id}:a{document.ingestion_attempt}:{step}"


def enqueue_step(
    queue: Queue,
    session: Session | None,
    document: Document,
    task: str,
    step: str,
    extra: dict | None = None,
    priority: int = 0,
) -> None:
    queue.enqueue(
        task,
        {"document_id": str(document.id), "attempt": document.ingestion_attempt, **(extra or {})},
        idempotency_key=job_key(document, step),
        organization_id=document.organization_id,
        priority=priority,
        session=session,
    )


def enqueue_inspection(queue: Queue, session: Session, document: Document) -> None:
    enqueue_step(queue, session, document, INSPECT, "inspect")


def enqueue_resume(queue: Queue, session: Session, document: Document, stage: Stage) -> None:
    task = _RESUME_TASK[stage]
    enqueue_step(queue, session, document, task, task.split(".", 1)[1])
