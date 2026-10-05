"""Ingestion pipeline wiring: which job runs after which.

    inspect ──► extract_range × N (parallel page ranges) ──► [ocr_range × K] ──► structure ──► analyze
        ──► [person confirms] ──► chunk ──┬─► verify ──► READY (keyword search and answers)
                                          └─► embed_batch × M (parallel) ──► verify_vectors

A large document is usable as soon as it is chunked: embedding it is limited by the
provider's tokens-per-minute quota and can take far longer than everything else.
Semantic search covers each chunk as soon as its vector is written.

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
VERIFY_VECTORS = "ingestion.verify_vectors"

# Queue order when many uploads run at once. A document past extraction goes ahead of other
# documents' page ranges, so each file reaches "ready" instead of all of them waiting for
# every file to be read. Embedding is the long tail, paced by the provider's quota, so every
# other job goes ahead of its queued batches.
TASK_PRIORITY = {
    STRUCTURE: 5, ANALYZE: 5, CHUNK: 5, VERIFY: 5, VERIFY_VECTORS: 5,
    EMBED_BATCH: -10,
}

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
    priority: int | None = None,
) -> None:
    queue.enqueue(
        task,
        {"document_id": str(document.id), "attempt": document.ingestion_attempt, **(extra or {})},
        idempotency_key=job_key(document, step),
        organization_id=document.organization_id,
        priority=TASK_PRIORITY.get(task, 0) if priority is None else priority,
        session=session,
    )


def enqueue_inspection(queue: Queue, session: Session, document: Document) -> None:
    enqueue_step(queue, session, document, INSPECT, "inspect")


def enqueue_resume(queue: Queue, session: Session, document: Document, stage: Stage) -> None:
    task = _RESUME_TASK[stage]
    enqueue_step(queue, session, document, task, task.split(".", 1)[1])


def enqueue_embedding(queue: Queue, session: Session, document: Document, total: int, batch_size: int,
                      round_: str = "") -> None:
    """One embed_batch job per `batch_size` chunks. A batch skips chunks that already have a vector,
    so a new `round_` (e.g. resuming after a provider outage) only embeds what is missing."""
    for start in range(0, total, batch_size):
        enqueue_step(
            queue, session, document, EMBED_BATCH, f"embed:{start}{round_}",
            {"start": start, "end": min(start + batch_size, total) - 1},
        )
