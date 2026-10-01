"""Grounded question answering.

    authenticate -> ACL scope -> plan (current/historical/version/compare)
    -> policy lookup -> hybrid retrieval (exact/keyword/vector/section, filtered in SQL)
    -> rerank -> evidence (context, authority, amendments, conflicts)
    -> NO-ANSWER GATE -> LLM (structured) -> claim/number/citation validation
    -> citation re-verification against the database -> answer

Latest version first: a question about the current policy is answered from the
version in force. Only when that yields no supported answer are earlier
versions searched, one step back at a time (the previous version, then the one
before), and an answer found there says so. Policies with no version in force
(expired) are never used this way. A question that names an uploaded file
("policy_document.pdf") is answered from that file alone, whatever its version.

Retrieve first, verify evidence, generate second. The LLM never searches and
never decides permissions; its output is discarded unless it validates.
"""

import logging
import re
import time
import uuid
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.infrastructure.ai.embeddings.base import EmbeddingProvider
from app.infrastructure.ai.llm.base import LLMProvider, LLMResult, LLMUnavailableError
from app.infrastructure.ai.reranker.base import RerankerProvider
from app.infrastructure.cache.base import Cache, build_cache_key
from app.modules.audit.service import record_event
from app.modules.auth.acl import visible_clause
from app.modules.auth.permissions import Principal
from app.modules.citations.numerics import extract_numeric_facts
from app.modules.documents.model import Document, DocumentStatus
from app.modules.policies.model import Policy, PolicyVersion, VersionStatus
from app.modules.rag.claim_stream import ClaimStream
from app.modules.rag.evidence import EvidenceItem, EvidenceSet, build_evidence, is_document_question, key_terms
from app.infrastructure.ai.llm.local import LocalLLM
from app.modules.rag.prompts import (
    OUTPUT_SCHEMA, SUMMARY_CHECK_PROMPT, SUMMARY_CHECK_SCHEMA, SYSTEM_PROMPT, build_user_prompt, comparison_text,
)
from app.modules.rag.query_plan import QueryClass, QueryPlan, plan_query
from app.modules.rag.query_rewrite import Rewrite, restated_questions, standalone_question
from app.modules.rag.schema import AnswerResponse, AskRequest, Claim, NoAnswer, Source
from app.modules.rag.validation import ABSENCE_PROBLEM, EvidenceText, TermIndex, validate_claims
from app.modules.search.model import Chunk
from app.modules.search.retrieval import HybridRetriever, NamedDocument, SearchFilters, VersionScope
from app.modules.search.schema import Provenance
from app.modules.search.service import cache_scope, provenance, retrieve_with_variants
from app.modules.versions.timeline import compare_versions, effective_on

logger = logging.getLogger(__name__)

SUGGESTIONS = [
    "Use the policy's name or number",
    "Add a date or version, for example “as of March 2025” or “v3”",
    "Ask about one specific rule or topic",
]
NO_ANSWER_MESSAGE = "None of the documents you can access answer this, so I won't guess."
# Bump when the answer-generation or validation contract changes so cached
# answers (including cached no-answers) are recomputed under the new contract.
#
# Changelog:
#   v1-v5  initial pipeline iterations
#   v6     evidence header (metadata echo) filtering added
#   v7     salient-coverage gate added
#   v8     polarity-flip validation added
#   v9     number-binding validation added
#   v10    subject-validation (named terms) check added
#   v11    version-fallback path added; fallback flagged in response plan
#   v12    comparison deterministic diff (D1) evidence type added
#   v13    acronym fast-path answer added
#   v14    cache key changed: raw history excluded; keyed on rewritten question
#   v15    summary check merged into claim validation (no second LLM call)
ANSWER_CACHE_VERSION = "v31"
# No supported answer in the version in force: worth looking one version back.
FALLBACK_REASONS = frozenset({
    "NO_RELEVANT_DOCUMENTS", "LOW_RELEVANCE", "KEY_TERMS_NOT_FOUND", "INSUFFICIENT_EVIDENCE",
    "ANSWER_FAILED_VALIDATION", "ANSWER_OFF_TOPIC",
})
_TERM = r"([A-Za-z][A-Za-z0-9-]{1,20})"
_ACRONYM_QUESTION = re.compile(
    rf"\bwhat\s+does\s+{_TERM}\s+stand\s+for\b"
    rf"|\bfull[\s-]+form\s+of\s+(?:the\s+)?(?:term\s+|acronym\s+|abbreviation\s+)?{_TERM}\b"
    rf"|\b(?:expand|expansion\s+of)\s+(?:the\s+)?(?:acronym\s+|abbreviation\s+)?{_TERM}\b"
    rf"|\bwhat\s+is\s+{_TERM}\s+short\s+for\b",
    re.IGNORECASE,
)
# Forms that name a bare token ("What does BESS mean?", "BESS full form") only count
# for an all-caps token, so "what does the rule mean" is not an acronym question.
_ACRONYM_MEANING = re.compile(
    r"\b(?i:what\s+does)\s+([A-Z][A-Z0-9]{1,11})\s+(?i:mean)\b|\b([A-Z][A-Z0-9]{1,11})\s+(?i:full[\s-]+form)\b"
)


# The evidence header's period format; document text does not use it.
_METADATA_ECHO = re.compile(r"\beffective\s+(?:from\s+)?\d{4}-\d{2}-\d{2}\s+(?:to|until)\s+(?:present|\d{4}-\d{2}-\d{2})", re.I)
_MONTHS = frozenset(
    "january february march april may june july august september october november december "
    "jan feb mar apr jun jul aug sep sept oct nov dec".split()
)
_VERSION_QUESTION = re.compile(r"\b(?:version|versions|effective|in\s+force|valid|when|date|dated|current|latest|superseded)\b", re.I)


def _asks_about_versions(question: str, plan: QueryPlan) -> bool:
    return plan.query_class is not QueryClass.CURRENT or bool(_VERSION_QUESTION.search(question))


def _drop_metadata_echo(results, question: str, plan: QueryPlan) -> None:
    """Restating the evidence header ("Version 1 ... effective 2024-10-01 to present")
    answers a question nobody asked and is not document text."""
    if _asks_about_versions(question, plan):
        return
    for result in results:
        if result.valid and _METADATA_ECHO.search(result.text):
            result.valid = False
            result.problems.append("restates document metadata the question did not ask about")


def _evidence_texts(evidence: EvidenceSet) -> dict[str, EvidenceText]:
    texts = {
        item.id: EvidenceText(item.id, item.full_text, _provenance_numbers(item), label=_label(item.source))
        for item in evidence.items
    }
    if evidence.comparison:
        texts["D1"] = EvidenceText("D1", comparison_text(evidence.comparison))
    return texts


def _without_file_names(question: str, named: list[NamedDocument]) -> str:
    for filename in sorted({d.filename for d in named}, key=len, reverse=True):
        question = re.sub(rf"(?<![\w.-]){re.escape(filename)}(?![\w-])", "the document", question, flags=re.I)
    return question


_GREETING = re.compile(
    r"^\W*(?:(?:hi+|hello+|hey+|hiya|namaste|greetings|good\s+(?:morning|afternoon|evening|day)|"
    r"thanks?(?:\s+you)?|thank\s+you(?:\s+(?:so|very)\s+much)?|ty|ok(?:ay)?|cool|great|bye|goodbye|"
    r"how\s+are\s+you|what'?s\s+up|there|governix|sir|madam|dear|all|team)\W*)+$",
    re.IGNORECASE,
)


