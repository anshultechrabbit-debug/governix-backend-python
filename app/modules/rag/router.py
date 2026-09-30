import json
import logging
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_app_settings, get_cache
from app.core.pagination import Page, PageParams, page_params
from app.core.responses import ApiResponse, ok
from app.infrastructure.cache.base import Cache
from app.modules.audit.model import AuditEvent
from app.modules.auth.dependencies import require
from app.modules.auth.permissions import Permission, Principal
from app.modules.rag.schema import AnswerResponse, AskRequest
from app.modules.rag.service import RAGService
from app.workers.runtime import get_runtime

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ai", tags=["ai assistant"])

Asker = Annotated[Principal, Depends(require(Permission.AI_QUERY))]


def get_service(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    cache: Annotated[Cache, Depends(get_cache)],
    settings=Depends(get_app_settings),
) -> RAGService:
    runtime = get_runtime()
    return RAGService(
        db, request.app.state.session_factory, settings, cache,
        embedder=runtime.embedder, reranker=runtime.reranker, llm_factory=lambda: runtime.llm,
    )


@router.post("/ask", response_model=ApiResponse[AnswerResponse])
def ask(
    request: Request,
    body: AskRequest,
    principal: Asker,
    service: Annotated[RAGService, Depends(get_service)],
):
    """Answer from authorised, effective evidence only, with verified citations.

    Rate limited per principal: this is the only endpoint that spends LLM budget.
    """
    settings = request.app.state.settings
    request.app.state.ai_limiter.check(f"ai:{principal.organization_id}:{principal.user_id}")
    return ok(service.ask(principal, body))


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@router.post("/ask/stream")
def ask_stream(
    request: Request,
    body: AskRequest,
    principal: Asker,
    cache: Annotated[Cache, Depends(get_cache)],
    settings=Depends(get_app_settings),
):
    """The same answer as POST /ai/ask, as server-sent events.

    event: stage   {"stage": "searching" | "reading" | "writing", ...}
    event: claim   {"text", "citations", "sources"}: a claim that passed validation
    event: done    the complete AnswerResponse (authoritative; identical to /ai/ask)
    event: error   {"code", "message"}

    A long answer takes as long to generate either way; streaming shows each
    validated claim as soon as it is written instead of after the last one.
    """
    request.app.state.ai_limiter.check(f"ai:{principal.organization_id}:{principal.user_id}")
    session_factory = request.app.state.session_factory
    runtime = get_runtime()

    def events():
        yield ": stream open\n\n"  # lets proxies and the browser start delivering at once
        # The stream outlives the request's dependencies, so it owns its session.
        with session_factory() as session:
            service = RAGService(
                session, session_factory, settings, cache,
                embedder=runtime.embedder, reranker=runtime.reranker, llm_factory=lambda: runtime.llm,
            )
            try:
                for kind, data in service.answer_events(principal, body):
                    yield _sse(kind, data.model_dump(mode="json") if kind == "done" else data)
            except Exception:
                logger.exception("Streaming answer failed")
                yield _sse("error", {"code": "ANSWER_FAILED", "message": "The answer could not be completed. Please try again."})

    return StreamingResponse(
        events(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class QueryHistoryItem(BaseModel):
    id: uuid.UUID
    question: str
    status: str | None
    created_at: datetime
    details: dict[str, Any]


@router.get("/queries", response_model=ApiResponse[Page[QueryHistoryItem]])
def my_queries(
    principal: Asker,
    db: Annotated[Session, Depends(get_db)],
    page: Annotated[PageParams, Depends(page_params)],
):
    """The caller's own recent AI questions (from the audit trail)."""
    # Filter base: the caller's own queries only, across their organisation.
    base = select(AuditEvent).where(
        AuditEvent.action == "ai.query",
        AuditEvent.actor_user_id == principal.user_id,
    )
    # COUNT on the base query, not on the paginated subquery: a count of
    # limit(N).offset(M) always returns at most N, not the true total.
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    events = db.scalars(
        base.order_by(AuditEvent.created_at.desc()).limit(page.limit).offset(page.offset)
    ).all()
    items = [
        QueryHistoryItem(
            id=e.id, question=e.details.get("question", ""), status=e.details.get("status"),
            created_at=e.created_at,
            details={k: e.details.get(k) for k in ("plan", "citations", "answer", "no_answer_reason")},
        )
        for e in events
    ]
    return ok(Page(items=items, total=total, **page.model_dump()))
