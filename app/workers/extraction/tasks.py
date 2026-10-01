"""PDF inspection and page-range text extraction.

A PDF is never loaded fully into memory: PyMuPDF opens it lazily and each job
touches only its own page range. Pages are persisted in small batches, so a
restarted job resumes from the first page it had not stored yet.
"""

import logging
import uuid
from concurrent.futures.process import BrokenProcessPool

import pymupdf
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.infrastructure.queue.registry import JobContext, task
from app.modules.documents.model import (
    Document,
    DocumentPage,
    DocumentStatus,
    ExtractionMethod,
)
from app.modules.ingestion import pipeline, progress
from app.modules.ingestion.extraction import extract_pages
from app.modules.ingestion.model import IngestionStage, Stage, StageStatus
from app.workers.common import PipelineError, failure_guard, load_document
from app.workers.extraction.pool import discard_pool, extraction_pool
from app.workers.runtime import get_runtime

logger = logging.getLogger(__name__)

PERSIST_EVERY_PAGES = 50
# A range at least this long is extracted on the process pool (if enabled).
POOL_MIN_PAGES = 100
OCR_STAGE_ENGINES = (ExtractionMethod.OCR, ExtractionMethod.OCR_UNAVAILABLE, ExtractionMethod.PENDING_OCR)


def open_pdf(path: str) -> pymupdf.Document:
    try:
        pdf = pymupdf.open(path, filetype="pdf")
    except Exception:
        raise PipelineError("CORRUPT_PDF", "The PDF could not be opened; it may be corrupted.") from None
    if pdf.needs_pass:
        pdf.close()
        raise PipelineError("ENCRYPTED_PDF", "The PDF is password-protected. Upload an unprotected copy.")
    return pdf