_WEEKDAYS = frozenset("monday tuesday wednesday thursday friday saturday sunday".split())
# Capitalised words that are ordinary words at the start of a clause or in titles of address.
_NOT_NAMES = frozenset(
    "what who when where which why how is are was were do does did can could should would will may might "
    "if the a an and or but in on at of for to from by with as please tell explain list give show compare "
    "i we you they he she it my our your their this that these those there here according under".split()
)


def named_entities(question: str) -> list[str]:
    """Places, organisations and other names the question asks about ("Kerala", "Goa").

    Capitalised words that are not the first word of a sentence, not common question words,
    and not part of a policy title the question quotes in title case (those are matched by
    the evidence checks). Acronyms are left to the term checks.
    """
    names = []
    for sentence in re.split(r"[.?!;:]\s+", question):
        words = re.findall(r"[A-Za-z][A-Za-z'’-]*", sentence)
        for index, word in enumerate(words):
            previous = words[index - 1] if index else ""
            following = words[index + 1] if index + 1 < len(words) else ""
            if (index == 0 or not word[0].isupper() or word.isupper() or len(word) < 3
                    or word.lower() in _NOT_NAMES or word.lower() in _MONTHS or word.lower() in _WEEKDAYS
                    # Part of a capitalised run ("Code of Ethics", "Gold Loan Policy"): a title.
                    or (previous[:1].isupper() and previous.lower() not in _NOT_NAMES and index > 1)
                    or following[:1].isupper()):
                continue
            names.append(word.lower())
    return list(dict.fromkeys(names))


_SEVERAL = re.compile(
    r"\b(?:and|also|as well as|along with)\s+(?:what|who|whom|when|where|which|why|how)\b"
    r"|\?\s*\S.*\?",
    re.IGNORECASE,
)


def asks_several(question: str) -> bool:
    """ "What is X and who issued it?", or two questions in one message."""
    return bool(_SEVERAL.search(question))


# A summary is a plain restatement of the claims when at least this share of its content words,
# and every figure in it, comes from them. It then cannot add knowledge, so the model check
# (a full round trip, about a second) is skipped.
RESTATEMENT_SHARE = 0.9


def restates(summary: str, claims: list[str]) -> bool:
    said = " ".join(claims)
    if {f.value for f in extract_numeric_facts(summary)} - {f.value for f in extract_numeric_facts(said)}:
        return False
    words = [w for w in re.findall(r"[a-z][a-z0-9-]{2,}", summary.lower()) if w not in _RESTATEMENT_FILLER]
    if not words:
        return True
    index = TermIndex(said)
    return sum(index.mentions(w) for w in words) / len(words) >= RESTATEMENT_SHARE


_RESTATEMENT_FILLER = frozenset(
    # "not", "no" and "never" are deliberately absent: a negation must come from the claims.
    "the and for with that this from are was were has have any all per such which their there "
    "its can may must shall should will would been being into than then also only".split()
)


def is_greeting(question: str) -> bool:
    return bool(_GREETING.match(question.strip()))


def _without_claims(run: Generator[tuple[str, Any], None, Any]) -> Generator[tuple[str, Any], None, Any]:
    """Pass a pipeline's progress through but hold back its claims; return its result."""
    while True:
        try:
            kind, data = next(run)
        except StopIteration as stop:
            return stop.value
        if kind != "claim":
            yield kind, data


def _combine(parts: list[tuple[str, AnswerResponse]]) -> AnswerResponse | None:
    """One answer from the answers to each part of a question, with one set of source numbers.

    Each part was answered and checked on its own. A part the documents do not answer is
    named, never filled in. None when no part was answered.
    """
    answered = [(q, r) for q, r in parts if r.status == "answered"]
    if not answered:
        return None
    sources: list[Source] = []
    numbers: dict[Any, int] = {}
    claims: list[Claim] = []
    conflicts: list[dict] = []
    warnings: list[str] = []
    checks: list[str] = []
    for index, (_question, response) in enumerate(answered, start=1):
        remap: dict[int, int] = {}
        for source in response.sources:
            key = source.chunk_id or f"{index}-{source.evidence_id}"
            if key not in numbers:
                numbers[key] = len(numbers) + 1
                sources.append(source.model_copy(update={"number": numbers[key], "evidence_id": f"P{index}-{source.evidence_id}"}))
            remap[source.number] = numbers[key]
        claims += [Claim(text=c.text, citations=[remap[n] for n in c.citations if n in remap]) for c in response.claims]
        conflicts += [{**c, "citations": [remap[n] for n in c.get("citations", []) if n in remap]} for c in response.conflicts]
        warnings += [w for w in response.warnings if w not in warnings]
        checks += response.plan.get("checks", [])
    warnings += [f'Not found in your documents: "{q}"' for q, r in parts if r.status != "answered"]
    summaries = [r.summary for _, r in answered if r.summary]
    first = answered[0][1]
    return AnswerResponse(
        question=first.question, status="answered",
        answer=" ".join(f"{c.text} [{', '.join(map(str, c.citations))}]" for c in claims),
        summary=" ".join(summaries) if len(summaries) == len(answered) else None,
        claims=claims, sources=sources, conflicts=conflicts, warnings=warnings,
        plan={**first.plan, "checks": checks, "parts": [q for q, _ in parts]},
        evidence_score=max(r.evidence_score for _, r in answered),
        model=first.model,
        usage={k: sum(r.usage.get(k, 0) for _, r in answered) for k in ("input_tokens", "output_tokens")},
        timings_ms={},
    )


def acronym_in_question(question: str) -> str | None:
    if match := _ACRONYM_QUESTION.search(question):
        return next(group for group in match.groups() if group).upper()
    if match := _ACRONYM_MEANING.search(question):
        return next(group for group in match.groups() if group)
    return None


def has_words(text: str) -> bool:
    """Letters or digits in any script: something a search could match."""
    return any(ch.isalnum() for ch in text)


class _NoAnswer(Exception):
    def __init__(self, reason: str, missing_terms: list[str] | None = None, suggestions: list[str] | None = None) -> None:
        self.reason = reason
        self.missing_terms = missing_terms or []
        self.suggestions = suggestions


@dataclass
class _Attempt:
    """What one retrieval-and-generation pass produced, kept even when it ends in no answer."""

    evidence: EvidenceSet = field(default_factory=lambda: EvidenceSet([], 0.0, [], 0.0, []))
    retrieved: list[uuid.UUID] = field(default_factory=list)
    llm_result: LLMResult | None = None
    claims_streamed: int = 0


