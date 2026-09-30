"""Section detection and content fingerprinting over all extracted pages."""

import logging

from sqlalchemy import delete, func, insert, select

from app.infrastructure.queue.registry import JobContext, task
from app.modules.documents.model import DocumentPage, DocumentSection, ExtractionMethod
from app.modules.ingestion import pipeline, progress
from app.modules.ingestion.model import Stage
from app.modules.ingestion.structure import LayoutStats, SectionDraft, StructureBuilder
from app.workers.common import PipelineError, failure_guard, load_document
from app.workers.runtime import get_runtime

logger = logging.getLogger(__name__)

PAGE_WINDOW = 200
SECTION_BATCH = 200


def iter_page_lines(session_factory, document_id, page_count):
    """Keyset-paged read of page layouts: bounded memory, no long-lived cursor.

    Uses its own session so reads never interfere with the writer's transaction.
    """
    for first in range(1, page_count + 1, PAGE_WINDOW):
        with session_factory() as reader:
            rows = reader.execute(
                select(DocumentPage.page_number, DocumentPage.lines)
                .where(
                    DocumentPage.document_id == document_id,
                    DocumentPage.page_number.between(first, first + PAGE_WINDOW - 1),
                )
                .order_by(DocumentPage.page_number)
            ).all()
        yield from rows


@task(pipeline.STRUCTURE)
def build_structure(payload: dict, ctx: JobContext) -> None:
    rt = get_runtime()
    with rt.session_factory() as session:
        document = load_document(session, payload)
        if document is None:
            return
        document_id, organization_id, page_count = document.id, document.organization_id, document.page_count
        with failure_guard(session, document_id, Stage.STRUCTURE, ctx):
            progress.start(session, document_id, Stage.STRUCTURE, total_units=page_count)
            session.commit()

            stats = LayoutStats()
            for _number, lines in iter_page_lines(rt.session_factory, document_id, page_count):
                stats.observe(lines)
            stats.finalize()
            ctx.heartbeat()

            # Idempotent: a rerun replaces the previous structure entirely.
            session.execute(delete(DocumentSection).where(DocumentSection.document_id == document_id))
            builder = StructureBuilder(stats)
            buffer: list[SectionDraft] = []
            for number, lines in iter_page_lines(rt.session_factory, document_id, page_count):
                buffer.extend(builder.add_page(number, lines))
                if len(buffer) >= SECTION_BATCH:
                    _insert_sections(session, document_id, organization_id, buffer)
                    buffer = []
                if number % PAGE_WINDOW == 0:
                    progress.set_done(session, document_id, Stage.STRUCTURE, number)
                    ctx.heartbeat()
            buffer.extend(builder.finish())
            _insert_sections(session, document_id, organization_id, buffer)

            if builder.fingerprint.word_count == 0:
                unavailable = session.scalar(
                    select(func.count()).select_from(DocumentPage).where(
                        DocumentPage.document_id == document_id,
                        DocumentPage.method == ExtractionMethod.OCR_UNAVAILABLE,
                    )
                )
                if unavailable:
                    raise PipelineError(
                        "OCR_UNAVAILABLE",
                        f"No text layer found and OCR is not available on this server "
                        f"({unavailable} scanned pages). Install Tesseract or enable cloud OCR, then retry.",
                    )
                raise PipelineError("NO_TEXT", "The document contains no extractable text.")

            document = load_document(session, payload, lock=True)
            if document is None:
                session.rollback()
                return
            document.content_hash = builder.fingerprint.content_hash
            document.simhash = builder.fingerprint.simhash
            progress.set_done(session, document_id, Stage.STRUCTURE, page_count)
            progress.finish(session, document_id, Stage.STRUCTURE, detail={
                "sections": builder.section_count,
                "body_font_size": stats.body_size,
                "boilerplate_lines_removed": len(stats.boilerplate),
                "words": builder.fingerprint.word_count,
            })
            pipeline.enqueue_step(rt.queue, session, document, pipeline.ANALYZE, "analyze")
            session.commit()
            logger.info("Document %s: %s sections", document_id, builder.section_count)


def _insert_sections(session, document_id, organization_id, drafts: list[SectionDraft]) -> None:
    if not drafts:
        return
    session.execute(insert(DocumentSection), [
        {
            "id": d.id,
            "document_id": document_id,
            "organization_id": organization_id,
            "parent_id": d.parent_id,
            "order_index": d.order_index,
            "level": d.level,
            "number": d.number,
            "title": d.title,
            "path": d.path[:4000],
            "page_start": d.page_start,
            "page_end": d.page_end,
            "content": d.content,
            "page_marks": d.page_marks,
            "char_count": d.length,
            "content_hash": d.content_hash,
        }
        for d in drafts
    ])
