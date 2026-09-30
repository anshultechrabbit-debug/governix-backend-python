"""OCR for pages without a usable text layer (only those pages, never the whole PDF)."""

import logging

from sqlalchemy import func, select, update

from app.infrastructure.queue.registry import JobContext, task
from app.modules.documents.model import DocumentPage, ExtractionMethod
from app.modules.ingestion import pipeline, progress
from app.modules.ingestion.extraction import lines_from_plain_text
from app.modules.ingestion.model import Stage
from app.workers.common import PipelineError, failure_guard, load_document
from app.workers.extraction.tasks import check_extraction_complete, open_pdf
from app.workers.runtime import get_runtime

logger = logging.getLogger(__name__)

COMMIT_EVERY_PAGES = 5


@task(pipeline.OCR_RANGE)
def ocr_range(payload: dict, ctx: JobContext) -> None:
    rt = get_runtime()
    start, end = payload["start"], payload["end"]
    with rt.session_factory() as session:
        document = load_document(session, payload)
        if document is None:
            return
        document_id, storage_key = document.id, document.storage_key
        in_range = (
            DocumentPage.document_id == document_id,
            DocumentPage.page_number.between(start, end),
            DocumentPage.method == ExtractionMethod.PENDING_OCR,
        )
        pages = list(session.scalars(select(DocumentPage.page_number).where(*in_range)))
        session.rollback()

        with failure_guard(session, document_id, Stage.OCR, ctx):
            engine = rt.ocr
            if not engine.is_available():
                logger.warning("Document %s: %s pages need OCR but no engine is available", document_id, len(pages))
                session.execute(update(DocumentPage).where(*in_range).values(
                    method=ExtractionMethod.OCR_UNAVAILABLE
                ))
            else:
                with rt.storage.local_path(storage_key) as path:
                    pdf = open_pdf(path)
                    try:
                        for index, number in enumerate(pages, start=1):
                            result = engine.ocr_page(pdf.load_page(number - 1))
                            text = result.text.strip()
                            session.execute(
                                update(DocumentPage)
                                .where(DocumentPage.document_id == document_id, DocumentPage.page_number == number)
                                .values(
                                    text=text, char_count=len(text), method=ExtractionMethod.OCR,
                                    lines=lines_from_plain_text(result.text), ocr_engine=result.engine,
                                )
                            )
                            if index % COMMIT_EVERY_PAGES == 0:
                                _record_progress(session, document_id)
                                ctx.heartbeat()
                    finally:
                        pdf.close()
            _record_progress(session, document_id)
            document = load_document(session, payload)
            if document is not None:
                check_extraction_complete(session, document)


def _record_progress(session, document_id) -> None:
    done = session.scalar(
        select(func.count()).select_from(DocumentPage).where(
            DocumentPage.document_id == document_id,
            DocumentPage.method.in_((ExtractionMethod.OCR, ExtractionMethod.OCR_UNAVAILABLE)),
        )
    )
    progress.set_done(session, document_id, Stage.OCR, done)
    session.commit()