class RAGService:
    def __init__(
        self,
        session: Session,
        session_factory: sessionmaker[Session],
        settings: Settings,
        cache: Cache,
        embedder: EmbeddingProvider | None,
        reranker: RerankerProvider | None,
        llm_factory: Callable[[], LLMProvider],
    ) -> None:
        self.session = session
        self.settings = settings
        self.cache = cache
        self.embedder: EmbeddingProvider | None = embedder
        self.reranker: RerankerProvider | None = reranker
        self._llm_factory: Callable[[], LLMProvider] = llm_factory
        self.retriever = HybridRetriever(session_factory, embedder)

    # --- public -----------------------------------------------------------------

    def ask(self, principal: Principal, request: AskRequest) -> AnswerResponse:
        for kind, data in self.answer_events(principal, request):
            if kind == "done":
                return data
        raise RuntimeError("The answer pipeline ended without a response.")  # pragma: no cover

    def answer_events(self, principal: Principal, request: AskRequest) -> Iterator[tuple[str, Any]]:
        """The answer pipeline as a stream of events.

            ("stage", {...})        progress: searching, reading, writing
            ("claim", {...})        a claim that has passed validation, as soon as the model finished it
            ("done", AnswerResponse) the complete, authoritative answer (identical to ask())

        Streamed claims go through the same validation and citation checks as the
        final answer and are numbered the same way, so the final response only
        confirms what was already shown.
        """
        started = time.perf_counter()
        key = self._answer_cache_key(principal, request)
        if (cached := self.cache.get(key)) is not None:
            response = AnswerResponse.model_validate(cached)
            response.cache_hit = True
            response.question = request.question
            response.query_id = self._audit(principal, request, response, retrieved=[], cache_hit=True)
            yield "done", response
            return

        timings: dict[str, float] = {}
        deadline = started + self.settings.RAG_DEADLINE_SECONDS
        yield "stage", {"stage": "searching"}

        prepared_request, rewrite, named, plan = self._prepare_request(principal, request, timings)
        # A question that asks several things ("the title and who issued it") is split first:
        # answered as one, it tends to answer only the first thing.
        restated = self._clarify(prepared_request.question, timings) if asks_several(prepared_request.question) else None
        split_first = bool(restated and len(restated) > 1)
        if split_first:
            response, retrieved = self._no_answer(prepared_request, plan, EvidenceSet([], 0.0, [], 0.0, []),
                                                  _NoAnswer("KEY_TERMS_NOT_FOUND"), None), []
        else:
            restated = None
            response, retrieved = yield from self._run_pipeline(
                principal, prepared_request, plan, rewrite, named, timings, deadline, started
            )
        if restated or (self._worth_clarifying(response, deadline) and (restated := self._clarify(prepared_request.question, timings))):
            # The words as typed found nothing ("pokucy", "hello ... in short", or two subjects
            # in one question): search again for the question(s) as the person meant them.
            # Nothing was shown yet.
            parts = []
            for question in restated:
                if time.perf_counter() >= deadline:
                    break
                part_request, _, part_named, part_plan = self._prepare_request(
                    principal, prepared_request.model_copy(update={"question": question, "history": []}), timings,
                )
                yield "stage", {"stage": "searching"}
                run = self._run_pipeline(
                    principal, part_request, part_plan, Rewrite(question), part_named, timings, deadline, started,
                )
                # Parts are numbered independently; their claims are shown once combined.
                part, part_retrieved = yield from (run if len(restated) == 1 else _without_claims(run))
                retrieved += part_retrieved
                parts.append((question, part_request, part_plan, part))
            if len(parts) == 1 and parts[0][3].status == "answered":
                _question, prepared_request, plan, response = parts[0]
                rewrite = Rewrite(_question, "clarified")
            elif len(parts) > 1 and (combined := _combine([(q, r) for q, _, _, r in parts])) is not None:
                response, plan = combined, next(p for _, _, p, r in parts if r.status == "answered")
                prepared_request = prepared_request.model_copy(update={"question": " · ".join(q for q, *_ in parts)})
                rewrite = Rewrite(prepared_request.question, "split")
            elif split_first:
                # No part could be answered on its own: try the question as asked.
                response, more = yield from self._run_pipeline(
                    principal, prepared_request, plan, rewrite, named, timings, deadline, started
                )
                retrieved += more
        response = self._decorate_and_persist(
            principal, request, prepared_request, response, plan, rewrite, retrieved, timings, started, key
        )
        yield "done", response

    def _answer_cache_key(self, principal: Principal, request: AskRequest) -> str:
        """Key on the (normalised) rewritten question plus the explicit filter parameters.

        Raw history is excluded: once resolved into a standalone question, two turns
        with identical rewrites but different history must share the same cache entry.
        """
        scope = cache_scope(self.session, principal)
        return build_cache_key(
            "rag", scope, ANSWER_CACHE_VERSION,
            " ".join(request.question.lower().split()),
            request.mode,
            request.as_of.isoformat() if request.as_of else None,
            sorted(str(v) for v in request.version_ids),
            sorted(str(p) for p in request.policy_ids),
            sorted(str(c) for c in request.category_ids),
        )

    def _prepare_request(
        self, principal: Principal, request: AskRequest, timings: dict[str, float]
    ) -> tuple[AskRequest, Any, list[NamedDocument], QueryPlan]:
        """Normalize question, resolve context/rewrite, strip named filenames, and plan query."""
        words = has_words(request.question)
        prepared, rewrite = self._standalone(request, timings) if words else (request, None)
        named = self.retriever.referenced_documents(principal, prepared.question) if words else []
        if named:
            # The file name is not document text: left in, it fails the key-term check and
            # its tokens ("v2" in "policy_v2.pdf") would be read as a version reference.
            prepared = prepared.model_copy(update={"question": _without_file_names(prepared.question, named)})
        plan = plan_query(
            prepared.question,
            ui_mode=None if prepared.mode == "auto" else prepared.mode,
            as_of=prepared.as_of,
            version_ids=prepared.version_ids,
            date_order=self.settings.DATE_ORDER,
        )
        return prepared, rewrite, named, plan

    def _run_pipeline(
        self,
        principal: Principal,
        request: AskRequest,
        plan: QueryPlan,
        rewrite: Any,
        named: list[NamedDocument],
        timings: dict[str, float],
        deadline: float,
        started: float,
    ) -> Generator[tuple[str, Any], None, tuple[AnswerResponse, list[int]]]:
        """Execute retrieval, gating, generation and version fallback while streaming stage/claim events."""
        attempt = _Attempt()
        try:
            if rewrite is None:
                raise _NoAnswer("NOT_A_QUESTION")
            if not rewrite.resolvable:
                raise _NoAnswer("NEEDS_CONTEXT")
            if is_greeting(request.question) or not self._has_subject(request.question, plan):
                # "hi", "thanks", "can you explain in short?": nothing to search for. Searching
                # anyway lets any passage pass the term checks, which have nothing to check.
                raise _NoAnswer("GREETING" if is_greeting(request.question) else "NO_SUBJECT", suggestions=[])
            referenced = request.policy_ids or self.retriever.referenced_policies(principal, request.question)
            filters = self._filters(principal, plan, request, referenced, named)
            try:
                response = yield from self._attempt(principal, request, plan, filters, attempt, timings, deadline, started)
            except _NoAnswer as no_answer:
                if named or not self._may_fall_back(request, plan, no_answer, attempt):
                    raise
                response = yield from self._previous_versions(
                    principal, request, plan, no_answer, attempt, timings, deadline, started,
                )
        except _NoAnswer as no_answer:
            response = self._no_answer(request, plan, attempt.evidence, no_answer, attempt.llm_result)
        return response, attempt.retrieved

    def _decorate_and_persist(
        self,
        principal: Principal,
        original: AskRequest,
        prepared: AskRequest,
        response: AnswerResponse,
        plan: QueryPlan,
        rewrite: Any,
        retrieved: list[int],
        timings: dict[str, float],
        started: float,
        key: str,
    ) -> AnswerResponse:
        """Decorate response with warnings, timings, audit trail, and persist into answer cache."""
        response.question = original.question
        if response.status == "answered" and plan.as_of and plan.as_of > datetime.now(UTC).date():
            response.warnings.append(
                f"{plan.as_of.isoformat()} is in the future. This is the version in force today; it may change before then."
            )
        if rewrite and rewrite.reason:
            response.plan["rewritten_question"] = prepared.question
            response.plan["rewrite_reason"] = rewrite.reason
            # Shown by the client as "Understood as: …" from plan.rewritten_question.
        timings["total"] = round((time.perf_counter() - started) * 1000, 1)
        response.timings_ms = timings
        response.query_id = self._audit(principal, original, response, retrieved=retrieved, cache_hit=False)
        self.cache.set(key, response.model_dump(mode="json"), ttl_seconds=self.settings.RAG_CACHE_TTL_SECONDS)
        return response

    # Nothing matched the words as typed: a rewording may. Not for an outage, a deadline,
    # or a question the documents were searched for and simply do not answer.
    CLARIFY_REASONS = frozenset({"KEY_TERMS_NOT_FOUND", "NO_RELEVANT_DOCUMENTS", "LOW_RELEVANCE", "ANSWER_OFF_TOPIC"})

    def _worth_clarifying(self, response: AnswerResponse, deadline: float) -> bool:
        return (
            response.status == "no_answer" and response.no_answer is not None
            and response.no_answer.reason in self.CLARIFY_REASONS
            and time.perf_counter() < deadline
        )

    def _clarify(self, question: str, timings: dict[str, float]) -> list[str] | None:
        step = time.perf_counter()
        try:
            llm = self._llm_factory()
        except LLMUnavailableError:
            return None
        clarified = restated_questions(llm, question)
        timings["clarify"] = round((time.perf_counter() - step) * 1000, 1)
        return clarified

    # --- one pass: retrieve, gate, generate --------------------------------------------

    def _attempt(
        self, principal, request, plan: QueryPlan, filters: SearchFilters, attempt: _Attempt,
        timings: dict[str, float], deadline: float, started: float, prefix: str = "",
    ) -> Generator[tuple[str, Any], None, AnswerResponse]:
        """Answer from the versions `filters` allows; raises _NoAnswer when they do not support one."""
        result = retrieve_with_variants(
            principal, self.retriever, self.embedder, self.cache, request.question, filters,
            limit=self.settings.RAG_RETRIEVAL_CANDIDATES,
        )
        timings.update({f"{prefix}retrieval_{k}": v for k, v in result.timings_ms.items()})
        attempt.retrieved += [c.chunk_id for c in result.candidates]

        step = time.perf_counter()
        attempt.evidence = evidence = build_evidence(
            self.session, principal, request.question, result.candidates,
            reranker=self.reranker, retriever=self.retriever, filters=filters,
            rerank_top_n=self.settings.RAG_RERANK_TOP_N, limit=self.settings.RAG_EVIDENCE_LIMIT,
        )
        if plan.query_class is QueryClass.COMPARISON:
            evidence.comparison = self._comparison(principal, plan)
        timings[f"{prefix}evidence"] = round((time.perf_counter() - step) * 1000, 1)

        self._gate(evidence, request.question, principal)
        yield "stage", {"stage": "reading", "passages": len(evidence.items)}

        step = time.perf_counter()
        response = self._acronym_answer(principal, request, plan, evidence, filters)
        if response is None:
            self._check_deadline(deadline, evidence)
            yield "stage", {"stage": "writing"}
            # Claims are held back until together they are on topic: the final answer
            # applies the same check, and a claim shown and then withdrawn reads as a
            # glitch ("an answer for a second, then no answer").
            held: list[dict] = []
            names: list[str] = []  # the policies cited so far
            passages: list[str] = []  # and their passages
            on_topic = evidence.comparison is not None or is_document_question(request.question)
            for part in self._generate_stream(principal, request, plan, evidence):
                if isinstance(part, LLMResult):
                    attempt.llm_result = part
                    continue
                held.append(part)
                for source in part.get("sources", []):
                    names.append(" ".join(filter(None, [source.get("policy_name"), source.get("document_title"), source.get("section_path")])))
                    passages.append(source.get("excerpt") or "")
                if not on_topic and self._off_topic(principal, request.question, [c["text"] for c in held] + names, passages) is not None:
                    continue
                on_topic = True
                timings.setdefault("first_claim", round((time.perf_counter() - started) * 1000, 1))
                for claim in held:
                    attempt.claims_streamed += 1
                    yield "claim", claim
                held = []
            response = self._validated_answer(principal, request, plan, evidence, attempt.llm_result)
        timings[f"{prefix}llm"] = round((time.perf_counter() - step) * 1000, 1)
        if attempt.llm_result is not None:
            timings[f"{prefix}validation"] = round((time.perf_counter() - step) * 1000, 1)
        return response

    # --- previous-version fallback ------------------------------------------------------

    def _may_fall_back(self, request: AskRequest, plan: QueryPlan, no_answer: _NoAnswer, attempt: _Attempt) -> bool:
        """Only a question about the current policy, left without support, looks at earlier versions.

        An explicit UI choice ("Current", a date, a version) is honoured as asked,
        and a streamed claim is never contradicted by a second answer.
        """
        return (
            self.settings.RAG_PREVIOUS_VERSION_FALLBACK
            and request.mode == "auto"
            and plan.query_class is QueryClass.CURRENT
            and plan.as_of is not None
            and no_answer.reason in FALLBACK_REASONS
            and attempt.claims_streamed == 0
        )

    def _previous_versions(
        self, principal, request, plan: QueryPlan, first: _NoAnswer, attempt: _Attempt,
        timings: dict[str, float], deadline: float, started: float,
    ) -> Generator[tuple[str, Any], None, AnswerResponse]:
        for depth, version_ids in enumerate(self._previous_version_sets(principal, request, plan.as_of), start=1):
            # Refuse to start another full pipeline pass if the deadline is already
            # exhausted: the LLM call would immediately time out anyway, and the
            # _check_deadline inside _attempt fires *before* the call, not after.
            # Raising here lets the outer handler emit a clean DEADLINE_EXCEEDED
            # no-answer rather than a timed-out fallback attempt.
            if time.perf_counter() >= deadline:
                raise _NoAnswer("DEADLINE_EXCEEDED")
            yield "stage", {"stage": "searching_previous_versions", "depth": depth}
            filters = SearchFilters(
                version_scope=VersionScope("versions", version_ids=version_ids),
                policy_ids=list(request.policy_ids), category_ids=list(request.category_ids),
            )
            fallback = _Attempt()
            try:
                response = yield from self._attempt(
                    principal, request, plan, filters, fallback, timings, deadline, started, prefix=f"previous_{depth}_",
                )
            except _NoAnswer as no_answer:
                attempt.retrieved += fallback.retrieved
                if no_answer.reason not in FALLBACK_REASONS or fallback.claims_streamed:
                    raise  # an outage or the deadline ends the search; so does a streamed claim
                continue
            attempt.retrieved += fallback.retrieved
            attempt.llm_result = fallback.llm_result
            self._mark_previous_version(response, set(version_ids), depth, first.reason)
            return response
        raise first

    def _previous_version_sets(self, principal: Principal, request: AskRequest, as_of: date) -> list[list[uuid.UUID]]:
        """Superseded versions grouped by how far back they are: [[each policy's previous], [the one before], ...].

        Only policies with a version in force on `as_of` take part, so an expired
        policy never answers a question about the current rules.
        """
        in_force = select(PolicyVersion.policy_id).where(
            PolicyVersion.status == VersionStatus.ACTIVE, effective_on(as_of)
        )
        query = (
            select(
                PolicyVersion.id,
                func.row_number().over(
                    partition_by=PolicyVersion.policy_id, order_by=PolicyVersion.effective_from.desc()
                ).label("depth"),
            )
            .join(Policy, Policy.id == PolicyVersion.policy_id)
            .where(
                PolicyVersion.status == VersionStatus.ACTIVE,
                PolicyVersion.effective_to.is_not(None),
                PolicyVersion.effective_to <= as_of,
                PolicyVersion.policy_id.in_(in_force),
                visible_clause(principal, Policy.organization_id, Policy.branch_id, Policy.department_id, Policy.id),
            )
        )
        if request.policy_ids:
            query = query.where(Policy.id.in_(request.policy_ids))
        if request.category_ids:
            query = query.where(Policy.category_id.in_(request.category_ids))
        ranked = query.subquery()
        rows = self.session.execute(
            select(ranked.c.id, ranked.c.depth).where(ranked.c.depth <= self.settings.RAG_FALLBACK_MAX_DEPTH)
        ).all()
        by_depth: dict[int, list[uuid.UUID]] = {}
        for version_id, depth in rows:
            by_depth.setdefault(depth, []).append(version_id)
        return [by_depth[d] for d in sorted(by_depth)]

    @staticmethod
    def _mark_previous_version(response: AnswerResponse, version_ids: set[uuid.UUID], depth: int, reason: str) -> None:
        cited = []
        for source in response.sources:
            if source.version_id in version_ids:
                source.previous_version = True
                note = f"{source.policy_name} v{source.version_label}"
                if source.effective_from:
                    until = source.effective_to.isoformat() if source.effective_to else "present"
                    note += f" (in force {source.effective_from.isoformat()} to {until})"
                if note not in cited:
                    cited.append(note)
        response.warnings.insert(0, (
            "The version in force today does not cover this, so this answer comes from an earlier version: "
            + "; ".join(cited) + "."
        ))
        response.plan["fallback"] = {"used": True, "depth": depth, "reason": reason}
        response.plan["explanation"] = "The current version had no supporting evidence; answered from a previous version"

    # --- planning ---------------------------------------------------------------

    def _standalone(self, request: AskRequest, timings: dict[str, float]):
        """The request with a standalone English question (follow-ups resolved, translated)."""
        step = time.perf_counter()
        try:
            llm = self._llm_factory()
        except LLMUnavailableError:
            logger.warning("LLM unavailable for query rewrite; proceeding with the raw question")
            llm = None
        rewrite = standalone_question(llm, request.question, request.history)
        if rewrite.reason:
            timings["rewrite"] = round((time.perf_counter() - step) * 1000, 1)
        if rewrite.question != request.question:
            request = request.model_copy(update={"question": rewrite.question})
        return request, rewrite

    def _filters(
        self, principal: Principal, plan: QueryPlan, request: AskRequest, referenced: list[uuid.UUID],
        named: list[NamedDocument],
    ) -> SearchFilters:
        named_versions = list(dict.fromkeys(d.version_id for d in named if d.version_id))
        if named_versions and request.mode == "auto" and plan.query_class is QueryClass.CURRENT:
            # Naming a file asks about that file, even when a newer version has replaced it.
            files = ", ".join(dict.fromkeys(d.filename for d in named))
            plan.query_class, plan.mode, plan.as_of = QueryClass.SPECIFIC_VERSION, "versions", None
            plan.version_ids, plan.explanation = named_versions, f"Question names the file {files}"
        if plan.mode == "versions":
            if not plan.version_ids:
                plan.version_ids = self._resolve_labels(principal, plan, referenced)
            visible = self._visible_versions(principal, plan.version_ids)
            if not visible:
                raise _NoAnswer("VERSION_NOT_FOUND")
            plan.version_ids = visible
            scope = VersionScope("versions", version_ids=visible)
        else:
            scope = VersionScope("as_of", as_of=plan.as_of)
        return SearchFilters(
            version_scope=scope,
            policy_ids=list(request.policy_ids),
            category_ids=list(request.category_ids),
            document_ids=[d.document_id for d in named],
        )

    def _resolve_labels(self, principal: Principal, plan: QueryPlan, referenced: list[uuid.UUID]) -> list[uuid.UUID]:
        query = select(PolicyVersion).join(Policy, Policy.id == PolicyVersion.policy_id).where(
            PolicyVersion.status == VersionStatus.ACTIVE,
            visible_clause(principal, Policy.organization_id, Policy.branch_id, Policy.department_id, Policy.id),
        )
        if referenced:
            query = query.where(PolicyVersion.policy_id.in_(referenced))
        if plan.version_labels:
            versions = self.session.scalars(query.where(PolicyVersion.version_label.in_(plan.version_labels))).all()
            if plan.query_class is QueryClass.COMPARISON and len({v.policy_id for v in versions}) > 1:
                raise _NoAnswer("COMPARISON_TARGET_UNCLEAR")
            return [v.id for v in versions]
        if plan.query_class is QueryClass.COMPARISON:
            if len(referenced) != 1:
                raise _NoAnswer("COMPARISON_TARGET_UNCLEAR")
            latest = self.session.scalars(query.order_by(PolicyVersion.effective_from.desc()).limit(2)).all()
            return [v.id for v in latest]
        return []

    def _visible_versions(self, principal: Principal, version_ids: list[uuid.UUID]) -> list[uuid.UUID]:
        if not version_ids:
            return []
        rows = self.session.execute(
            select(PolicyVersion.id).join(Policy, Policy.id == PolicyVersion.policy_id).where(
                PolicyVersion.id.in_(version_ids),
                visible_clause(principal, Policy.organization_id, Policy.branch_id, Policy.department_id, Policy.id),
            )
        ).scalars().all()
        return list(rows)

    def _comparison(self, principal: Principal, plan: QueryPlan) -> dict | None:
        # Fetch both versions in a single query instead of two serial session.get() calls.
        versions = list(self.session.scalars(
            select(PolicyVersion).where(PolicyVersion.id.in_(plan.version_ids))
        ).all())
        versions = [v for v in versions if v is not None]
        if len(versions) != 2 or versions[0].policy_id != versions[1].policy_id:
            raise _NoAnswer("COMPARISON_TARGET_UNCLEAR")
        older, newer = sorted(versions, key=lambda v: v.effective_from)
        return compare_versions(self.session, older, newer, include_content=False)

    # --- gate, generation, validation ------------------------------------------------

    def _gate(self, evidence: EvidenceSet, question: str, principal: Principal) -> None:
        if evidence.comparison is not None:
            return  # the deterministic diff is itself sufficient evidence
        if not evidence.items:
            raise _NoAnswer("NO_RELEVANT_DOCUMENTS")
        if evidence.top_score < self.settings.RAG_MIN_EVIDENCE_SCORE:
            raise _NoAnswer("LOW_RELEVANCE")
        # Questions about the document itself ("the exact title, issuing authority
        # and legal basis") are phrased in words the cover page does not use;
        # the cover is in the evidence and the model + validator decide.
        if is_document_question(question):
            return
        if evidence.coverage < self.settings.RAG_MIN_TERM_COVERAGE:
            raise _NoAnswer("KEY_TERMS_NOT_FOUND", evidence.missing_terms)
        salient, missing = self._salient_coverage(principal, question, evidence)
        if salient < self.settings.RAG_MIN_SALIENT_COVERAGE:
            raise _NoAnswer("KEY_TERMS_NOT_FOUND", missing)

    def _salient_coverage(self, principal: Principal, question: str, evidence: EvidenceSet) -> tuple[float, list[str]]:
        """Share of the question's distinguishing weight (IDF) that the evidence covers.

        Plain coverage counts every term alike, so "prepayment charges on home
        loans" passes on evidence about home loans that never mentions
        prepayment. Weighting by rarity makes the term that picks out the
        subject decide, which is also what sends such a question on to an
        earlier version that does cover it.
        """
        if not evidence.missing_terms:
            return 1.0, []
        weights = self.retriever.visible_term_weights(principal, key_terms(question))
        total = sum(weights.values())
        if total <= 0:
            return 1.0, []
        missing = [t for t in weights if t in set(evidence.missing_terms)]
        return 1 - sum(weights[t] for t in missing) / total, missing

    def _check_on_topic(self, principal: Principal, question: str, said: list[str], cited: list[str]) -> None:
        if (missing := self._off_topic(principal, question, said, cited)) is not None:
            raise _NoAnswer("ANSWER_OFF_TOPIC", missing)

    def _off_topic(self, principal: Principal, question: str, said: list[str], cited: list[str]) -> list[str] | None:
        """The question's terms the answer leaves out, when too much is left out; None when on topic.

        `said` is the answer's sentences and the names of the policies they cite; `cited` is
        the passages they cite. Each claim is true to its evidence, but true statements about
        something else are not an answer:

        * the distinguishing weight of the question's terms (as for the evidence gate) must be
          mostly covered by what the answer says, or the policies it names ("the maximum rate on
          microfinance loans" does not answer a question about deposits);
        * a place, organisation or other name the question asks about ("Kerala", "Goa") must
          appear in the answer or in what it cites: general facts that never mention it are not
          an answer about it.
        """
        everything = TermIndex(" ".join(said + cited))
        absent = [name for name in named_entities(question) if not everything.mentions(name)]
        if absent:
            return absent
        # A date in the question chose the version; the answer need not repeat it.
        terms = [t for t in key_terms(question) if not t.isdigit() and t not in _MONTHS]
        weights = self.retriever.visible_term_weights(principal, terms)
        total = sum(weights.values())
        if total <= 0:
            return None
        spoken = TermIndex(" ".join(said))
        missing = [t for t in weights if not spoken.mentions(t)]
        if 1 - sum(weights[t] for t in missing) / total < self.settings.RAG_MIN_SALIENT_COVERAGE:
            return missing
        return None

    def _check_deadline(self, deadline: float, evidence: EvidenceSet) -> None:
        """Abandon an answer that has already spent its whole budget.

        Retrieval and evidence building are bounded (statement timeouts, lane
        limits). The LLM call is the one unbounded step, so the deadline is
        enforced here, before paying for it. Answering from the retrieved
        evidence without the model is not an option: the response contract
        requires validated claims.
        """
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise _NoAnswer("DEADLINE_EXCEEDED")

    def _generate_stream(self, principal, request, plan: QueryPlan, evidence: EvidenceSet) -> Iterator[dict | LLMResult]:
        """Yield each claim that validates, as the model finishes it, then the LLMResult."""
        question = request.question
        items = evidence.by_id()
        texts = _evidence_texts(evidence)
        claims = ClaimStream()
        numbering: dict[str, int] = {}
        try:
            llm: LLMProvider = self._llm_factory()
            context_items = [{"id": i.id, "text": i.full_text} for i in evidence.items]
            if evidence.comparison:
                context_items.insert(0, {"id": "D1", "text": comparison_text(evidence.comparison)})
            for part in llm.stream_json(
                SYSTEM_PROMPT, build_user_prompt(question, plan, evidence), OUTPUT_SCHEMA,
                context={"question": question, "evidence": context_items, "conflicts": evidence.conflicts},
            ):
                if isinstance(part, LLMResult):
                    yield part
                    return
                for raw in claims.feed(part):
                    if (claim := self._streamed_claim(principal, request, plan, evidence, raw, texts, items, numbering)):
                        yield claim
        except LLMUnavailableError as exc:
            logger.error("LLM unavailable: %s", exc)
            raise _NoAnswer("LLM_UNAVAILABLE") from None

    def _streamed_claim(self, principal, request, plan, evidence, raw, texts, items, numbering) -> dict | None:
        """One claim checked exactly as the final answer checks it, or None if it fails."""
        results = validate_claims([raw], texts, key_terms(request.question), request.question)
        if not results:
            return None
        _drop_metadata_echo(results, request.question, plan)
        result = results[0]
        if not result.valid:
            return None
        verified = self._verify_citations(principal, {e for e in result.evidence_ids if e != "D1"}, items)
        if not all(e == "D1" or e in verified for e in result.evidence_ids):
            return None
        new_sources = []
        for evidence_id in result.evidence_ids:
            if evidence_id not in numbering:
                numbering[evidence_id] = len(numbering) + 1
                new_sources.append(self._source(numbering[evidence_id], evidence_id, items, evidence).model_dump(mode="json"))
        return {"text": result.text, "citations": [numbering[e] for e in result.evidence_ids], "sources": new_sources}

    def _acronym_answer(self, principal, request, plan, evidence: EvidenceSet, filters) -> AnswerResponse | None:
        """Answer an explicit acronym question from an exact glossary row.

        Glossaries are often label/value tables, so an LLM may incorrectly
        regard a row as insufficient prose evidence. This path is deliberately
        narrow: both the abbreviation and its adjacent expansion must occur
        together in one ACL-filtered, in-scope chunk.

        The ranked evidence set is tried first, then an exact-pattern lookup.
        A glossary table is frequently split mid-table by the chunker, leaving
        the row a person can see in the PDF in a tiny chunk of its own that
        relevance ranking never selects; the lookup finds it anyway.
        """
        acronym = acronym_in_question(request.question)
        if not acronym:
            return None
        rows = self.retriever.glossary_rows(principal, acronym, filters)
        glossary = {f"G{i}": _glossary_item(f"G{i}", candidate) for i, candidate in enumerate(rows, start=1)}
        if glossary:
            glossary = self._refresh_provenance(glossary)
        candidates = list(evidence.items) + list(glossary.values())
        # A glossary/definition row is authoritative; an inline "(STATCOMs)" in
        # prose is used only when no row defines the term.
        for inline in (False, True):
            if response := self._acronym_from(principal, request, plan, evidence, acronym, candidates, inline):
                return response
        return None

    def _acronym_from(self, principal, request, plan, evidence, acronym, items, inline) -> AnswerResponse | None:
        for item in items:
            expansion = _acronym_expansion(acronym, item.full_text, inline=inline)
            if not expansion:
                continue
            if item.id not in self._verify_citations(principal, {item.id}, {item.id: item}):
                continue
            claim = Claim(text=f"{acronym} stands for {expansion}.", citations=[1])
            return AnswerResponse(
                question=request.question, status="answered", answer=f"{claim.text} [1]",
                claims=[claim], sources=[self._source(1, item.id, {item.id: item}, evidence)],
                conflicts=[], warnings=[], plan=plan.describe(), evidence_score=evidence.top_score,
                model="deterministic-acronym", usage={}, timings_ms={},
            )
        return None

    def _refresh_provenance(self, items: dict) -> dict:
        """Attach real provenance to glossary rows fetched outside the evidence set."""
        sources = provenance(self.session, [item.candidate for item in items.values()])
        for item in items.values():
            if source := sources.get(item.candidate.chunk_id):
                item.source = source
        return items

    def _validated_answer(self, principal, request, plan, evidence: EvidenceSet, llm_result) -> AnswerResponse:
        items = evidence.by_id()
        texts = _evidence_texts(evidence)
        content = llm_result.content if llm_result else {}
        results = validate_claims(content.get("claims", []), texts, key_terms(request.question), request.question)
        _drop_metadata_echo(results, request.question, plan)
        # What the checks removed is for the audit trail and debugging, not the reader:
        # the answer they see is already only what passed.
        if any(ABSENCE_PROBLEM in r.problems for r in results):
            warnings_first = ["Part of your question is not covered by your documents."]
        else:
            warnings_first = []
        checks = [f"Removed an unsupported statement: {', '.join(r.problems)}" for r in results if not r.valid]
        checks += [f"Ignored {p}" for r in results if r.valid for p in r.problems]
        warnings: list[str] = warnings_first
        valid = [r for r in results if r.valid]

        verified = self._verify_citations(principal, {e for r in valid for e in r.evidence_ids if e != "D1"}, items)
        valid = [r for r in valid if all(e == "D1" or e in verified for e in r.evidence_ids)]
        if not valid:
            if content.get("insufficient_evidence") or not results:
                raise _NoAnswer("INSUFFICIENT_EVIDENCE")
            raise _NoAnswer("ANSWER_FAILED_VALIDATION")
        if evidence.comparison is None and not is_document_question(request.question):
            cited = [e for e in dict.fromkeys(e for r in valid for e in r.evidence_ids) if e in texts]
            self._check_on_topic(principal, request.question, [r.text for r in valid] + [texts[e].label for e in cited],
                                 [texts[e].text for e in cited])

        numbering: dict[str, int] = {}
        for result in valid:
            for evidence_id in result.evidence_ids:
                numbering.setdefault(evidence_id, len(numbering) + 1)
        claims = [Claim(text=r.text, citations=[numbering[e] for e in r.evidence_ids]) for r in valid]
        sources = [self._source(n, e, items, evidence) for e, n in numbering.items()]

        conflicts = list(evidence.conflicts)
        for noted in content.get("conflicts", []):
            ids = [e for e in noted.get("evidence_ids", []) if e in items]
            if ids and not any(set(ids) == set(c["evidence_ids"]) for c in conflicts):
                conflicts.append({"type": "NOTED_BY_MODEL", "description": noted.get("description", ""), "evidence_ids": ids})
        # Show a disagreement only between sources the answer actually cites; an amendment
        # whenever the amended clause is cited, since the reader is relying on it.
        conflicts = [
            c for c in conflicts if c["evidence_ids"] and (
                c["evidence_ids"][0] in numbering if c["type"] == "AMENDED"
                else all(e in numbering for e in c["evidence_ids"])
            )
        ]
        for conflict in conflicts:
            conflict["citations"] = [numbering[e] for e in conflict["evidence_ids"] if e in numbering]

        answer = " ".join(f"{c.text} [{', '.join(map(str, c.citations))}]" for c in claims)
        summary = self._summary(request.question, content.get("summary"), valid, texts, claims)
        if llm_result and llm_result.model == LocalLLM.model_id and self.settings.LLM_PROVIDER != "local":
            warnings.insert(0, "The AI service was unavailable, so this answer quotes the documents directly.")
        if conflicts:
            warnings.insert(0, "Your sources disagree on part of this. Check both before relying on it.")
        return AnswerResponse(
            question=request.question, status="answered", answer=answer, summary=summary, claims=claims, sources=sources,
            conflicts=conflicts, warnings=warnings, plan={**plan.describe(), "checks": checks}, evidence_score=evidence.top_score,
            model=llm_result.model if llm_result else None,
            usage={"input_tokens": llm_result.input_tokens, "output_tokens": llm_result.output_tokens} if llm_result else {},
            timings_ms={},
        )

    def _summary(self, question: str, text, valid: list, texts: dict[str, EvidenceText], claims: list[Claim]) -> str | None:
        """The plain-words answer, or None. It is dropped, never repaired, when it fails.

        It must pass the claim checks against the evidence its claims cite, and then
        a second model call must find every statement in it supported by those
        verified claims. Word overlap alone cannot tell a faithful paraphrase from
        one that adds background knowledge or an opinion ("which may benefit
        customers"); the second check can.
        """
        if not isinstance(text, str) or not text.strip():
            return None
        cited = list(dict.fromkeys(e for r in valid for e in r.evidence_ids))
        [result] = validate_claims([{"text": text, "evidence_ids": cited}], texts, key_terms(question), question)
        if not result.valid:
            return None
        if restates(result.text, [claim.text for claim in claims]):
            return result.text  # nothing in it the verified claims do not already say: no second call
        statements = "\n".join(f"- {claim.text}" for claim in claims)
        try:
            check = self._llm_factory().generate_json(
                SUMMARY_CHECK_PROMPT,
                f"Question: {question}\n\nVerified statements:\n{statements}\n\nShort answer: {result.text}",
                SUMMARY_CHECK_SCHEMA,
            )
        except LLMUnavailableError:
            return None
        content = check.content or {}
        if content.get("supported") is not True or content.get("unsupported"):
            logger.info("Dropped an unsupported summary: %s", content.get("unsupported"))
            return None
        return result.text

    def _verify_citations(self, principal: Principal, evidence_ids: set[str], items) -> set[str]:
        """Re-check in the database that every cited chunk exists, is READY and visible."""
        chunk_ids = {items[e].candidate.chunk_id: e for e in evidence_ids if e in items}
        if not chunk_ids:
            return set()
        rows = self.session.execute(
            select(Chunk.id, Chunk.page_start, Chunk.page_end, Chunk.version_id)
            .join(Document, Document.id == Chunk.document_id)
            .where(
                Chunk.id.in_(chunk_ids),
                Document.status == DocumentStatus.READY,
                visible_clause(principal, Chunk.organization_id, Chunk.branch_id, Chunk.department_id, Chunk.policy_id),
            )
        ).all()
        verified = set()
        for chunk_id, page_start, page_end, version_id in rows:
            evidence_id = chunk_ids[chunk_id]
            source = items[evidence_id].source
            if (page_start, page_end, version_id) == (source.page_start, source.page_end, source.version_id):
                verified.add(evidence_id)
        return verified

    def _source(self, number: int, evidence_id: str, items, evidence: EvidenceSet) -> Source:
        if evidence_id == "D1":
            comparison = evidence.comparison
            return Source(
                number=number, evidence_id="D1", kind="comparison",
                policy_name=None, excerpt=comparison_text(comparison)[:1500],
                version_label=f"{comparison['from_version']['label']} → {comparison['to_version']['label']}",
            )
        item = items[evidence_id]
        source = item.source
        return Source(
            number=number, evidence_id=evidence_id, chunk_id=item.candidate.chunk_id,
            document_id=source.document_id, document_title=source.document_title,
            policy_id=source.policy_id, policy_name=source.policy_name, version_id=source.version_id,
            version_label=source.version_label, effective_from=source.effective_from,
            effective_to=source.effective_to, section_number=source.section_number,
            section_path=source.section_path, page_start=source.page_start, page_end=source.page_end,
            category=item.category_name, authority_rank=item.authority_rank, excerpt=item.candidate.text,
        )

    def _no_answer(self, request, plan, evidence, no_answer: _NoAnswer, llm_result) -> AnswerResponse:
        messages = {
            "NOT_A_QUESTION": "Please type your question in words, for example the policy or topic and what you want to know.",
            "GREETING": (
                "Hello! I answer questions about your organization's policies and documents, "
                "with a citation for every point. What would you like to know?"
            ),
            "NO_SUBJECT": "What would you like to know? Please name the policy or topic you are asking about.",
            "LLM_UNAVAILABLE": "The answering service is currently unavailable. Please try again later.",
            "COMPARISON_TARGET_UNCLEAR": "Please name the policy (and versions) you want to compare.",
            "NEEDS_CONTEXT": (
                "This question refers to an earlier part of the conversation that is not available. "
                "Please restate it with the subject you mean (for example, which discussion, figure or policy)."
            ),
            "VERSION_NOT_FOUND": "That version could not be found among the documents you can access.",
            "DEADLINE_EXCEEDED": (
                "This took too long to answer. Try narrowing the question or naming a policy."
            ),
        }
        return AnswerResponse(
            question=request.question, status="no_answer", answer=None, claims=[], sources=[],
            conflicts=[], warnings=[],
            no_answer=NoAnswer(
                reason=no_answer.reason,
                message=messages.get(no_answer.reason, NO_ANSWER_MESSAGE),
                suggestions=no_answer.suggestions if no_answer.suggestions is not None else SUGGESTIONS,
                # An off-topic answer leaves out words the documents do contain; listing
                # them as "not mentioned in your documents" would mislead.
                missing_terms=[] if no_answer.reason == "ANSWER_OFF_TOPIC" else no_answer.missing_terms,
            ),
            plan=plan.describe(), evidence_score=evidence.top_score,
            model=llm_result.model if llm_result else None,
            usage={"input_tokens": llm_result.input_tokens, "output_tokens": llm_result.output_tokens} if llm_result else {},
            timings_ms={},
        )

    @staticmethod
    def _has_subject(question: str, plan: QueryPlan) -> bool:
        """The question names something to look for, or asks about a document or its versions."""
        return bool(
            key_terms(question) or is_document_question(question) or acronym_in_question(question)
            or plan.query_class is not QueryClass.CURRENT or plan.version_ids
        )

    # --- audit ---------------------------------------------------------------------

    def _audit(self, principal, request, response: AnswerResponse, *, retrieved, cache_hit: bool) -> uuid.UUID:
        event = record_event(
            self.session, "ai.query", actor=principal, resource_type="ai_query",
            details={
                "question": request.question,
                "mode": request.mode,
                "plan": response.plan,
                "status": response.status,
                "no_answer_reason": response.no_answer.reason if response.no_answer else None,
                "retrieved_chunk_ids": [str(c) for c in retrieved[:50]],
                "cited_chunk_ids": [str(s.chunk_id) for s in response.sources if s.chunk_id],
                "citations": [
                    {"n": s.number, "document_id": str(s.document_id) if s.document_id else None,
                     "version": s.version_label, "section": s.section_number, "pages": [s.page_start, s.page_end]}
                    for s in response.sources
                ],
                "answer": response.answer,
                "warnings": response.warnings,
                "conflicts": len(response.conflicts),
                "model": response.model,
                "usage": response.usage,
                "latency_ms": response.timings_ms.get("total"),
                "cache_hit": cache_hit,
                "permission_scope": principal.permission_scope,
                "asked_at": datetime.now(UTC).isoformat(),
            },
        )
        # flush() stages the INSERT in the current transaction without committing it.
        # The outer transaction (owned by the FastAPI dependency or the streaming
        # session context) is committed exactly once at the end of the request,
        # so a mid-stream crash cannot commit the audit row while leaving other
        # writes uncommitted. The streaming path owns its own session and still
        # exits cleanly via the `with session_factory() as session` context.
        self.session.flush()
        return event.id


