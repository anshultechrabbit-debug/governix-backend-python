import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class ConversationTurn(BaseModel):
    """An earlier turn, used only to resolve references in a follow-up question.

    A long earlier answer (a list of every requirement) is cut, not refused: refusing it would
    fail every question asked after it in the same chat. Only its opening is ever read.
    """

    question: str = Field(max_length=2000)
    answer: str | None = Field(default=None, max_length=8000)

    @field_validator("question", "answer", mode="before")
    @classmethod
    def _cut(cls, value, info):
        limit = 2000 if info.field_name == "question" else 8000
        return value[:limit] if isinstance(value, str) else value


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    # Earlier turns of this conversation, oldest first. Never used as evidence.
    history: list[ConversationTurn] = Field(default_factory=list, max_length=10)
    # auto: decided from the question; the others mirror the UI selector.
    mode: Literal["auto", "current", "historical", "version", "compare"] = "auto"
    as_of: date | None = None
    version_ids: list[uuid.UUID] = Field(default_factory=list, max_length=2)
    policy_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    category_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    # The saved conversation to add this question to; a new one is started when absent.
    conversation_id: uuid.UUID | None = None


class Source(BaseModel):
    number: int
    evidence_id: str
    kind: Literal["passage", "comparison"] = "passage"
    chunk_id: uuid.UUID | None = None
    document_id: uuid.UUID | None = None
    document_title: str | None = None
    policy_id: uuid.UUID | None = None
    policy_name: str | None = None
    version_id: uuid.UUID | None = None
    version_label: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    section_number: str | None = None
    section_path: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    category: str | None = None
    authority_rank: int | None = None
    # Taken from an earlier version because the version in force did not cover the question.
    previous_version: bool = False
    excerpt: str


class Claim(BaseModel):
    text: str
    citations: list[int]


class NoAnswer(BaseModel):
    reason: str
    message: str
    suggestions: list[str]
    missing_terms: list[str] = []


class AnswerResponse(BaseModel):
    query_id: uuid.UUID | None = None
    question: str
    status: Literal["answered", "no_answer"]
    answer: str | None
    # The direct answer in plain words, checked against the evidence its claims cite.
    summary: str | None = None
    claims: list[Claim]
    sources: list[Source]
    conflicts: list[dict[str, Any]]
    warnings: list[str]
    no_answer: NoAnswer | None = None
    plan: dict[str, Any]
    evidence_score: float
    model: str | None
    usage: dict[str, int]
    timings_ms: dict[str, float]
    cache_hit: bool = False
    # The saved conversation this answer was added to.
    conversation_id: uuid.UUID | None = None


class ConversationRead(BaseModel):
    id: uuid.UUID
    title: str
    created_at: datetime
    last_message_at: datetime
    message_count: int = 0


class ConversationMessageRead(BaseModel):
    id: uuid.UUID
    question: str
    options: dict[str, Any]
    answer: AnswerResponse
    created_at: datetime


class ConversationDetail(ConversationRead):
    messages: list[ConversationMessageRead]


class ConversationUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
