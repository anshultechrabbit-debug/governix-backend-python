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

import dataclasses
from decimal import Decimal, InvalidOperation
import logging
from concurrent.futures import ThreadPoolExecutor
import re
import threading
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
from app.modules.policies.model import Policy, PolicyStatus, PolicyVersion, VersionStatus
from app.modules.rag.claim_stream import ClaimStream
from app.modules.rag.evidence import (
    EvidenceItem, EvidenceSet, build_evidence, coverage_of, is_document_question, key_terms,
)
from app.infrastructure.ai.llm.local import LocalLLM
from app.modules.rag.prompts import (
    OUTPUT_SCHEMA, SUMMARY_CHECK_PROMPT, SUMMARY_CHECK_SCHEMA, SYSTEM_PROMPT, build_user_prompt, comparison_text,
)
from app.modules.rag.query_plan import COMPARISON_WORDS, QueryClass, QueryPlan, normal_label, plan_query, version_mentions, without_version_refs
from app.modules.rag.query_rewrite import Rewrite, restated_questions, standalone_question
from app.modules.rag.schema import AnswerResponse, AskRequest, Claim, NoAnswer, Source
from app.modules.rag.validation import ABSENCE_PROBLEM, EvidenceText, TermIndex, validate_claims
from app.modules.search.model import Chunk
from app.modules.search.retrieval import (
    Candidate, HybridRetriever, NamedDocument, SearchFilters, VersionScope, names_code, requested_clauses, requested_codes,
)
from app.modules.search.policy_names import without_references
from app.modules.search.schema import Provenance
from app.modules.search.rephrase import rephraser
from app.modules.search.service import cache_scope, provenance, retrieve_with_variants
from app.modules.versions.integrity import newer_versions, unrelated_versions
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
#   v35    synonym-aware term checks, ambiguity clarification, earlier-version notes,
#          version-register corrections, computed comparisons
#   v36    the model's insufficient_evidence is honoured; identifier clauses (KAP-KEY-01) count as named;
#          policies named by alias leave the term checks; abbreviations match their expansions
ANSWER_CACHE_VERSION = "v36"
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
# "The latest version is Version 1.0", "Version 1.0 is the current version".
_LATEST_CLAIM = re.compile(
    r"\b(?:latest|newest|most\s+recent|current)\s+(?:version|edition)\b"
    r"|\b(?:version|edition)\s*[\d.]+\s+is\s+the\s+(?:latest|newest|current|most\s+recent)\b",
    re.I,
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
        item.id: EvidenceText(item.id, item.full_text, _provenance_numbers(item),
                              label=_label(item.source, named=item.source.version_id not in evidence.unrelated),
                              versions=frozenset(filter(None, [item.source.version_label])))
        for item in evidence.items
    }
    if evidence.comparison:
        compared = evidence.comparison
        texts["D1"] = EvidenceText("D1", comparison_text(compared), versions=frozenset(
            [compared["from_version"]["label"], compared["to_version"]["label"]]))
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


# "Compare v1 with the current version": the other side is the version in force, not the previous one.
_WITH_CURRENT = re.compile(r"\b(?:current|latest|newest|most\s+recent|in\s+force|today'?s?)\b", re.I)


def comparison_subject(question: str, policy_names: list[str]) -> list[str]:
    """What a comparison is about ("penal", "charge", "cap"); empty for "what changed in v2?" or
    "what is new in the home loan policy?", which ask for every change."""
    names = TermIndex(" ".join(policy_names))
    return [t for t in key_terms(question) if t not in COMPARISON_WORDS and not names.mentions(t)]


_SEVERAL = re.compile(
    r"\b(?:and|also|as well as|along with)\s+(?:what|who|whom|when|where|which|why|how)\b"
    r"|\?\s*\S.*\?",
    re.IGNORECASE,
)


# "... what is the limit? Is it 15 months?": a check on the same question, not a second one.
_CONFIRMATION = re.compile(
    r"\?\s*(?:is|isn'?t|are|was|does|do)\s+(?:it|that|this|they|the\s+\w+)\b[^?]{0,80}\?\s*$"
    r"|\?\s*(?:right|correct|true|isn'?t\s+it|is\s+that\s+(?:right|correct))\s*\?\s*$",
    re.IGNORECASE,
)


def asks_several(question: str) -> bool:
    """ "What is X and who issued it?", or two questions in one message."""
    if question.count("?") == 2 and _CONFIRMATION.search(question):
        return False
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


# Parts of a several-question message are answered at once, a few at a time.
PART_WORKERS = 5
_PART_POOL = ThreadPoolExecutor(max_workers=PART_WORKERS, thread_name_prefix="rag-part")

# Version diffs computed for questions (a stored one predates sentence-level changes, or the two
# versions are not consecutive). A 14,000-section diff takes about two seconds; the documents of a
# version do not change while it exists, so a diff is kept for a while, keyed by both documents.
_DIFF_CACHE: dict[tuple, tuple[float, dict]] = {}
_DIFF_CACHE_MAX = 64
_DIFF_TTL_SECONDS = 3600
_DIFF_LOCK = threading.Lock()
# Changes about one subject given to the model: enough for every version of a long-lived policy.
MAX_SUBJECT_CHANGES = 12


def _change_chain(versions: list[tuple[PolicyVersion, str]], named: list[uuid.UUID],
                  referenced: list[uuid.UUID]) -> list[uuid.UUID]:
    """One policy's versions to diff for a subject comparison, oldest first: from the earliest to the
    latest version the question names (and every version between, so "first changed" is exact), or
    all versions of the one policy the question names when it names no version. Empty when the
    question does not single out one policy."""
    by_policy: dict[uuid.UUID, list[PolicyVersion]] = {}
    for version, _ in versions:
        by_policy.setdefault(version.policy_id, []).append(version)
    if named:
        chosen = [v for v, _ in versions if v.id in set(named)]
        policies = {v.policy_id for v in chosen}
        if len(policies) != 1 or len(chosen) < 2:
            return []
        start, end = min(v.effective_from for v in chosen), max(v.effective_from for v in chosen)
        return [v.id for v in by_policy[policies.pop()] if start <= v.effective_from <= end]
    targets = [p for p in dict.fromkeys(referenced) if len(by_policy.get(p, [])) >= 2]
    return [v.id for v in by_policy[targets[0]]] if len(targets) == 1 else []


