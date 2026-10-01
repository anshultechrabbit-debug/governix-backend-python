import json
import logging
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
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
from app.modules.rag.conversations import ConversationService
from app.modules.rag.schema import (
    AnswerResponse, AskRequest, ConversationDetail, ConversationMessageRead, ConversationRead, ConversationUpdate,
)
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
    request.app.state.ai_limiter.check(f"ai:{principal.organization_id}:{principal.user_id}")
    response = service.ask(principal, body)
    _save(service.session, principal, body, response)
    return ok(response)


def _save(session: Session, principal: Principal, body: AskRequest, response: AnswerResponse) -> None:
    """Add the turn to the asker's chat history. A failure here never costs them the answer."""
    try:
        conversation = ConversationService(session).record(principal, body, response)
    except Exception:
        logger.exception("Could not save the answer to the chat history")
        session.rollback()
        return
    response.conversation_id = conversation.id if conversation else None


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
                    if kind == "done":
                        _save(session, principal, body, data)
                        data = data.model_dump(mode="json")
                    yield _sse(kind, data)
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


# --- chat history ------------------------------------------------------------------


def get_conversations(db: Annotated[Session, Depends(get_db)]) -> ConversationService:
    return ConversationService(db)


Conversations = Annotated[ConversationService, Depends(get_conversations)]


def _read(conversation, message_count: int) -> ConversationRead:
    return ConversationRead(
        id=conversation.id, title=conversation.title, created_at=conversation.created_at,
        last_message_at=conversation.last_message_at, message_count=message_count,
    )


@router.get("/conversations", response_model=ApiResponse[Page[ConversationRead]])
def list_conversations(
    principal: Asker,
    service: Conversations,
    page: Annotated[PageParams, Depends(page_params)],
    search: Annotated[str | None, Query(max_length=200)] = None,
):
    """The caller's own conversations, most recent first."""
    rows, total = service.recent(principal, search=search, limit=page.limit, offset=page.offset)
    return ok(Page(items=[_read(c, n) for c, n in rows], total=total, **page.model_dump()))


@router.get("/conversations/{conversation_id}", response_model=ApiResponse[ConversationDetail])
def get_conversation(conversation_id: uuid.UUID, principal: Asker, service: Conversations):
    """A conversation with every question and the answer it got."""
    conversation = service.get(principal, conversation_id)
    messages = service.messages(conversation)
    return ok(ConversationDetail(
        **_read(conversation, len(messages)).model_dump(),
        messages=[
            ConversationMessageRead(id=m.id, question=m.question, options=m.options,
                                    answer=AnswerResponse.model_validate(m.answer), created_at=m.created_at)
            for m in messages
        ],
    ))


@router.patch("/conversations/{conversation_id}", response_model=ApiResponse[ConversationRead])
def rename_conversation(conversation_id: uuid.UUID, body: ConversationUpdate, principal: Asker, service: Conversations):
    conversation = service.rename(principal, conversation_id, body.title)
    return ok(_read(conversation, len(service.messages(conversation))))


@router.delete("/conversations/{conversation_id}", response_model=ApiResponse[dict])
def delete_conversation(conversation_id: uuid.UUID, principal: Asker, service: Conversations):
    service.delete(principal, conversation_id)
    return ok({"deleted": True})


@router.delete("/conversations", response_model=ApiResponse[dict])
def delete_all_conversations(principal: Asker, service: Conversations):
    """Clear the caller's whole chat history."""
    return ok({"deleted": service.delete_all(principal)})