@task(pipeline.INSPECT)
def inspect(payload: dict, ctx: JobContext) -> None:
    rt = get_runtime()
    with rt.session_factory() as session:
        document = load_document(session, payload, lock=True)
        if document is None:
            return
        with failure_guard(session, document.id, Stage.EXTRACTION, ctx):
            with rt.storage.local_path(document.storage_key) as path:
                pdf = open_pdf(path)
                try:
                    page_count = pdf.page_count
                    metadata = {k: v for k, v in (pdf.metadata or {}).items() if v}
                    toc = pdf.get_toc(simple=True)[:500]
                finally:
                    pdf.close()
            if page_count == 0:
                raise PipelineError("EMPTY_PDF", "The PDF has no pages.")

            document.status = DocumentStatus.PROCESSING
            document.page_count = page_count
            document.pdf_metadata = {**metadata, "outline": toc}
            progress.start(session, document.id, Stage.EXTRACTION, total_units=page_count)
            batch = rt.settings.EXTRACTION_BATCH_PAGES
            for start in range(1, page_count + 1, batch):
                end = min(start + batch - 1, page_count)
                pipeline.enqueue_step(
                    rt.queue, session, document, pipeline.EXTRACT_RANGE, f"extract:{start}",
                    {"start": start, "end": end},
                )
            session.commit()
            logger.info("Document %s: %s pages in %s ranges", document.id, page_count, -(-page_count // batch))


@task(pipeline.EXTRACT_RANGE)
def extract_range(payload: dict, ctx: JobContext) -> None:
    rt = get_runtime()
    start, end = payload["start"], payload["end"]
    with rt.session_factory() as session:
        document = load_document(session, payload)
        if document is None:
            return
        document_id, organization_id = document.id, document.organization_id
        storage_key = document.storage_key
        session.rollback()  # do not hold a snapshot while extracting

        with failure_guard(session, document_id, Stage.EXTRACTION, ctx):
            done = set(session.scalars(
                select(DocumentPage.page_number).where(
                    DocumentPage.document_id == document_id,
                    DocumentPage.page_number.between(start, end),
                )
            ))
            session.rollback()
            with rt.storage.local_path(storage_key) as path:
                open_pdf(path).close()  # a clear error for a corrupt or protected file
                pending_pages = [n for n in range(start, end + 1) if n not in done]
                batches = [pending_pages[i:i + PERSIST_EVERY_PAGES]
                           for i in range(0, len(pending_pages), PERSIST_EVERY_PAGES)]
                ocr_min_chars = rt.settings.OCR_MIN_CHARS
                ids = {"document_id": document_id, "organization_id": organization_id}
                for rows in _extracted(rt.settings, path, batches, ocr_min_chars):
                    _persist_pages(session, document_id, [{**ids, **row} for row in rows])
                    ctx.heartbeat()

            pending = session.scalar(
                select(func.count()).select_from(DocumentPage).where(
                    DocumentPage.document_id == document_id,
                    DocumentPage.page_number.between(start, end),
                    DocumentPage.method == ExtractionMethod.PENDING_OCR,
                )
            )
            document = load_document(session, payload)
            if document is None:
                return
            if pending:
                _mark_ocr_running(session, document_id)
                pipeline.enqueue_step(
                    rt.queue, session, document, pipeline.OCR_RANGE, f"ocr:{start}",
                    {"start": start, "end": end},
                )
            session.commit()
            check_extraction_complete(session, document)


def _extracted(settings, path: str, batches: list[list[int]], ocr_min_chars: int):
    """Each batch's page rows, in order: on the process pool for a large range, else here."""
    pool = extraction_pool(settings) if sum(map(len, batches)) >= POOL_MIN_PAGES else None
    if pool is not None:
        futures = [pool.submit(extract_pages, path, batch, ocr_min_chars) for batch in batches]
        try:
            for index, future in enumerate(futures):
                yield future.result()
            return
        except BrokenProcessPool:
            logger.warning("An extraction process died; extracting the rest of this range here", exc_info=True)
            discard_pool()
            batches = batches[index:]
        finally:
            for future in futures:
                future.cancel()
    for batch in batches:
        yield extract_pages(path, batch, ocr_min_chars)


def _persist_pages(session: Session, document_id: uuid.UUID, rows: list[dict]) -> None:
    if rows:
        session.execute(insert(DocumentPage).values(rows).on_conflict_do_nothing())
    stored = session.scalar(
        select(func.count()).select_from(DocumentPage).where(DocumentPage.document_id == document_id)
    )
    progress.set_done(session, document_id, Stage.EXTRACTION, stored)
    session.commit()


def _mark_ocr_running(session: Session, document_id: uuid.UUID) -> None:
    stage = session.scalar(
        select(IngestionStage).where(
            IngestionStage.document_id == document_id, IngestionStage.stage == Stage.OCR
        )
    )
    if stage.status == StageStatus.PENDING:
        progress.start(session, document_id, Stage.OCR)
    ocr_pages = session.scalar(
        select(func.count()).select_from(DocumentPage).where(
            DocumentPage.document_id == document_id, DocumentPage.method.in_(OCR_STAGE_ENGINES)
        )
    )
    progress.set_total(session, document_id, Stage.OCR, ocr_pages)


def check_extraction_complete(session: Session, document: Document) -> None:
    """Advance to structure analysis once every page is stored and no OCR is pending.

    Each range job calls this after committing its own pages, so the last one to
    finish always observes the complete set; duplicate enqueues collapse on the key.
    """
    rt = get_runtime()
    counts = dict(
        session.execute(
            select(DocumentPage.method, func.count())
            .where(DocumentPage.document_id == document.id)
            .group_by(DocumentPage.method)
        ).all()
    )
    stored = sum(counts.values())
    if stored < document.page_count:
        return
    extraction = session.scalar(
        select(IngestionStage).where(
            IngestionStage.document_id == document.id, IngestionStage.stage == Stage.EXTRACTION
        )
    )
    if extraction.status != StageStatus.COMPLETED:
        progress.finish(session, document.id, Stage.EXTRACTION, detail={
            "text_pages": counts.get(ExtractionMethod.TEXT, 0),
            "empty_pages": counts.get(ExtractionMethod.EMPTY, 0),
        })
    if counts.get(ExtractionMethod.PENDING_OCR):
        session.commit()
        return

    ocr_done = counts.get(ExtractionMethod.OCR, 0)
    unavailable = counts.get(ExtractionMethod.OCR_UNAVAILABLE, 0)
    progress.finish(
        session, document.id, Stage.OCR,
        status=StageStatus.COMPLETED if ocr_done or unavailable else StageStatus.SKIPPED,
        detail={"ocr_pages": ocr_done, "ocr_unavailable_pages": unavailable},
    )
    pipeline.enqueue_step(rt.queue, session, document, pipeline.STRUCTURE, "structure")
    session.commit()