def _result_of(run: Generator[tuple[str, Any], None, Any]) -> Any:
    """Run a pipeline generator to the end, discarding its events; return its result."""
    while True:
        try:
            next(run)
        except StopIteration as stop:
            return stop.value


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


def _with_versions(question: str, parts: list[str] | None) -> list[str] | None:
    """Each part of a split question, still about the versions the whole question names.

    "Under Version 1.0, what does clause 1.1.6 say and how long is ... retained under 1.2.13?"
    must not become a question about clause 1.2.13 of whatever version is in force."""
    named = version_mentions(question)
    if not parts or not named:
        return parts
    return [p if version_mentions(p) else f"In {' and '.join(named)}: {p}" for p in parts]


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
    def __init__(self, reason: str, missing_terms: list[str] | None = None, suggestions: list[str] | None = None,
                 message: str | None = None) -> None:
        self.reason = reason
        self.missing_terms = missing_terms or []
        self.suggestions = suggestions
        self.message = message


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
        self._rephrase = rephraser(llm_factory)
        self._session_factory = session_factory
        self.retriever = HybridRetriever(session_factory, embedder)
        # Each question as asked -> what it asks about, without the policies it names
        # ("in the KYC/AML Policy"): see _subject.
        self._subjects: dict[str, str] = {}

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
        restated = _with_versions(prepared_request.question, restated)
        split_first = bool(restated and len(restated) > 1)
        if split_first:
            response, retrieved = self._no_answer(prepared_request, plan, EvidenceSet([], 0.0, [], 0.0, []),
                                                  _NoAnswer("KEY_TERMS_NOT_FOUND"), None), []
        else:
            restated = None
            response, retrieved = yield from self._run_pipeline(
                principal, prepared_request, plan, rewrite, named, timings, deadline, started
            )
        if restated or (self._worth_clarifying(response, deadline)
                        and (restated := _with_versions(prepared_request.question,
                                                        self._clarify(prepared_request.question, timings)))):
            # The words as typed found nothing ("pokucy", "hello ... in short", or two subjects
            # in one question): search again for the question(s) as the person meant them.
            # Nothing was shown yet.
            parts = []
            if len(restated) == 1:
                question = restated[0]
                part_request, _, part_named, part_plan = self._prepare_request(
                    principal, prepared_request.model_copy(update={"question": question, "history": []}), timings,
                )
                yield "stage", {"stage": "searching"}
                part, part_retrieved = yield from self._run_pipeline(
                    principal, part_request, part_plan, Rewrite(question), part_named, timings, deadline, started,
                )
                retrieved += part_retrieved
                parts.append((question, part_request, part_plan, part))
            else:
                # Several questions: each is answered on its own, in parallel, with its own session
                # and time limit; their claims are shown once combined (numbering differs per part).
                yield "stage", {"stage": "searching", "parts": len(restated)}
                futures = [_PART_POOL.submit(self._answer_part, principal, prepared_request, q) for q in restated]
                for question, future in zip(restated, futures, strict=True):
                    part_request, part_plan, part, part_retrieved = future.result()
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

    def _answer_part(self, principal: Principal, request: AskRequest, question: str):
        """One part of a several-question message, answered in a worker thread with its own session."""
        with self._session_factory() as session:
            service = RAGService(session, self._session_factory, self.settings, self.cache,
                                 self.embedder, self.reranker, self._llm_factory)
            timings: dict[str, float] = {}
            part_request, _, named, plan = service._prepare_request(
                principal, request.model_copy(update={"question": question, "history": []}), timings,
            )
            started = time.perf_counter()
            response, retrieved = _result_of(service._run_pipeline(
                principal, part_request, plan, Rewrite(question), named, timings,
                started + self.settings.RAG_DEADLINE_SECONDS, started,
            ))
            session.rollback()
        return part_request, plan, response, retrieved

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
            self._subjects[request.question] = without_references(
                request.question, self.retriever.policy_mentions(principal, request.question))
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
        if response.status == "answered":
            self._registry_corrections(response)
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
            limit=self.settings.RAG_RETRIEVAL_CANDIDATES, rephrase=self._rephrase,
        )
        timings.update({f"{prefix}retrieval_{k}": v for k, v in result.timings_ms.items()})
        attempt.retrieved += [c.chunk_id for c in result.candidates]

        step = time.perf_counter()
        attempt.evidence = evidence = build_evidence(
            self.session, principal, request.question, result.candidates,
            reranker=self.reranker, retriever=self.retriever, filters=filters,
            rerank_top_n=self.settings.RAG_RERANK_TOP_N, limit=self.settings.RAG_EVIDENCE_LIMIT,
        )
        if plan.diff:
            evidence.comparison = self._comparison(principal, plan)
        elif plan.diff_versions:
            evidence.comparison = self._subject_changes(principal, plan, request.question)
        evidence.unrelated = unrelated_versions(self.session, [i.source.version_id for i in evidence.items])
        self._true_periods(evidence)
        timings[f"{prefix}evidence"] = round((time.perf_counter() - step) * 1000, 1)

        self._gate(evidence, request.question, principal)
        if plan.query_class in (QueryClass.CURRENT, QueryClass.HISTORICAL, QueryClass.SPECIFIC_VERSION):
            _refuse_if_ambiguous(evidence, self._subject(request.question))
        yield "stage", {"stage": "reading", "passages": len(evidence.items)}

        step = time.perf_counter()
        response = self._acronym_answer(principal, request, plan, evidence, filters)
        if response is None:
            self._check_deadline(deadline, evidence)
            yield "stage", {"stage": "writing"}
            try:
                response = yield from self._write(principal, request, plan, evidence, attempt, timings, started)
            except _NoAnswer as no_answer:
                # True statements about something else ("pricing proposals above Rs. 104 lakh"
                # for "the Pricing threshold") while a passage names everything asked: once more,
                # from those passages only. Nothing was shown: off-topic claims are held back.
                focused = _focused(evidence, self._subject(request.question))
                if no_answer.reason != "ANSWER_OFF_TOPIC" or attempt.claims_streamed or focused is None:
                    raise
                self._check_deadline(deadline, focused)
                attempt.evidence = focused
                response = yield from self._write(principal, request, plan, focused, attempt, timings, started)
        timings[f"{prefix}llm"] = round((time.perf_counter() - step) * 1000, 1)
        if attempt.llm_result is not None:
            timings[f"{prefix}validation"] = round((time.perf_counter() - step) * 1000, 1)
        return response

    def _write(
        self, principal, request, plan: QueryPlan, evidence: EvidenceSet, attempt: _Attempt,
        timings: dict[str, float], started: float,
    ) -> Generator[tuple[str, Any], None, AnswerResponse]:
        """Generate the answer from `evidence`, streaming each claim once the answer is on topic."""
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
            unrelated = {str(v) for v in evidence.unrelated}
            for source in part.get("sources", []):
                named = str(source.get("version_id")) not in unrelated
                # The policy a claim cites counts as said ("the processing fee on a personal loan" is
                # answered from the personal loan policy); its section heading does not: a heading on the
                # subject does not make a sentence beneath it about the subject.
                names.append(" ".join(filter(None, [
                    source.get("policy_name") if named else None, source.get("document_title") if named else None,
                ])))
                passages.append(source.get("excerpt") or "")
            if not on_topic and self._off_topic(principal, request.question, [c["text"] for c in held] + names, passages) is not None:
                continue
            on_topic = True
            timings.setdefault("first_claim", round((time.perf_counter() - started) * 1000, 1))
            for claim in held:
                attempt.claims_streamed += 1
                yield "claim", claim
            held = []
        return self._validated_answer(principal, request, plan, evidence, attempt.llm_result)

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
            # A version whose document never finished indexing has nothing to search.
            .join(Document, Document.id == PolicyVersion.document_id)
            .where(
                Document.status == DocumentStatus.READY,
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
        statement = select(ranked.c.id, ranked.c.depth)
        if self.settings.RAG_FALLBACK_MAX_DEPTH is not None:
            statement = statement.where(ranked.c.depth <= self.settings.RAG_FALLBACK_MAX_DEPTH)
        rows = self.session.execute(statement).all()
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
        if plan.query_class is QueryClass.COMPARISON:
            self._plan_comparison(principal, plan, request.question, referenced)
        if plan.mode == "versions":
            if not plan.version_ids:
                plan.version_ids = self._resolve_labels(principal, plan, referenced)
            visible = self._visible_versions(principal, plan.version_ids)
            if not visible:
                raise _NoAnswer("VERSION_NOT_FOUND")
            plan.version_ids = visible
            scope = VersionScope("versions", version_ids=visible)
        elif plan.mode == "all":
            scope = VersionScope("all")
        else:
            scope = VersionScope("as_of", as_of=plan.as_of)
        return SearchFilters(
            version_scope=scope,
            policy_ids=list(request.policy_ids),
            category_ids=list(request.category_ids),
            document_ids=[d.document_id for d in named],
        )

    def _resolve_labels(self, principal: Principal, plan: QueryPlan, referenced: list[uuid.UUID]) -> list[uuid.UUID]:
        """The versions the question names ("v2", "Version 1.0", "edition 3")."""
        wanted = {normal_label(label) for label in plan.version_labels}
        return [v.id for v, _ in self._searchable_versions(principal, referenced) if normal_label(v.version_label) in wanted]

    def _searchable_versions(self, principal: Principal, policy_ids: list[uuid.UUID]) -> list[tuple[PolicyVersion, str]]:
        """Versions the caller can see whose document is searchable, with their policy's name, oldest first."""
        query = (
            select(PolicyVersion, Policy.name)
            .join(Policy, Policy.id == PolicyVersion.policy_id)
            .join(Document, Document.id == PolicyVersion.document_id)
            .where(
                PolicyVersion.status == VersionStatus.ACTIVE,
                Policy.status == PolicyStatus.ACTIVE,
                Document.status == DocumentStatus.READY,
                visible_clause(principal, Policy.organization_id, Policy.branch_id, Policy.department_id, Policy.id),
            )
            .order_by(PolicyVersion.effective_from)
        )
        if policy_ids:
            query = query.where(PolicyVersion.policy_id.in_(policy_ids))
        return [(version, name) for version, name in self.session.execute(query).all()]

    def _plan_comparison(self, principal: Principal, plan: QueryPlan, question: str, referenced: list[uuid.UUID]) -> None:
        """Which versions a comparison reads, and whether it is answered from the section diff.

        A comparison of something specific ("the penal charge cap in v1 and v2", "how has the
        cap changed?") is answered from the passages about it in each version, every version
        when none is named. One with no subject ("what changed in v2?") is answered from the
        section diff of two versions of one policy.
        """
        versions = self._searchable_versions(principal, [])
        today = datetime.now(UTC).date()
        if plan.version_labels and not plan.version_ids:
            wanted = {normal_label(label) for label in plan.version_labels}
            if referenced:
                in_referenced = [(v, n) for v, n in versions if v.policy_id in referenced]
                if any(normal_label(v.version_label) in wanted for v, _ in in_referenced):
                    versions = in_referenced  # "v2 of the home loan policy": not v2 of every policy
            named = [v for v, _ in versions if normal_label(v.version_label) in wanted]
            if len(plan.version_labels) == 1:
                # "What changed in version 2.0?": against the version before it (or the one in
                # force, when the question says so).
                named += [other for v in named if (other := self._counterpart(v, versions, question, today))]
            plan.version_ids = list(dict.fromkeys(v.id for v in named))
            if not plan.version_ids:
                raise _NoAnswer("VERSION_NOT_FOUND")
        chosen = [(v, n) for v, n in versions if v.id in set(plan.version_ids)]
        names = [n for _, n in chosen] or [n for v, n in versions if v.policy_id in referenced]
        if comparison_subject(question, names):
            if not plan.version_ids:
                plan.mode, plan.explanation = "all", "Question compares versions; each passage is labelled with its version"
            plan.diff_versions = _change_chain(versions, plan.version_ids, referenced)
            return
        # Every change between two versions of one policy.
        if not plan.version_ids:
            by_policy: dict[uuid.UUID, list[PolicyVersion]] = {}
            for version, _ in versions:
                by_policy.setdefault(version.policy_id, []).append(version)
            targets = [p for p, vs in by_policy.items() if len(vs) >= 2 and (not referenced or p in referenced)]
            if len(targets) != 1:
                raise _NoAnswer("COMPARISON_TARGET_UNCLEAR")
            policy_versions = by_policy[targets[0]]
            newest = ([v for v in policy_versions if _in_force(v, today)] or policy_versions)[-1]
            pair = [v for v in policy_versions if v.effective_from < newest.effective_from][-1:] + [newest]
            chosen = [(v, "") for v in (pair if len(pair) == 2 else policy_versions[-2:])]
        by_policy = {}
        for version, _ in chosen:
            by_policy.setdefault(version.policy_id, []).append(version)
        pairs = [vs for vs in by_policy.values() if len(vs) >= 2]
        if len(pairs) != 1:
            raise _NoAnswer("COMPARISON_TARGET_UNCLEAR")
        plan.version_ids, plan.diff = [pairs[0][0].id, pairs[0][-1].id], True

    @staticmethod
    def _counterpart(version: PolicyVersion, versions: list[tuple[PolicyVersion, str]], question: str, today: date):
        others = [v for v, _ in versions if v.policy_id == version.policy_id and v.id != version.id]
        if not others:
            return None
        if _WITH_CURRENT.search(question):
            return ([v for v in others if _in_force(v, today)] or others)[-1]
        earlier = [v for v in others if v.effective_from < version.effective_from]
        return earlier[-1] if earlier else others[0]

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
        return self._version_diff(older, newer)

    def _version_diff(self, older: PolicyVersion, newer: PolicyVersion) -> dict:
        """The deterministic diff of two versions of one policy, with the sentences that changed."""
        stored = newer.change_summary if newer.supersedes_version_id == older.id else None
        if stored and all("changes" in item for item in stored.get("modified", [])):
            # Stored when the version was published: the same diff, without seconds of recomputing.
            return stored
        key = (older.id, newer.id, older.document_id, newer.document_id)
        with _DIFF_LOCK:
            if (cached := _DIFF_CACHE.get(key)) and cached[0] > time.monotonic():
                return cached[1]
        diff = compare_versions(self.session, older, newer, include_content=False)
        with _DIFF_LOCK:
            if len(_DIFF_CACHE) >= _DIFF_CACHE_MAX:
                _DIFF_CACHE.pop(next(iter(_DIFF_CACHE)))
            _DIFF_CACHE[key] = (time.monotonic() + _DIFF_TTL_SECONDS, diff)
        return diff

    def _subject_changes(self, principal: Principal, plan: QueryPlan, question: str) -> dict | None:
        """What changed about the question's subject across a policy's versions, from the version diffs.

        "How did the STR filing deadline change from v1.0 to v3.0?", "Which version first changed the
        LTV?", "In which version was the top-up clause removed?": the consecutive diffs of the versions
        hold every changed sentence; those that carry most of the subject's distinguishing weight are
        the answer's evidence. Unlike passage search, this cannot miss a version or a removed clause.
        None when no changed sentence is about the subject (it may simply not have changed)."""
        versions = sorted(self.session.scalars(
            select(PolicyVersion).where(PolicyVersion.id.in_(plan.diff_versions))
        ).all(), key=lambda v: v.effective_from)
        if len(versions) < 2:
            return None
        policy_name = self.session.scalar(select(Policy.name).where(Policy.id == versions[0].policy_id)) or ""
        terms = [t for t in comparison_subject(self._subject(question), [policy_name]) if not t.isdigit()]
        weights = self.retriever.visible_term_weights(principal, terms)
        total = sum(weights.values())
        if total <= 0:
            return None
        matched = []
        for older, newer in zip(versions, versions[1:]):
            diff = self._version_diff(older, newer)
            for item in diff.get("modified", []):
                for change in item.get("changes", []):
                    said = TermIndex(" ".join(filter(None, [change["old"], change["new"]])))
                    if sum(w for t, w in weights.items() if said.mentions(t)) / total >= self.settings.RAG_MIN_SALIENT_COVERAGE:
                        matched.append({
                            "versions": f"From Version {older.version_label} to Version {newer.version_label}",
                            "new": item["new"], "old": item["old"], "changes": [change],
                            "numeric_changes": {"changed": []},
                        })
        if not matched:
            return None
        first, last = versions[0], versions[-1]
        return {
            "from_version": {"id": str(first.id), "label": first.version_label, "effective_from": first.effective_from.isoformat()},
            "to_version": {"id": str(last.id), "label": last.version_label, "effective_from": last.effective_from.isoformat()},
            "summary_lines": [
                "Every version in between was compared with the next; only the changes about the question's subject "
                "are listed. A version not listed did not change it.",
            ],
            "modified": matched[:MAX_SUBJECT_CHANGES],
            "added": [], "removed": [],
        }

    # --- gate, generation, validation ------------------------------------------------

    def _subject(self, question: str) -> str:
        """What the question asks about: without the policies it names explicitly ("in the KYC/AML
        Policy", "of the Home Loan Policy v2"). Those words chose the document, as a version label
        chooses the version; the passage need not repeat them. A policy named in a way it cannot be
        resolved to ("the gold loan policy") stays in, so the term checks still catch it."""
        return self._subjects.get(question, question)

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
        # "What does clause 1.1.6 say about the amount that needs concurrence?": the clause the
        # question names is its topic, as for the on-topic check; its words need not be repeated.
        clauses = requested_clauses(question)
        if clauses and any(_names_clause(item.candidate.text, c) for item in evidence.items for c in clauses):
            return
        # The same for a clause named by its identifier ("What does KAP-KEY-01 say?").
        codes = requested_codes(question)
        if codes and any(names_code(item.candidate.text, c) for item in evidence.items for c in codes):
            return
        if (subject := self._subject(question)) != question:
            evidence.coverage, evidence.missing_terms = coverage_of(
                subject, [i.full_text + " " + i.source.section_path for i in evidence.items])
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
        weights = self.retriever.visible_term_weights(principal, key_terms(self._subject(question)))
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
        # "Section 4.47.7": the clause the question names is its topic, whatever its words.
        # One cited passage stating the clause is enough: every claim is still checked against
        # its own citation, and a second, related passage must not make the clause "missing".
        clauses = requested_clauses(question)
        if clauses and cited and any(_names_clause(passage, c) for passage in cited for c in clauses):
            return None
        codes = requested_codes(question)
        if codes and cited and any(names_code(passage, c) for passage in cited for c in codes):
            return None
        everything = TermIndex(" ".join(said + cited))
        subject = self._subject(question)
        absent = [name for name in named_entities(subject) if not everything.mentions(name)]
        if absent:
            return absent
        # A date in the question chose the version; the answer need not repeat it.
        terms = [t for t in key_terms(subject) if not t.isdigit() and t not in _MONTHS]
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
                    if claims.declared_insufficient:
                        continue  # the answer will be a no-answer: show nothing
                    if (claim := self._streamed_claim(principal, request, plan, evidence, raw, texts, items, numbering)):
                        yield claim
        except LLMUnavailableError as exc:
            logger.error("LLM unavailable: %s", exc)
            raise _NoAnswer("LLM_UNAVAILABLE") from None

    def _streamed_claim(self, principal, request, plan, evidence, raw, texts, items, numbering) -> dict | None:
        """One claim checked exactly as the final answer checks it, or None if it fails."""
        results = validate_claims([raw], texts, key_terms(self._subject(request.question)), request.question)
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
        if content.get("insufficient_evidence") is True:
            # The model read the evidence and found no answer in it. Claims it wrote anyway are about
            # something nearby (the home loan LTV for a gold loan question): true, but not an answer.
            raise _NoAnswer("INSUFFICIENT_EVIDENCE")
        results = validate_claims(content.get("claims", []), texts, key_terms(self._subject(request.question)),
                                  request.question)
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
            named = [" ".join(filter(None, [items[e].source.policy_name, items[e].source.document_title]))
                     for e in cited if e in items and items[e].source.version_id not in evidence.unrelated]
            self._check_on_topic(principal, request.question, [r.text for r in valid] + named, [texts[e].text for e in cited])

        numbering: dict[str, int] = {}
        for result in valid:
            for evidence_id in result.evidence_ids:
                numbering.setdefault(evidence_id, len(numbering) + 1)
        claims = [Claim(text=r.text, citations=[numbering[e] for e in r.evidence_ids]) for r in valid]
        claims = _without_repeats(claims)
        if computed := _magnitude_comparison(request.question, valid, items, numbering):
            claims.append(computed)
            checks.append("Computed the difference from the two cited figures")
        sources = [self._source(n, e, items, evidence) for e, n in numbering.items()]
        if request.mode == "auto" and plan.query_class is QueryClass.CURRENT and evidence.comparison is None:
            for claim, source in self._earlier_version_notes(principal, request.question, valid, items, len(sources)):
                claims.append(claim)
                sources.append(source)
            if len(sources) > len(numbering):
                warnings.append("This rule was different in an earlier version; both are shown.")

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
        summary = self._summary(request.question, content.get("summary"), valid, texts, claims,
                                subject=self._subject(request.question))
        warnings += self._version_warnings(request.question, plan, sources, evidence)
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

    # --- version integrity -----------------------------------------------------------

    def _true_periods(self, evidence: EvidenceSet) -> None:
        """End each passage's period where a newer registered version began.

        A version stays "in force to present" in the database when the version after it was
        registered but its document failed to process; the header would then tell the model
        (and the reader) that a replaced version is current."""
        found: dict[tuple, list] = {}
        for item in evidence.items:
            source = item.source
            if not source.policy_id or not source.effective_from:
                continue
            key = (source.policy_id, source.effective_from)
            if key not in found:
                found[key] = newer_versions(self.session, source.policy_id, source.effective_from)
            later = found[key]
            if later and (source.effective_to is None or later[0].effective_from < source.effective_to):
                source.effective_to = later[0].effective_from

    def _version_warnings(self, question: str, plan: QueryPlan, sources: list[Source], evidence: EvidenceSet) -> list[str]:
        """What the reader must know about the versions an answer cites."""
        warnings: list[str] = []
        today = datetime.now(UTC).date()
        seen: set = set()
        for source in sources:
            if source.version_id in evidence.unrelated and source.version_id not in seen:
                seen.add(source.version_id)
                odd = evidence.unrelated[source.version_id]
                warnings.append(
                    f"Version {odd.version_label} of '{source.policy_name}' does not appear to be a version of this "
                    f"policy: its text is unrelated to Version {odd.previous_label} (it is titled "
                    f"\u201c{odd.heading}\u201d). Check that version record before relying on it."
                )
        if plan.query_class is QueryClass.COMPARISON and plan.version_ids and evidence.comparison is None:
            # A side of the comparison with nothing to cite is named, never filled in.
            labels = dict(self.session.execute(
                select(PolicyVersion.id, PolicyVersion.version_label).where(PolicyVersion.id.in_(plan.version_ids))
            ).all())
            cited_versions = {s.version_id for s in sources}
            uncovered = [labels[v] for v in plan.version_ids if v in labels and v not in cited_versions]
            if uncovered and len(uncovered) < len(plan.version_ids):
                warnings.append("Nothing in " + " or ".join(f"Version {label}" for label in uncovered)
                                + " addresses this, so it could not be compared.")
            for odd in unrelated_versions(self.session, plan.version_ids).values():
                if odd.version_id not in seen:
                    seen.add(odd.version_id)
                    warnings.append(
                        f"Version {odd.version_label} does not appear to be a version of this policy: its text is "
                        f"unrelated to Version {odd.previous_label} (it is titled \u201c{odd.heading}\u201d)."
                    )
        if plan.query_class is not QueryClass.CURRENT:
            return warnings
        cited = {}
        for source in sources:
            if source.policy_id and source.effective_from and not source.previous_version:
                cited.setdefault(source.policy_id, source)
        for policy_id, source in cited.items():
            later = newer_versions(self.session, policy_id, source.effective_from)
            stuck = [v for v in later if v.effective_from <= today and not v.searchable
                     and (v.effective_to is None or v.effective_to > today)]
            if stuck:
                version = stuck[-1]
                warnings.append(
                    f"Version {version.label} of '{source.policy_name}' has been in force since "
                    f"{version.effective_from.isoformat()}, but its document could not be processed "
                    f"({version.document_status}), so this answer comes from Version {source.version_label} "
                    f"and may be out of date. Re-process or re-upload Version {version.label}."
                )
            upcoming = [v for v in later if v.effective_from > today]
            if upcoming and _VERSION_QUESTION.search(question):
                warnings.append("Registered but not yet in force: " + "; ".join(
                    f"Version {v.label} from {v.effective_from.isoformat()}"
                    + ("" if v.searchable else f" (document {v.document_status})") for v in upcoming) + ".")
        return warnings

    def _registry_corrections(self, response: AnswerResponse) -> None:
        """Make a finished answer agree with the version register.

        * An answer taken from an earlier version because the version recorded as in force is
          another document says so.
        * A claim that some version is "the latest" or "the current" one is withdrawn when the
          register holds a newer version: the model only sees the versions it can search, and
          a version whose document failed or is misfiled is invisible to it.
        """
        newer: dict = {}
        for source in response.sources:
            if source.policy_id and source.effective_from and (source.policy_id, source.effective_from) not in newer:
                newer[(source.policy_id, source.effective_from)] = (
                    source, newer_versions(self.session, source.policy_id, source.effective_from))
        for source, later in newer.values():
            if not source.previous_version:
                continue
            for odd in unrelated_versions(self.session, [v.id for v in later]).values():
                note = (f"The version recorded as in force, Version {odd.version_label}, does not appear to be a "
                        f"version of '{source.policy_name}' (it is titled \u201c{odd.heading}\u201d), so it was "
                        "not used. Check that version record.")
                if note not in response.warnings:
                    response.warnings.append(note)
        newest: dict = {}
        for source in response.sources:
            if source.policy_id and source.effective_from:
                if source.policy_id not in newest or source.effective_from > newest[source.policy_id]:
                    newest[source.policy_id] = source.effective_from
        if not any(newer_versions(self.session, policy_id, start) for policy_id, start in newest.items()):
            return
        kept = [c for c in response.claims if not _LATEST_CLAIM.search(c.text)]
        if kept and len(kept) < len(response.claims):
            response.claims = kept
            response.answer = " ".join(f"{c.text} [{', '.join(map(str, c.citations))}]" for c in kept)
            response.summary = None  # it may repeat the withdrawn claim
            response.warnings.append("A statement about which version is the latest was removed: the register "
                                     "lists a newer version than the documents that could be searched.")

    # --- what the previous version said -----------------------------------------------

    MAX_VERSION_NOTES = 2

    def _earlier_version_notes(self, principal, question: str, valid, items, numbered: int) -> list[tuple[Claim, Source]]:
        """The same rule as worded in the version before the one in force, where it differs.

        A question about the current rule is answered from the version in force; silently leaving
        out that the previous version said something else (another figure, another approver) hides
        a change the reader may rely on. Matched deterministically: the same clause number, or for
        an unnumbered rule (a definition) a rule on the same page that names every term of the
        question. Nothing is shown when the wording is unchanged.
        """
        notes: list[tuple[Claim, Source]] = []
        seen: set[str] = set()
        today = datetime.now(UTC).date()
        terms = [t for t in key_terms(self._subject(question)) if not t.isdigit()]
        for result in valid:
            if len(notes) >= self.MAX_VERSION_NOTES:
                break
            item = next((items[e] for e in result.evidence_ids if e in items), None)
            if item is None or not item.source.version_id or not item.source.policy_id:
                continue
            if item.source.effective_to is not None and item.source.effective_to <= today:
                continue  # already an earlier version (a fallback answer)
            current = _best_rule(item.candidate.text, result.text)
            if not current:
                continue
            clause = m.group(1) if (m := _CLAUSE_START.match(current)) else None
            previous = self._previous_version(principal, item.source)
            if previous is None:
                continue
            found = self._rule_in_version(principal, previous, clause, item.source.page_start, current, terms)
            if found is None:
                continue
            rule, candidate = found
            if _rule_key(rule) == _rule_key(current) or rule in seen:
                continue
            seen.add(rule)
            source = provenance(self.session, [candidate]).get(candidate.chunk_id)
            if source is None:
                continue
            until = source.effective_to.isoformat() if source.effective_to else "present"
            number = numbered + len(notes) + 1
            claim = Claim(text=(f"Version {source.version_label} (in force {source.effective_from} to {until}) "
                                f"said instead: \u201c{rule}\u201d"), citations=[number])
            notes.append((claim, Source(
                number=number, evidence_id=f"PREV{len(notes) + 1}", chunk_id=candidate.chunk_id,
                document_id=source.document_id, document_title=source.document_title, policy_id=source.policy_id,
                policy_name=source.policy_name, version_id=source.version_id, version_label=source.version_label,
                effective_from=source.effective_from, effective_to=source.effective_to,
                section_number=source.section_number, section_path=source.section_path,
                page_start=source.page_start, page_end=source.page_end, previous_version=True, excerpt=candidate.text,
            )))
        return notes

    def _previous_version(self, principal: Principal, source) -> PolicyVersion | None:
        """The searchable version of the same policy that came just before `source`'s version."""
        if source.effective_from is None:
            return None
        return self.session.scalars(
            select(PolicyVersion)
            .join(Policy, Policy.id == PolicyVersion.policy_id)
            .join(Document, Document.id == PolicyVersion.document_id)
            .where(
                PolicyVersion.policy_id == source.policy_id,
                PolicyVersion.id != source.version_id,
                PolicyVersion.status == VersionStatus.ACTIVE,
                PolicyVersion.effective_from < source.effective_from,
                Document.status == DocumentStatus.READY,
                visible_clause(principal, Policy.organization_id, Policy.branch_id, Policy.department_id, Policy.id),
            )
            .order_by(PolicyVersion.effective_from.desc())
            .limit(1)
        ).first()

    def _rule_in_version(self, principal, version: PolicyVersion, clause: str | None, page: int, current: str,
                         terms: list[str]) -> tuple[str, Candidate] | None:
        """The rule in `version` that corresponds to `current`: same clause number, or (unnumbered)
        on the same page and naming every term of the question."""
        query = select(Chunk).where(
            Chunk.version_id == version.id,
            visible_clause(principal, Chunk.organization_id, Chunk.branch_id, Chunk.department_id, Chunk.policy_id),
        )
        if clause:
            query = query.where(Chunk.text.op("~")(rf"(^|[^0-9.]){re.escape(clause)}([^0-9]|$)"))
        else:
            query = query.where(Chunk.page_start <= page, Chunk.page_end >= page)
        for chunk in self.session.scalars(query.order_by(Chunk.chunk_index).limit(8)):
            for rule in _RULE_BREAK.split(chunk.text):
                rule = " ".join(rule.split())
                if clause:
                    if not ((m := _CLAUSE_START.match(rule)) and m.group(1) == clause):
                        continue
                else:
                    index = TermIndex(rule)
                    if not terms or not all(index.mentions(t) for t in terms) or _overlap(rule, current) < 0.5:
                        continue
                return rule, Candidate(
                    chunk_id=chunk.id, document_id=chunk.document_id, policy_id=chunk.policy_id,
                    version_id=chunk.version_id, section_id=chunk.section_id, chunk_index=chunk.chunk_index,
                    text=chunk.text, section_path=chunk.section_path, section_number=chunk.section_number,
                    page_start=chunk.page_start, page_end=chunk.page_end,
                )
        return None

    def _summary(self, question: str, text, valid: list, texts: dict[str, EvidenceText], claims: list[Claim],
                 subject: str | None = None) -> str | None:
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
        [result] = validate_claims([{"text": text, "evidence_ids": cited}], texts, key_terms(subject or question), question)
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
                message=no_answer.message or messages.get(no_answer.reason, NO_ANSWER_MESSAGE),
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


def _names_clause(text: str, clause: str) -> bool:
    """The text states clause "4.25.9" itself (not "4.25.19" or "14.25.9")."""
    return re.search(rf"(?:^|[^0-9.]){re.escape(clause)}(?:[^0-9]|$)", text) is not None


_CLAIM_WORD = re.compile(r"[a-z][a-z0-9]{2,}")


def _claim_words(text: str) -> set[str]:
    return {w for w in _CLAIM_WORD.findall(without_version_refs(text).lower()) if w not in _RESTATEMENT_FILLER}


def _claim_figures(text: str) -> set[tuple[str, str]]:
    return {f.key for f in extract_numeric_facts(without_version_refs(text))}


def _without_repeats(claims: list[Claim]) -> list[Claim]:
    """Claims without one that only repeats an earlier claim: the same figures, and no word the
    earlier claim does not already say. Rows that differ by a single word ("Loans against Shares
    have a 50% margin", "Loans for IPOs have a 50% margin") are different facts and both stay.
    The kept claim takes the dropped one's citations, so no source is lost."""
    kept: list[Claim] = []
    for claim in claims:
        words, figures = _claim_words(claim.text), _claim_figures(claim.text)
        for earlier in kept:
            if figures == _claim_figures(earlier.text) and words <= _claim_words(earlier.text):
                earlier.citations = list(dict.fromkeys(earlier.citations + claim.citations))
                break
        else:
            kept.append(claim)
    return kept


# "Which version has the higher threshold, and by how much?", "how much did the cap increase?"
_MAGNITUDE_QUESTION = re.compile(
    r"\b(?:higher|lower|larger|smaller|greater|bigger|stricter|longer|shorter|more|less|fewer|"
    r"by\s+how\s+much|how\s+much|difference|increase[sd]?|decrease[sd]?|rais(?:e|ed)|reduc(?:e|ed))\b",
    re.I,
)
_UNITS = {"percent": "percentage points"}


def _magnitude_comparison(question: str, valid, items: dict, numbering: dict[str, int]) -> Claim | None:
    """The arithmetic a comparison question asks for, computed from two cited figures.

    The model may not state a number its evidence does not contain, so "by how much" would
    otherwise go unanswered. When exactly two verified claims each state one figure of the same
    kind from a different version, the difference is computed here, exactly, and cites both.
    """
    if not _MAGNITUDE_QUESTION.search(question):
        return None
    stated = []
    for result in valid:
        versions = {(items[e].source.version_label, items[e].source.effective_from) for e in result.evidence_ids if e in items}
        facts = [f for f in extract_numeric_facts(without_version_refs(result.text)) if f.kind != "date"]
        if len(versions) == 1 and len(facts) == 1:
            stated.append((next(iter(versions)), facts[0], result))
    if len(stated) != 2 or stated[0][0][0] == stated[1][0][0]:
        return None
    (older, a, ra), (newer, b, rb) = sorted(stated, key=lambda s: s[0][1] or date.min)
    unit_a, unit_b = a.value.partition(" ")[2], b.value.partition(" ")[2]
    if a.kind != b.kind or unit_a != unit_b:
        return None
    try:
        first, second = Decimal(a.value.partition(" ")[0]), Decimal(b.value.partition(" ")[0])
    except InvalidOperation:
        return None
    if first == second:
        text = f"Version {older[0]} and Version {newer[0]} state the same figure ({a.raw})."
    else:
        difference = abs(second - first)
        if a.kind == "amount":
            amount = f"Rs. {difference:,.0f}" if difference == difference.to_integral() else f"Rs. {difference:,}"
        else:
            amount = f"{difference:f} {_UNITS.get(a.kind, unit_a)}".strip()
        higher = older if first > second else newer
        direction = "increased" if second > first else "decreased"
        text = (f"Version {higher[0]} has the higher figure: from Version {older[0]} ({a.raw}) to "
                f"Version {newer[0]} ({b.raw}) it {direction} by {amount}.")
    citations = list(dict.fromkeys(numbering[e] for r in (ra, rb) for e in r.evidence_ids if e in numbering))
    return Claim(text=text, citations=citations)


# A question that asks for every match ("which products have a 50% margin?", "list all ...")
# wants them all; one that asks for "the" value of something wants one.
_WANTS_ALL = re.compile(
    r"\b(?:list|all|each|every|any|which\s+\w+s|what\s+are|cover|covers|about|overview|summary|summari[sz]e|"
    r"purpose|scope|latest|current\s+version|versions?)\b", re.I)
_CLAUSE_START = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,3}){1,3})\b")
_RULE_BREAK = re.compile(r"\n+|(?<=[.;])\s+(?=\d{1,3}(?:\.\d{1,3}){1,3}\s)")
# This many different rules that each fit the whole question, setting different figures,
# make "the" value a guess.
AMBIGUOUS_RULES = 3
MAX_CHOICES = 6


