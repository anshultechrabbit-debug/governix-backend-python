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
(expired) are never used this way.

Retrieve first, verify evidence, generate second. The LLM never searches and
never decides permissions; its output is discarded unless it validates.
"""

import logging
import re
import time
import uuid
from collections.abc import Generator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.infrastructure.ai.llm.base import LLMProvider, LLMResult, LLMUnavailableError
from app.infrastructure.cache.base import Cache, build_cache_key
from app.modules.audit.service import record_event
from app.modules.auth.acl import visible_clause
from app.modules.auth.permissions import Principal
from app.modules.citations.numerics import extract_numeric_facts
from app.modules.documents.model import Document, DocumentStatus
from app.modules.policies.model import Policy, PolicyVersion, VersionStatus
from app.modules.rag.claim_stream import ClaimStream
from app.modules.rag.evidence import EvidenceItem, EvidenceSet, build_evidence, is_document_question, key_terms
from app.modules.rag.prompts import OUTPUT_SCHEMA, SYSTEM_PROMPT, build_user_prompt, comparison_text
from app.modules.rag.query_plan import QueryClass, QueryPlan, plan_query
from app.modules.rag.query_rewrite import standalone_question
from app.modules.rag.schema import AnswerResponse, AskRequest, Claim, NoAnswer, Source
from app.modules.rag.validation import EvidenceText, validate_claims
from app.modules.search.model import Chunk
from app.modules.search.retrieval import HybridRetriever, SearchFilters, VersionScope
from app.modules.search.schema import Provenance
from app.modules.search.service import cache_scope, provenance, retrieve_with_variants
from app.modules.versions.timeline import compare_versions, effective_on

logger = logging.getLogger(__name__)

SUGGESTIONS = [
    "Try another policy name or its policy number",
    "Specify a date or version (e.g. 'as of March 2025' or 'v3')",
    "Ask a more specific question",
]
NO_ANSWER_MESSAGE = "I couldn't find sufficient supporting information in the available documents."
# Bump when the answer-generation or validation contract changes so cached
# answers (including cached no-answers) are recomputed under the new contract.
ANSWER_CACHE_VERSION = "v10"
# No supported answer in the version in force: worth looking one version back.
FALLBACK_REASONS = frozenset({
    "NO_RELEVANT_DOCUMENTS", "LOW_RELEVANCE", "KEY_TERMS_NOT_FOUND", "INSUFFICIENT_EVIDENCE",
    "ANSWER_FAILED_VALIDATION",
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
    texts = {item.id: EvidenceText(item.id, item.full_text, _provenance_numbers(item)) for item in evidence.items}
    if evidence.comparison:
        texts["D1"] = EvidenceText("D1", comparison_text(evidence.comparison))
    return texts


def acronym_in_question(question: str) -> str | None:
    if match := _ACRONYM_QUESTION.search(question):
        return next(group for group in match.groups() if group).upper()
    if match := _ACRONYM_MEANING.search(question):
        return next(group for group in match.groups() if group)
    return None


class _NoAnswer(Exception):
    def __init__(self, reason: str, missing_terms: list[str] | None = None) -> None:
        self.reason = reason
        self.missing_terms = missing_terms or []


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
        embedder,
        reranker,
        llm_factory,
    ) -> None:
        self.session = session
        self.settings = settings
        self.cache = cache
        self.embedder = embedder
        self.reranker = reranker
        self._llm_factory = llm_factory
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
        scope = cache_scope(self.session, principal)
        key = build_cache_key("rag", scope, ANSWER_CACHE_VERSION, " ".join(request.question.lower().split()),
                              request.model_dump(mode="json", exclude={"question"}))
        if (cached := self.cache.get(key)) is not None:
            response = AnswerResponse.model_validate(cached)
            response.cache_hit = True
            response.query_id = self._audit(principal, request, response, retrieved=[], cache_hit=True)
            yield "done", response
            return

        timings: dict[str, float] = {}
        deadline = started + self.settings.RAG_DEADLINE_SECONDS
        original = request
        yield "stage", {"stage": "searching"}
        request, rewrite = self._standalone(request, timings)
        plan = plan_query(
            request.question,
            ui_mode=None if request.mode == "auto" else request.mode,
            as_of=request.as_of,
            version_ids=request.version_ids,
            date_order=self.settings.DATE_ORDER,
        )
        attempt = _Attempt()
        try:
            if not rewrite.resolvable:
                raise _NoAnswer("NEEDS_CONTEXT")
            referenced = request.policy_ids or self.retriever.referenced_policies(principal, request.question)
            filters = self._filters(principal, plan, request, referenced)
            try:
                response = yield from self._attempt(principal, request, plan, filters, attempt, timings, deadline, started)
            except _NoAnswer as no_answer:
                if not self._may_fall_back(request, plan, no_answer, attempt):
                    raise
                response = yield from self._previous_versions(
                    principal, request, plan, no_answer, attempt, timings, deadline, started,
                )
        except _NoAnswer as no_answer:
            response = self._no_answer(request, plan, attempt.evidence, no_answer, attempt.llm_result)
        retrieved = attempt.retrieved

        response.question = original.question
        if rewrite.reason:
            response.plan["rewritten_question"] = request.question
            response.plan["rewrite_reason"] = rewrite.reason
            if rewrite.reason == "translation" and response.status == "answered":
                response.warnings.insert(0, f'Answered in English. The question was searched as: "{request.question}"')
        timings["total"] = round((time.perf_counter() - started) * 1000, 1)
        response.timings_ms = timings
        response.query_id = self._audit(principal, original, response, retrieved=retrieved, cache_hit=False)
        self.cache.set(key, response.model_dump(mode="json"), ttl_seconds=self.settings.RAG_CACHE_TTL_SECONDS)
        yield "done", response

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
            for part in self._generate_stream(principal, request, plan, evidence):
                if isinstance(part, LLMResult):
                    attempt.llm_result = part
                else:
                    timings.setdefault("first_claim", round((time.perf_counter() - started) * 1000, 1))
                    attempt.claims_streamed += 1
                    yield "claim", part
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
            "The version currently in force does not cover this. Answered from a previous version: "
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
            llm = None
        rewrite = standalone_question(llm, request.question, request.history)
        if rewrite.reason:
            timings["rewrite"] = round((time.perf_counter() - step) * 1000, 1)
        if rewrite.question != request.question:
            request = request.model_copy(update={"question": rewrite.question})
        return request, rewrite

    def _filters(self, principal: Principal, plan: QueryPlan, request: AskRequest, referenced: list[uuid.UUID]) -> SearchFilters:
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
        versions = [self.session.get(PolicyVersion, v) for v in plan.version_ids]
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
        results = validate_claims([raw], texts)
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
        results = validate_claims(content.get("claims", []), texts)
        _drop_metadata_echo(results, request.question, plan)
        warnings = [f"Removed an unsupported statement: {', '.join(r.problems)}" for r in results if not r.valid]
        warnings += [f"Ignored {p}" for r in results if r.valid for p in r.problems]
        valid = [r for r in results if r.valid]

        verified = self._verify_citations(principal, {e for r in valid for e in r.evidence_ids if e != "D1"}, items)
        valid = [r for r in valid if all(e == "D1" or e in verified for e in r.evidence_ids)]
        if not valid:
            if content.get("insufficient_evidence") or not results:
                raise _NoAnswer("INSUFFICIENT_EVIDENCE")
            raise _NoAnswer("ANSWER_FAILED_VALIDATION")

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
        for conflict in conflicts:
            conflict["citations"] = [numbering[e] for e in conflict["evidence_ids"] if e in numbering]

        answer = " ".join(f"{c.text} [{', '.join(map(str, c.citations))}]" for c in claims)
        if any(c["citations"] for c in conflicts):
            warnings.insert(0, "Sources disagree on part of this answer; see conflicts.")
        return AnswerResponse(
            question=request.question, status="answered", answer=answer, claims=claims, sources=sources,
            conflicts=conflicts, warnings=warnings, plan=plan.describe(), evidence_score=evidence.top_score,
            model=llm_result.model if llm_result else None,
            usage={"input_tokens": llm_result.input_tokens, "output_tokens": llm_result.output_tokens} if llm_result else {},
            timings_ms={},
        )

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
                suggestions=SUGGESTIONS,
                missing_terms=no_answer.missing_terms,
            ),
            plan=plan.describe(), evidence_score=evidence.top_score,
            model=llm_result.model if llm_result else None,
            usage={"input_tokens": llm_result.input_tokens, "output_tokens": llm_result.output_tokens} if llm_result else {},
            timings_ms={},
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
        self.session.flush()
        self.session.commit()
        return event.id


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
