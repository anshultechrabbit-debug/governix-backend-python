"""Chat history: each person's conversations with the AI Assistant.

A conversation is private to the person who had it. Every answered (or
unanswered) question is stored with the complete response it got, so reopening
a conversation shows exactly what was shown then; asking again in it sends the
earlier turns as history, as the live chat does.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.modules.auth.permissions import Principal
from app.modules.rag.model import Conversation, ConversationMessage
from app.modules.rag.schema import AnswerResponse, AskRequest

TITLE_CHARS = 80


def title_from(question: str) -> str:
    text = " ".join(question.split())
    return text if len(text) <= TITLE_CHARS else text[: TITLE_CHARS - 1].rsplit(" ", 1)[0] + "…"


class ConversationService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _owned(self, principal: Principal):
        return select(Conversation).where(
            Conversation.user_id == principal.user_id, Conversation.organization_id == principal.organization_id,
        )

    def get(self, principal: Principal, conversation_id: uuid.UUID) -> Conversation:
        conversation = self.session.scalar(self._owned(principal).where(Conversation.id == conversation_id))
        if conversation is None:
            raise NotFoundError("Conversation not found.")
        return conversation

    def record(self, principal: Principal, request: AskRequest, response: AnswerResponse) -> Conversation | None:
        """Add the question and its answer to the conversation (a new one when none is given). Commits."""
        if principal.organization_id is None:
            return None
        conversation = None
        if request.conversation_id is not None:
            conversation = self.session.scalar(self._owned(principal).where(Conversation.id == request.conversation_id))
        if conversation is None:  # new, or deleted in another tab: start afresh rather than lose the answer
            conversation = Conversation(
                organization_id=principal.organization_id, user_id=principal.user_id, title=title_from(request.question),
            )
            self.session.add(conversation)
            self.session.flush()
        self.session.add(ConversationMessage(
            conversation_id=conversation.id,
            question=request.question,
            options=request.model_dump(mode="json", include={"mode", "as_of", "version_ids", "policy_ids", "category_ids"}),
            answer=response.model_dump(mode="json"),
        ))
        conversation.last_message_at = datetime.now(UTC)
        self.session.commit()
        return conversation

    def recent(self, principal: Principal, *, search: str | None, limit: int, offset: int):
        counts = (
            select(ConversationMessage.conversation_id, func.count().label("messages"))
            .group_by(ConversationMessage.conversation_id).subquery()
        )
        query = self._owned(principal)
        if search:
            query = query.where(Conversation.title.ilike(f"%{search.strip()}%"))
        total = self.session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = self.session.execute(
            query.add_columns(func.coalesce(counts.c.messages, 0))
            .outerjoin(counts, counts.c.conversation_id == Conversation.id)
            .order_by(Conversation.last_message_at.desc()).limit(limit).offset(offset)
        ).all()
        return rows, total

    def messages(self, conversation: Conversation) -> list[ConversationMessage]:
        return list(self.session.scalars(
            select(ConversationMessage).where(ConversationMessage.conversation_id == conversation.id)
            .order_by(ConversationMessage.created_at)
        ))

    def rename(self, principal: Principal, conversation_id: uuid.UUID, title: str) -> Conversation:
        conversation = self.get(principal, conversation_id)
        conversation.title = " ".join(title.split())[:200]
        self.session.commit()
        return conversation

    def delete(self, principal: Principal, conversation_id: uuid.UUID) -> None:
        self.session.delete(self.get(principal, conversation_id))
        self.session.commit()

    def delete_all(self, principal: Principal) -> int:
        ids = select(Conversation.id).where(
            Conversation.user_id == principal.user_id, Conversation.organization_id == principal.organization_id,
        )
        count = self.session.execute(delete(Conversation).where(Conversation.id.in_(ids))).rowcount
        self.session.commit()
        return count or 0