def _label(source) -> str:
    return " ".join(filter(None, [source.policy_name, source.document_title, source.section_path]))


def _provenance_numbers(item) -> set[tuple[str, str]]:
    """Numbers an answer may state from the evidence's provenance (not its text)."""
    source = item.source
    allowed: set[tuple[str, str]] = set()
    for raw in (source.section_number, source.version_label, str(source.page_start), str(source.page_end), source.section_path):
        if raw:
            allowed |= {f.key for f in extract_numeric_facts(raw)}
    for when in (source.effective_from, source.effective_to):
        if when:
            allowed.add(("date", when.isoformat()))
    for amendment in item.amended_by:
        allowed.add(("date", amendment["effective_from"]))
        for clause in amendment["clauses"]:
            allowed |= {f.key for f in extract_numeric_facts(clause)}
    return allowed


def _is_acronym_expansion(value: str) -> bool:
    """Reject adjacent prose; accept a short human-readable glossary value."""
    words = re.findall(r"[A-Za-z][A-Za-z-]*", value)
    return 2 <= len(words) <= 12 and len(value) <= 160


_CONNECTORS = frozenset("of and for the in on to by with & a an".split())


def _matches_acronym(acronym: str, expansion: str) -> bool:
    """The expansion spells the acronym: same first letter, letters in order.

    "STATCOM" <- "STATic COMpensator", "TBCB" <- "Tariff Based Competitive Bidding".
    Rejects a neighbouring table cell such as "TBCB | Under Bidding 2028-29".
    """
    letters = re.sub(r"[^a-z]", "", acronym.lower())
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z-]*", expansion) if w.lower() not in _CONNECTORS]
    if not letters or not words or words[0][0].lower() != letters[0]:
        return False
    remaining = iter(re.sub(r"[^a-z]", "", " ".join(words).lower()))
    return all(letter in remaining for letter in letters)


