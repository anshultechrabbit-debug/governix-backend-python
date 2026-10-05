"""Chunk a confirmed document's sections and fan out embedding batches."""

import logging

from sqlalchemy import delete, func, insert, select

from app.infrastructure.queue.registry import JobContext, task
from app.modules.documents.model import DocumentSection
from app.modules.ingestion import pipeline, progress
from app.modules.ingestion.model import Stage, StageStatus
from app.modules.search.chunking import SectionInput, chunk_section
from app.modules.search.model import Chunk
from app.workers.common import PipelineError, failure_guard, load_document
from app.workers.runtime import get_runtime

logger = logging.getLogger(__name__)

SECTION_WINDOW = 200
INSERT_BATCH = 500


@task(pipeline.CHUNK)
def chunk_document(payload: dict, ctx: JobContext) -> None:
    rt = get_runtime()
    with rt.session_factory() as session:
        document = load_document(session, payload, lock=True)
        if document is None:
            return
        if document.policy_version_id is None:
            return  # not confirmed; nothing to index
        with failure_guard(session, document.id, Stage.CHUNKING, ctx):
            total_sections = session.scalar(
                select(func.count()).select_from(DocumentSection).where(DocumentSection.document_id == document.id)
            )
            progress.start(session, document.id, Stage.CHUNKING, total_units=total_sections)
            session.execute(delete(Chunk).where(Chunk.document_id == document.id))  # idempotent rebuild

            scope = {
                "organization_id": document.organization_id,
                "branch_id": document.branch_id,
                "department_id": document.department_id,
                "document_id": document.id,
                "policy_id": document.policy_id,
                "version_id": document.policy_version_id,
                "category_id": document.category_id,
            }
            index, done, rows = 0, 0, []
            last_order = 0
            while True:
                with rt.session_factory() as reader:
                    sections = reader.scalars(
                        select(DocumentSection)
                        .where(DocumentSection.document_id == document.id, DocumentSection.order_index > last_order)
                        .order_by(DocumentSection.order_index)
                        .limit(SECTION_WINDOW)
                    ).all()
                if not sections:
                    break
                for section in sections:
                    last_order = section.order_index
                    drafts = chunk_section(SectionInput(
                        section.id, section.number, section.path, section.content,
                        section.page_marks, section.page_start,
                    ))
                    for draft in drafts:
                        rows.append({
                            **scope,
                            "section_id": draft.section_id,
                            "chunk_index": index,
                            "section_number": draft.section_number,
                            "section_path": draft.section_path,
                            "text": draft.text,
                            "page_start": draft.page_start,
                            "page_end": draft.page_end,
                            "char_start": draft.char_start,
                            "token_count": draft.token_count,
                            "chunk_hash": draft.chunk_hash,
                        })
                        index += 1
                    if len(rows) >= INSERT_BATCH:
                        session.execute(insert(Chunk), rows)
                        rows = []
                done += len(sections)
                progress.set_done(session, document.id, Stage.CHUNKING, done)
                ctx.heartbeat()
            if rows:
                session.execute(insert(Chunk), rows)
            if index == 0:
                raise PipelineError("NO_CHUNKS", "The document produced no searchable text.")
            progress.finish(session, document.id, Stage.CHUNKING, detail={"chunks": index})

            if rt.settings.EMBEDDING_PROVIDER.lower() == "none":
                progress.finish(session, document.id, Stage.EMBEDDING, status=StageStatus.SKIPPED,
                                detail={"reason": "EMBEDDING_PROVIDER=none"})
            else:
                progress.start(session, document.id, Stage.EMBEDDING, total_units=index)
                progress.start(session, document.id, Stage.INDEXING)
                pipeline.enqueue_embedding(rt.queue, session, document, index, rt.settings.EMBED_BATCH_SIZE)
            # Searchable now by keyword; vectors are added as the embedding batches finish.
            pipeline.enqueue_step(rt.queue, session, document, pipeline.VERIFY, "verify")
            session.commit()
            logger.info("Document %s: %s chunks", document.id, index)