def _rules_matching(evidence: EvidenceSet, question: str) -> dict[tuple, dict[str, tuple]]:
    """Per policy version, the rules (clauses, rows) that mention every term of the question,
    with the figures each one sets."""
    terms = [t for t in key_terms(question) if not t.isdigit()]
    found: dict[tuple, dict[str, tuple]] = {}
    if not terms:
        return found
    for item in evidence.items:
        group = (item.source.policy_name or item.source.document_title, item.source.version_label)
        for rule in _RULE_BREAK.split(item.candidate.text):
            rule = " ".join(rule.split())
            figures = tuple(sorted(f.value for f in extract_numeric_facts(rule) if f.kind != "date"))
            if not figures or not all(TermIndex(rule).mentions(t) for t in terms):
                continue
            clause = (m.group(1) if (m := _CLAUSE_START.match(rule)) else None) or f"page {item.source.page_start}"
            found.setdefault(group, {}).setdefault(clause, (figures, rule, item.source.page_start))
    return found


def _refuse_if_ambiguous(evidence: EvidenceSet, question: str) -> None:
    """Ask which rule is meant, rather than pick one, when several different rules answer the
    question as asked with different figures ("the retention period for the approval note" when
    the policy sets 7, 10, 12 and 14 years in different chapters)."""
    if (requested_clauses(question) or requested_codes(question) or _WANTS_ALL.search(question)
            or is_document_question(question)):
        return
    for (policy, version), rules in _rules_matching(evidence, question).items():
        if len(rules) < AMBIGUOUS_RULES or len({figures for figures, _, _ in rules.values()}) < AMBIGUOUS_RULES:
            continue
        where = f"{policy} v{version}" if version else policy
        choices = [
            f"Clause {clause} (page {page}): {rule[:140].rstrip()}{'…' if len(rule) > 140 else ''}"
            if not clause.startswith("page") else f"{clause.capitalize()}: {rule[:140]}"
            for clause, (_, rule, page) in list(rules.items())[:MAX_CHOICES]
        ]
        raise _NoAnswer(
            "AMBIGUOUS",
            suggestions=choices + ["Or name the clause, section or product you mean."],
            message=(f"{where} has at least {len(rules)} different rules that match this question and set "
                     "different values, so I won't pick one. Which one do you mean?"),
        )


