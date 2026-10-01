"""Evidence engine: rerank, diversify, expand context, attach authority and
amendments, detect conflicts, and decide whether there is enough to answer.

All inputs are already permission- and version-filtered candidates; nothing
here widens the search scope except amendments, which are fetched through the
same ACL + effective-date predicate.
"""

import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.infrastructure.ai.reranker.base import RerankerProvider
from app.modules.auth.acl import can_see
from app.modules.auth.permissions import Principal
from app.modules.categories.model import Category
from app.modules.citations.numerics import extract_numeric_facts, sentence_at
from app.modules.rag.validation import TermIndex
from app.modules.documents.model import Document, DocumentStatus
from app.modules.policies.model import DocumentRelationship, PolicyVersion, RelationStatus, VersionStatus
from app.modules.search.retrieval import (
    Candidate,
    HybridRetriever,
    SearchFilters,
    VersionScope,
    expand_context,
    query_terms,
)
from app.modules.search.schema import Provenance
from app.modules.search.service import provenance

MAX_PER_DOCUMENT = 3
# Share of the reranker in the evidence order; the rest is the fused hybrid
# rank. Measured on the NEP acceptance set after the keyword lane gained IDF:
# 0.5 matched or beat 0.7 on every metric (R@1 0.46 -> 0.52, MRR 0.60 -> 0.63);
# 1.0 (reranker only) was clearly worse (R@1 0.37).
RERANK_WEIGHT = 0.5
GENERIC_TERMS = frozenset(
    "current currently latest policy policies bank banks rule rules document documents version "
    "details detail say says tell explain applicable".split()
)
# Words that frame a question rather than name its subject ("mentioned in the
# comments section", "the exact title"). Requiring them in the evidence rejects
# answerable questions before the model ever sees them.
QUESTION_TERMS = frozenset(
    "mention mentioned mentions discuss discussed discussion discusses describe described describes "
    "stated state's regarding according about exact exactly specific specifically particular "
    "section sections part mentioned given provide provided provides list listed name named "
    "suggest suggested suggests suggestion propose proposed identify identifies identified "
    "consider considered considers main key important overall summary summarise summarize "
    "agree agreed disagree did does stand stands mean means meant "
    # Greetings, politeness and how the answer should look ("in short", "a short script"):
    # they say how to answer, not what about, and no document is expected to contain them.
    "hello hi hey dear please kindly thanks thank want wanted wants need know tell explain explanation "
    "short shortly brief briefly simple simply quick quickly script overview whole entire just really "
    "help understand lines words points bullet bullets "
    # How to proceed, not what about: "how should X be handled", "different variants".
    "handle handled handling treat treated dealt deal manage managed different various "
    # Reasoning about a quoted rule: "if the following conditions are met, what follows",
    # "which statement is inconsistent with the source".
    "following follows follow met statement statements inconsistent consistent incorrect correct true false "
    "implies imply implied source quoted scenario conclusion conclude "
    "make makes made automatically basically actually giving given "
    "told tell telling yesterday today tomorrow might maybe perhaps sure much many could would "
    "some so am think thought heard said "
    "each every list category categories type types kind kinds interval intervals topic topics cover covers covered "
    # Joining words of rule-book questions: "Auto Loan extended to students", "serving exporters".
    "serving serve serves served extended extending covering segment segments".split()
)
# Questions about the document itself: its title, publisher, date, legal basis.
_DOCUMENT_QUESTION = re.compile(
    r"\bthis\s+(?:document|plan|policy|report|circular|manual|guideline|notification|publication)\b"
    r"|\b(?:title|name)\s+of\b|\bpublish(?:ed|er|ing)?\b|\bpublication\b|\bissu(?:ed|ing)\s+(?:by|authority)\b"
    r"|\blegal\s+basis\b|\bunder\s+which\s+(?:act|law|section)\b|\bwhich\s+section\s+of\b"
    r"|\b(?:type|kind|sort)\s+of\s+(?:document|policy|circular|report)\b|\bwhat\s+is\s+(?:this|the)\s+document\s+about\b",
    re.I,
)
AMENDING = {"AMENDS", "SUPERSEDES", "REPLACES", "CLARIFIES"}


@dataclass
class EvidenceItem:
    id: str
    candidate: Candidate
    source: Provenance
    score: float
    rerank_score: float
    context_before: str = ""
    context_after: str = ""
    category_name: str | None = None
    authority_rank: int = 0
    amended_by: list[dict] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        return "\n".join(t for t in (self.context_before, self.candidate.text, self.context_after) if t)


