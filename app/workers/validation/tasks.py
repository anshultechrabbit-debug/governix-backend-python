"""Index verification.

verify          after chunking: the keyword index is complete, so the document becomes READY
verify_vectors  after the last embedding batch: every chunk has a vector the index can find
"""

import logging
from datetime import UTC, datetime

from sqlalchemy import func, select, text

from app.infrastructure.queue.registry import JobContext, task
from app.modules.audit.service import record_event
from app.modules.documents.model import DocumentStatus
from app.modules.ingestion import pipeline, progress
from app.modules.ingestion.model import Stage, StageStatus
from app.modules.organizations.repository import bump_knowledge_version
from app.modules.policies.publication import document_published
from app.modules.search.model import Chunk
from app.workers.common import PipelineError, failure_guard, load_document
from app.workers.runtime import get_runtime

logger = logging.getLogger(__name__)
SELF_RETRIEVAL_SAMPLES = 5
# Every retrieval query joins these small tables. They rarely reach autovacuum's
# analyze threshold, and with no statistics the planner guessed one row each and
# nested-looped over the whole chunk table (0.5s per lane instead of ~15ms).
PLANNER_TABLES = ("documents", "policies", "policy_versions")


@task(pipeline.VERIFY)
def verify(payload: dict, ctx: JobContext) -> None:
    rt = get_runtime()
    with rt.session_factory() as session:
        document = load_document(session, payload, lock=True)
        if document is None:
            return
        with failure_guard(session, document.id, Stage.VERIFICATION, ctx):
            total, indexed_fts, bad_pages = session.execute(
                select(
                    func.count(),
                    func.count().filter(Chunk.tsv.is_not(None)),
                    func.count().filter((Chunk.page_start < 1) | (Chunk.page_end > document.page_count)
                                        | (Chunk.page_end < Chunk.page_start)),
                ).where(Chunk.document_id == document.id)
            ).one()
            problems = []
            if total == 0:
                problems.append("no chunks")
            if indexed_fts != total:
                problems.append(f"{total - indexed_fts} chunks without keyword index")
            if bad_pages:
                problems.append(f"{bad_pages} chunks with invalid page references")
            if problems:
                raise PipelineError("INDEX_VALIDATION_FAILED", "Index validation failed: " + "; ".join(problems))

            progress.finish(session, document.id, Stage.VERIFICATION, detail={"chunks": total})
            if rt.settings.EMBEDDING_PROVIDER.lower() == "none":
                progress.finish(session, document.id, Stage.EMBEDDING, status=StageStatus.SKIPPED)
                progress.finish(session, document.id, Stage.INDEXING, detail={"keyword_indexed": indexed_fts})
            document.status = DocumentStatus.READY
            document.ready_at = datetime.now(UTC)
            bump_knowledge_version(session, document.organization_id)
            record_event(
                session, "document.ready", organization_id=document.organization_id,
                resource_type="document", resource_id=document.id,
                details={"chunks": total, "policy_version_id": str(document.policy_version_id)},
            )
            document_published(session, rt.queue, document)
            session.commit()
            logger.info("Document %s is READY (%s chunks)", document.id, total)
        refresh_planner_statistics(session)


@task(pipeline.VERIFY_VECTORS)
def verify_vectors(payload: dict, ctx: JobContext) -> None:
    """A broken vector index leaves the document searchable by keyword: only this stage fails."""
    rt = get_runtime()
    with rt.session_factory() as session:
        document = load_document(session, payload, lock=True)
        if document is None:
            return
        with failure_guard(session, document.id, Stage.INDEXING, ctx):
            total, embedded = session.execute(
                select(func.count(), func.count(Chunk.embedding)).where(Chunk.document_id == document.id)
            ).one()
            problems = []
            if embedded != total:
                problems.append(f"{total - embedded} chunks without embeddings")
            self_hits = _self_retrieval(session, document.id) if total else None
            # Identical chunks can legitimately outrank each other; zero hits means a broken index.
            if self_hits == 0:
                problems.append("vector index returned no chunk as its own nearest neighbour")
            if problems:
                message = "Vector index validation failed: " + "; ".join(problems)
                logger.error("Document %s: %s", document.id, message)
                progress.finish(session, document.id, Stage.INDEXING, status=StageStatus.FAILED,
                                detail={"error": message})
            else:
                progress.finish(session, document.id, Stage.INDEXING, detail={
                    "vector_indexed": embedded, "self_retrieval": self_hits,
                })
                if document.status == DocumentStatus.READY:
                    # Answers cached while only keyword search covered it are recomputed.
                    bump_knowledge_version(session, document.organization_id)
            session.commit()


def refresh_planner_statistics(session) -> None:
    """ANALYZE the small tables every search joins (milliseconds). Best effort."""
    try:
        session.execute(text(f"ANALYZE {', '.join(PLANNER_TABLES)}"))
        session.commit()
    except Exception:  # noqa: BLE001 - statistics are an optimisation, never a failure
        session.rollback()
        logger.warning("Could not refresh planner statistics", exc_info=True)


def _self_retrieval(session, document_id) -> int:
    """Each sampled chunk's nearest neighbour (within its document) should be itself."""
    sample = session.execute(
        select(Chunk.id, Chunk.embedding).where(Chunk.document_id == document_id)
        .order_by(func.random()).limit(SELF_RETRIEVAL_SAMPLES)
    ).all()
    hits = 0
    for chunk_id, embedding in sample:
        nearest = session.scalar(
            select(Chunk.id).where(Chunk.document_id == document_id)
            .order_by(Chunk.embedding.cosine_distance(embedding)).limit(1)
        )
        hits += nearest == chunk_id
    return hits