def _best_rule(text: str, claim: str) -> str | None:
    """The rule (clause, line) of a passage that a claim restates."""
    rules = [" ".join(r.split()) for r in _RULE_BREAK.split(text) if r.strip()]
    scored = [(_overlap(r, claim), r) for r in rules]
    best = max(scored, default=(0.0, None))
    return best[1] if best[0] >= 0.5 else None


def _overlap(a: str, b: str) -> float:
    words_a, words_b = _claim_words(a), _claim_words(b)
    return len(words_a & words_b) / max(1, min(len(words_a), len(words_b)))


def _rule_key(rule: str) -> str:
    return " ".join(re.findall(r"[a-z0-9.%]+", rule.lower()))


def _focused(evidence: EvidenceSet, question: str) -> EvidenceSet | None:
    """The passages that mention every term of the question, when only some of them do."""
    terms = [t for t in key_terms(question) if not t.isdigit()]
    if not terms:
        return None
    items = [i for i in evidence.items if all(TermIndex(i.full_text).mentions(t) for t in terms)]
    if not items or len(items) == len(evidence.items):
        return None
    kept = {i.id for i in items}
    conflicts = [c for c in evidence.conflicts if all(e in kept for e in c["evidence_ids"])]
    return dataclasses.replace(evidence, items=items, conflicts=conflicts)


def _in_force(version: PolicyVersion, on: date) -> bool:
    return version.effective_from <= on and (version.effective_to is None or version.effective_to > on)


def _label(source, *, named: bool = True) -> str:
    """What the evidence header says about a passage: its policy, section, version and period.
    "Version 2.0 has the later effective date" is supported by the header, not the passage text.

    A passage from a document filed under a policy it is not a version of (`named=False`) is not
    credited with the policy's name: that name says nothing about what the passage is about."""
    version = f"Version {source.version_label}" if source.version_label else None
    period = None
    if source.effective_from:
        end = source.effective_to.isoformat() if source.effective_to else "present"
        period = f"effective date {source.effective_from.isoformat()} to {end}"
    names = [source.policy_name, source.document_title] if named else []
    return " ".join(filter(None, [*names, source.section_path, version, period]))


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