@dataclass
class EvidenceSet:
    items: list[EvidenceItem]
    coverage: float
    missing_terms: list[str]
    top_score: float
    conflicts: list[dict]
    comparison: dict | None = None  # deterministic diff for comparison questions

    def by_id(self) -> dict[str, EvidenceItem]:
        return {item.id: item for item in self.items}


def key_terms(question: str) -> list[str]:
    """Subject terms the evidence must contain. Numbers of two or more digits count:
    "170 schemes" or "in 2050" must be in the evidence, not only nearby words."""
    return [
        t for t in query_terms(question)
        if t not in GENERIC_TERMS and t not in QUESTION_TERMS and (not t.isdigit() or len(t) >= 2)
    ]


def is_document_question(question: str) -> bool:
    return bool(_DOCUMENT_QUESTION.search(question))


def coverage_of(question: str, texts: list[str]) -> tuple[float, list[str]]:
    """Fraction of the question's subject terms present in the evidence.

    Terms and evidence are both reduced to a stem (app.core.stemming) so that a
    morphological variant still counts: the evidence saying "renewable" answers a
    question about "renewables". The previous 6-character truncation instead
    produced false misses on long words and rejected answerable questions.

    A shared 5-character prefix also counts, which covers derivational pairs a
    suffix stemmer cannot join ("generate"/"generation", "require"/"requirement").
    Without it the gate rejected questions whose evidence used the noun form of a
    verb the question phrased as a verb. A hyphenated term ("FIU-IND") counts when
    each of its parts is present, as the text is indexed word by word.
    """
    terms = key_terms(question)
    if not terms:
        return 1.0, []  # nothing specific to look for; relevance is judged by score
    index = TermIndex("\n".join(texts))
    missing = [t for t in terms if not index.mentions(t)]
    return 1 - len(missing) / len(terms), missing


def build_evidence(
    session: Session,
    principal: Principal,
    question: str,
    candidates: list[Candidate],
    *,
    reranker: RerankerProvider | None,
    retriever: HybridRetriever,
    filters: SearchFilters,
    rerank_top_n: int,
    limit: int,
) -> EvidenceSet:
    pool = candidates[: max(rerank_top_n * 4, limit)]
    if not pool:
        return EvidenceSet([], 0.0, key_terms(question), 0.0, [])

    max_fused = max(c.fused for c in pool) or 1.0
    rerank = reranker.score(question, [f"{c.section_path}\n{c.text}" for c in pool]) if reranker else [
        c.fused / max_fused for c in pool
    ]
    for candidate, score in zip(pool, rerank):
        candidate.rerank_score = score
    ranked = sorted(pool, key=lambda c: _blend(c, max_fused), reverse=True)

    # Repeated boilerplate (a disclaimer on every page, a copied clause) must
    # not fill several evidence slots with the same words: keep the best-ranked
    # copy of each distinct passage.
    ranked = distinct_passages(ranked)
    selected: list[Candidate] = []
    per_document: Counter = Counter()
    for candidate in ranked[:rerank_top_n]:
        if per_document[candidate.document_id] >= MAX_PER_DOCUMENT:
            continue
        per_document[candidate.document_id] += 1
        selected.append(candidate)
        if len(selected) == limit:
            break
    # The per-document cap only exists to diversify across documents. When few
    # documents are relevant, fill the remaining slots in rank order instead of
    # answering from three passages.
    chosen = {c.chunk_id for c in selected}
    for candidate in ranked[:rerank_top_n]:
        if len(selected) >= limit:
            break
        if candidate.chunk_id not in chosen:
            chosen.add(candidate.chunk_id)
            selected.append(candidate)

    if is_document_question(question):
        present = {c.chunk_id for c in selected}
        documents = list(dict.fromkeys(c.document_id for c in selected))[:2]
        cover = [c for c in retriever.front_matter(principal, documents, filters) if c.chunk_id not in present]
        for candidate in cover:
            candidate.rerank_score = candidate.rerank_score or 0.0
        selected = cover + selected

    selected += _amendment_candidates(session, principal, question, selected, retriever, filters)
    sources = provenance(session, selected)
    neighbours = expand_context(session, selected)
    categories = _categories(session, [s.category_id for s in sources.values()])

    items = []
    for index, candidate in enumerate(selected, start=1):
        before = [n.text for n in neighbours.get(candidate.chunk_id, []) if n.chunk_index < candidate.chunk_index]
        after = [n.text for n in neighbours.get(candidate.chunk_id, []) if n.chunk_index > candidate.chunk_index]
        source = sources[candidate.chunk_id]
        category = categories.get(source.category_id)
        items.append(EvidenceItem(
            id=f"E{index}",
            candidate=candidate,
            source=source,
            score=round(_blend(candidate, max_fused), 4),
            rerank_score=round(candidate.rerank_score, 4),
            context_before=" ".join(before)[-600:],
            context_after=" ".join(after)[:600],
            category_name=category.name if category else None,
            authority_rank=category.authority_rank if category else 0,
        ))
    _attach_amendments(session, principal, items, filters.version_scope.effective_date())
    coverage, missing = coverage_of(question, [i.full_text + " " + i.source.section_path for i in items])
    return EvidenceSet(
        items=items,
        coverage=round(coverage, 3),
        missing_terms=missing,
        top_score=max((i.score for i in items), default=0.0),
        conflicts=detect_conflicts(items),
    )