def _valid_expansion(acronym: str, expansion: str) -> bool:
    # A glossary value defines the term and does not repeat it. Prose that
    # restates the acronym ("HVDC is used for bulk power...") is a sentence
    # about the subject, not its expansion.
    return (
        _is_acronym_expansion(expansion)
        and not re.search(rf"\b{re.escape(acronym)}\b", expansion, re.I)
        and _matches_acronym(acronym, expansion)
    )


def _cell(value: str) -> str:
    """A table cell without its column label ("Expansion: High Voltage" -> "High Voltage")."""
    return re.sub(r"^[^:|]{1,40}:\s*", "", value.strip()).strip()


def _singular(phrase: str) -> str:
    head, _, last = phrase.rpartition(" ")
    if last.lower().endswith("ies"):
        last = last[:-3] + "y"
    elif last.lower().endswith("s") and not last.lower().endswith("ss"):
        last = last[:-1]
    return f"{head} {last}".strip()


def _acronym_expansion(acronym: str, text: str, *, inline: bool = True) -> str:
    """The expansion of `acronym` defined in `text`, or "".

    Handles every layout the extractor produces: "HVDC - High Voltage Direct
    Current" on one line, the label/value stack "HVDC\\nHigh Voltage Direct
    Current", table rows "HVDC | High Voltage Direct Current" (optionally with
    column labels), and inline definitions "State Transmission Utilities (STUs)".
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        cells = [_cell(c) for c in line.split(" | ")] if " | " in line else []
        if len(cells) >= 2 and cells[0].upper() == acronym:
            expansion = cells[1]
        elif same_line := re.fullmatch(rf"{re.escape(acronym)}\s*(?:[-—–:]\s*|\s{{2,}})(.+)", line, re.IGNORECASE):
            expansion = same_line.group(1).strip()
        elif line.upper() == acronym and index + 1 < len(lines):
            expansion = lines[index + 1]
        else:
            continue
        if _valid_expansion(acronym, expansion):
            return expansion
    return _inline_expansion(acronym, text) if inline else ""


def _inline_expansion(acronym: str, text: str) -> str:
    """ "... State Transmission Utilities (STUs)" -> "State Transmission Utility"."""
    for match in re.finditer(rf"\(\s*{re.escape(acronym)}(s?)\s*\)", text):
        words = re.findall(r"[A-Za-z][\w&-]*", text[max(0, match.start() - 160):match.start()])
        for size in range(2, min(len(words), 12) + 1):
            phrase = " ".join(words[-size:])
            if words[-size][0].isupper() and _valid_expansion(acronym, phrase):
                return _singular(phrase) if match.group(1) else phrase
    return ""


def _glossary_item(identifier: str, candidate) -> EvidenceItem:
    """An evidence item for a glossary row found outside the ranked evidence set.

    Provenance is filled in afterwards from the database; the candidate already
    carries everything needed to identify the chunk.
    """
    return EvidenceItem(
        id=identifier, candidate=candidate, score=candidate.fused, rerank_score=0.0,
        source=Provenance(
            document_id=candidate.document_id, document_title="", policy_id=candidate.policy_id,
            policy_name=None, category_id=None, version_id=candidate.version_id,
            version_label=None, effective_from=None, effective_to=None,
            section_number=candidate.section_number, section_path=candidate.section_path,
            page_start=candidate.page_start, page_end=candidate.page_end,
        ),
    )
