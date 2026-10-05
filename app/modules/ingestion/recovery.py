"""Resume documents that failed only because a provider was not available.

A document that failed with EMBEDDINGS_UNAVAILABLE (no API key, provider
outage) stayed FAILED forever, even after the configuration was fixed and the
worker restarted: only a person pressing "retry" could resume it. The worker
now runs this sweep at startup and resumes such documents once the provider
works again. Failures caused by the document itself are never retried here.
"""

import logging
import time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.infrastructure.queue.base import Queue
from app.modules.documents.model import Document, DocumentStatus
from app.modules.documents.service import requeue_failed
from app.modules.ingestion import pipeline, progress
from app.modules.ingestion.model import IngestionStage, Stage, StageStatus

logger = logging.getLogger(__name__)

# error code -> the provider that must now be available for a retry to succeed
PROVIDER_FAILURES = {
    "EMBEDDINGS_UNAVAILABLE": "embedder",
    "OCR_UNAVAILABLE": "ocr",
}
SWEEP_LIMIT = 500


def resume_provider_failures(session: Session, queue: Queue, available: set[str]) -> list[Document]:
    """Requeue failed documents whose missing provider is now in `available`."""
    codes = [code for code, provider in PROVIDER_FAILURES.items() if provider in available]
    if not codes:
        return []
    documents = session.scalars(
        select(Document)
        .where(Document.status == DocumentStatus.FAILED, Document.error["code"].astext.in_(codes))
        .order_by(Document.updated_at)
        .limit(SWEEP_LIMIT)
        .with_for_update(skip_locked=True)
    ).all()
    for document in documents:
        stage = requeue_failed(session, queue, document, actor=None)
        logger.info("Resuming document %s from %s after provider recovery", document.id, stage)
    session.commit()
    return list(documents)


def resume_ready_embeddings(session: Session, queue: Queue, batch_size: int) -> list[Document]:
    """Finish the vectors of READY documents whose embedding stage failed after they became searchable."""
    from app.modules.search.model import Chunk

    documents = session.scalars(
        select(Document)
        .join(IngestionStage, IngestionStage.document_id == Document.id)
        .where(Document.status == DocumentStatus.READY, IngestionStage.stage == Stage.EMBEDDING,
               IngestionStage.status == StageStatus.FAILED)
        .order_by(Document.updated_at)
        .limit(SWEEP_LIMIT)
        .with_for_update(of=Document, skip_locked=True)
    ).all()
    for document in documents:
        total = session.scalar(select(func.count()).where(Chunk.document_id == document.id))
        progress.start(session, document.id, Stage.EMBEDDING)
        progress.start(session, document.id, Stage.INDEXING)
        # A new round: the failed batches' jobs are spent; their idempotency keys stay taken.
        pipeline.enqueue_embedding(queue, session, document, total, batch_size,
                                   round_=f":r{int(time.time())}")
        logger.info("Resuming embeddings of document %s", document.id)
    session.commit()
    return list(documents)


def available_providers(runtime) -> set[str]:
    """Providers that can be constructed with the current configuration."""
    available = set()
    try:
        if runtime.embedder is not None:
            available.add("embedder")
    except Exception:  # noqa: BLE001 - any construction failure means "not available"
        logger.info("Embedding provider not available; failed embeddings stay failed")
    try:
        ocr = runtime.ocr
        if ocr is not None and ocr.is_available():
            available.add("ocr")
    except Exception:  # noqa: BLE001
        logger.info("OCR provider not available; failed OCR documents stay failed")
    return available


def startup_sweep(session_factory) -> None:
    """Run the recovery sweep when a worker starts. Never raises: startup must not depend on it."""
    from app.workers.runtime import get_runtime

    try:
        runtime = get_runtime()
        with session_factory() as session:
            available = available_providers(runtime)
            resumed = resume_provider_failures(session, runtime.queue, available)
            if "embedder" in available:
                resumed += resume_ready_embeddings(session, runtime.queue, runtime.settings.EMBED_BATCH_SIZE)
        if resumed:
            logger.info("Resumed %s document(s) after provider recovery", len(resumed))
    except Exception:  # noqa: BLE001
        logger.warning("Provider-recovery sweep failed; failed documents can still be retried by hand", exc_info=True)