def _blend(candidate: Candidate, max_fused: float) -> float:
    return RERANK_WEIGHT * (candidate.rerank_score or 0.0) + (1 - RERANK_WEIGHT) * candidate.fused / max_fused


def _passage_key(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def distinct_passages(candidates: list[Candidate]) -> list[Candidate]:
    """Candidates in order, dropping any whose text repeats an earlier one's."""
    seen: set[str] = set()
    distinct = []
    for candidate in candidates:
        key = _passage_key(candidate.text)
        if key in seen:
            continue
        seen.add(key)
        distinct.append(candidate)
    return distinct


def _categories(session: Session, ids) -> dict[uuid.UUID, Category]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {c.id: c for c in session.scalars(select(Category).where(Category.id.in_(ids)))}


# Maximum number of amending documents to retrieve passages from.
# Each one costs a full 4-lane retrieval; without a cap the cost is O(amendments).
MAX_AMENDMENT_DOCS = 4


def _amendment_candidates(session, principal, question, selected, retriever: HybridRetriever, filters) -> list[Candidate]:
    """Pull in the best passage of any in-force document that amends a selected policy.

    Capped at MAX_AMENDMENT_DOCS: each amending document triggers a full 4-lane
    hybrid retrieval, so without a bound the cost is linear in the number of
    confirmed amendments — which can be large for long-lived policies.
    """
    policy_ids = {c.policy_id for c in selected if c.policy_id}
    if not policy_ids:
        return []
    sources = session.execute(
        select(DocumentRelationship.source_document_id)
        .join(Document, Document.id == DocumentRelationship.source_document_id)
        .where(
            DocumentRelationship.target_policy_id.in_(policy_ids),
            DocumentRelationship.status == RelationStatus.CONFIRMED,
            DocumentRelationship.relation_type.in_(AMENDING),
            Document.status == DocumentStatus.READY,
        )
    ).scalars().all()
    present = {c.document_id for c in selected}
    missing = [d for d in dict.fromkeys(sources) if d not in present]
    if not missing:
        return []
    # Cap before firing any retrieval: cost = MAX_AMENDMENT_DOCS × 4 lane queries max.
    missing = missing[:MAX_AMENDMENT_DOCS]
    # Same ACL + version scope as the main retrieval; only the document set is
    # narrowed. This runs the full 4-lane retriever, so it is the single most
    # expensive step in evidence building: at most one extra passage per amending
    # document is kept, so the request is bounded by the number of amendments.
    scoped = SearchFilters(version_scope=filters.version_scope, document_ids=missing)
    extra = retriever.retrieve(principal, question, scoped, limit=len(missing) * 2).candidates
    best: dict[uuid.UUID, Candidate] = {}
    for candidate in extra:
        best.setdefault(candidate.document_id, candidate)
    for candidate in best.values():
        candidate.rerank_score = candidate.rerank_score or 0.0
    return list(best.values())


def _attach_amendments(session: Session, principal: Principal, items: list[EvidenceItem], as_of: date | None) -> None:
    policy_ids = {i.source.policy_id for i in items if i.source.policy_id}
    if not policy_ids:
        return
    rows = session.execute(
        select(DocumentRelationship, Document, PolicyVersion)
        .join(Document, Document.id == DocumentRelationship.source_document_id)
        .join(PolicyVersion, PolicyVersion.id == Document.policy_version_id)
        .where(
            DocumentRelationship.target_policy_id.in_(policy_ids),
            DocumentRelationship.status == RelationStatus.CONFIRMED,
            DocumentRelationship.relation_type.in_(AMENDING),
            Document.status == DocumentStatus.READY,
            PolicyVersion.status == VersionStatus.ACTIVE,
        )
    ).all()
    for relationship, document, version in rows:
        if not can_see(principal, document.organization_id, document.branch_id, document.department_id, document.policy_id):
            continue
        if as_of is not None and version.effective_from > as_of:
            continue  # the amendment was not yet in force at the question's date
        info = {
            "relation_type": relationship.relation_type,
            "document_id": str(document.id),
            "document_title": document.title,
            "clauses": relationship.clauses,
            "effective_from": version.effective_from.isoformat(),
        }
        for item in items:
            if item.source.policy_id != relationship.target_policy_id:
                continue
            number = item.source.section_number or ""
            if not relationship.clauses or any(number == c or number.startswith(f"{c}.") for c in relationship.clauses):
                item.amended_by.append(info)


def detect_conflicts(items: list[EvidenceItem]) -> list[dict]:
    """Deterministic conflicts: amended clauses, and differing figures for the same subject.

    The inner fact-pair loop is O(items² × facts²). With RAG_EVIDENCE_LIMIT=14 items
    this is bounded, but we exit as soon as the 10-conflict cap is reached so that
    raising the limit in the future does not silently create a hot path.
    """
    MAX_CONFLICTS = 10
    conflicts: list[dict] = []
    seen: set[tuple] = set()

    def _add(conflict: dict) -> bool:
        """Add if unseen; return True when the cap is reached."""
        key = (conflict["type"], tuple(sorted(conflict["evidence_ids"])))
        if key not in seen:
            seen.add(key)
            conflicts.append(conflict)
        return len(conflicts) >= MAX_CONFLICTS

    by_document = {i.source.document_id: i for i in items}
    for item in items:
        for amendment in item.amended_by:
            amending = by_document.get(uuid.UUID(amendment["document_id"]))
            capped = _add({
                "type": "AMENDED",
                "description": (
                    f"{item.source.policy_name} section {item.source.section_number or ''} is "
                    f"{amendment['relation_type'].lower()} by '{amendment['document_title']}' "
                    f"(effective {amendment['effective_from']})."
                ).replace("  ", " "),
                "evidence_ids": [item.id] + ([amending.id] if amending else []),
                "resolution_hint": "The later, more specific amendment governs the amended clause.",
            })
            if capped:
                return conflicts

    facts = {
        i.id: [(f, sentence_at(i.candidate.text, f.start, f.end)) for f in extract_numeric_facts(i.candidate.text)
               if f.kind in ("percent", "amount", "duration")]
        for i in items
    }
    for a_index, a in enumerate(items):
        for b in items[a_index + 1:]:
            if a.source.document_id == b.source.document_id or a.source.policy_id == b.source.policy_id:
                continue
            for fact_a, sentence_a in facts[a.id]:
                for fact_b, sentence_b in facts[b.id]:
                    if fact_a.kind != fact_b.kind or fact_a.value == fact_b.value:
                        continue
                    shared = _subject_words(sentence_a) & _subject_words(sentence_b)
                    if len(shared) >= 3:
                        higher = max((a, b), key=lambda i: (i.authority_rank, i.source.effective_from or date.min))
                        capped = _add({
                            "type": "NUMERIC_DISAGREEMENT",
                            "description": (
                                f"'{a.source.policy_name}' states {fact_a.raw} while "
                                f"'{b.source.policy_name}' states {fact_b.raw} for "
                                f"{' '.join(sorted(shared)[:5])}."
                            ),
                            "evidence_ids": [a.id, b.id],
                            "authority": [
                                {"evidence_id": i.id, "category": i.category_name, "authority_rank": i.authority_rank,
                                 "effective_from": i.source.effective_from.isoformat() if i.source.effective_from else None}
                                for i in (a, b)
                            ],
                            "resolution_hint": (
                                f"Higher authority / later effective date: {higher.id}. "
                                "Both are shown; confirm which applies to your case."
                            ),
                        })
                        if capped:
                            return conflicts
    return conflicts


# Units, comparisons and connecting words: two figures that share only these ("Rs 100 crore"
# and "less than Rs 1 crore") are not about the same thing.
_NOT_SUBJECT = frozenset(
    "shall the and for not exceed exceeding with from above below than per this that which will may must "
    "are was were has have been any all such other more less equal equals upto up to over under minimum maximum "
    "crore crores lakh lakhs lac lacs rupees rupee inr thousand million billion amount amounts rate rates "
    "percent per cent year years month months day days period value total".split()
)


def _subject_words(sentence: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{3,}", sentence.lower())} - GENERIC_TERMS - _NOT_SUBJECT


def default_filters(scope: VersionScope, policy_ids: list[uuid.UUID]) -> SearchFilters:
    return SearchFilters(version_scope=scope, policy_ids=policy_ids)
