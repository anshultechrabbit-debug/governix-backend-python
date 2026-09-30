"""Document intelligence: identify, classify, match and suggest (never auto-confirm)."""

import logging

from sqlalchemy import select

from app.infrastructure.queue.registry import JobContext, task
from app.modules.audit.service import record_event
from app.modules.documents.model import DocumentStatus
from app.modules.ingestion import pipeline, progress
from app.modules.ingestion.analysis.analyzer import analyze_document
from app.modules.ingestion.model import IngestionStage, Stage
from app.workers.common import failure_guard, load_document
from app.workers.runtime import get_runtime

logger = logging.getLogger(__name__)


@task(pipeline.ANALYZE)
def analyze(payload: dict, ctx: JobContext) -> None:
    rt = get_runtime()
    with rt.session_factory() as session:
        document = load_document(session, payload, lock=True)
        if document is None:
            return
        document_id = document.id
        with failure_guard(session, document.id, Stage.ANALYSIS, ctx):
            progress.start(session, document.id, Stage.ANALYSIS)
            structure = session.scalar(
                select(IngestionStage).where(
                    IngestionStage.document_id == document.id, IngestionStage.stage == Stage.STRUCTURE
                )
            )
            body_font = float((structure.detail or {}).get("body_font_size") or 0.0)
            analysis = analyze_document(session, document, rt.settings, body_font, llm_factory=lambda: rt.llm)

            document.status = DocumentStatus.AWAITING_CONFIRMATION
            progress.finish(session, document.id, Stage.ANALYSIS, detail={
                "decision": analysis.decision, "confidence": analysis.confidence,
            })
            progress.wait(session, document.id, Stage.CONFIRMATION)
            record_event(
                session, "document.analyzed", organization_id=document.organization_id,
                resource_type="document", resource_id=document.id,
                details={
                    "decision": analysis.decision,
                    "confidence": analysis.confidence,
                    "suggested_name": analysis.suggested_name,
                    "matched_policy_id": str(analysis.matched_policy_id) if analysis.matched_policy_id else None,
                },
            )
            session.commit()
            logger.info("Document %s analysed: %s (%.2f)", document.id, analysis.decision, analysis.confidence)
    # A bulk upload confirms its group once every file has been analysed.
    from app.modules.uploads.hooks import document_settled

    document_settled(rt.session_factory, rt.queue, document_id)
