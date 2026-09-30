"""Hybrid retrieval: ACL + metadata + version filters FIRST, then four lanes.

    exact    quoted phrases, clause numbers ("5.2"), named divisions, page
             numbers ("page 500"), identifiers ("BNK-126"), policy/circular numbers
    keyword  PostgreSQL full-text (OR of query lexemes), ranked by IDF-weighted
             term matches: ts_rank_cd alone has no IDF, so a rare discriminating
             term ("5000") drowned among common ones ("policy", "page")
    vector   pgvector HNSW cosine (iterative scan keeps recall under filters)
    section  section-level full text (document -> section -> chunk)

Every lane runs the same scope predicate inside its own SQL, so unauthorised or
out-of-date content can never enter the candidate set. Lanes run in parallel
and are fused with weighted Reciprocal Rank Fusion.

Cost control: the executor is module-level and shared by every request (a pool
per `retrieve()` call used to multiply threads by lane-count x variant-count),
every lane runs under a `statement_timeout`, and the exact lane issues one
batched statement per match class instead of one query per phrase/clause.
"""

import atexit
import logging
import math
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Literal

from sqlalchemy import Select, and_, func, literal_column, or_, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.infrastructure.ai.embeddings.base import EmbeddingProvider
from app.modules.auth.acl import visible_clause
from app.modules.auth.permissions import Principal
from app.modules.documents.model import Document, DocumentSection, DocumentStatus
from app.modules.policies.model import Policy, PolicyStatus, PolicyVersion, VersionStatus
from app.modules.search.model import Chunk
from app.modules.versions.timeline import effective_on

logger = logging.getLogger(__name__)

RRF_K = 60
LANE_WEIGHTS = {"exact": 1.5, "keyword": 1.0, "vector": 1.0, "section": 0.6}
LANE_LIMITS = {"exact": 20, "keyword": 50, "vector": 50, "section": 30}
POLICY_BOOST = 1.3
# Second vector pass restricted to the leading candidate documents. A global
# HNSW search can fail to reach a small document whose neighbours sit in a graph
# dominated by other (often near-duplicate) content; restricted to a few
# documents, the planner uses an exact or filtered scan and recall is complete.
ROUTED_DOCUMENTS = 3
HNSW_EF_SEARCH = 100
STOPWORDS = frozenset(
    "a an and are as at be by can do does for from has have how i in is it its me my of on or our "
    "please should tell than that the their there these this to us was we what when where which who "
    "why will with would you your".split()
)
_TOKEN = re.compile(r"[a-z0-9]+(?:[.\-/][a-z0-9]+)*")
_PHRASE = re.compile(r'"([^"]{3,200})"')
_CLAUSE = re.compile(r"\b(?:clause|section|para(?:graph)?|point)\s+(\d{1,2}(?:\.\d{1,3}){0,4})\b", re.I)
# "Chapter 5", "Annexure II": a named division, found by its heading or contents row.
_DIVISION = re.compile(r"\b(chapter|annexure|annex|appendix|schedule|part)\s+(\d{1,3}|[ivxlcdm]{1,6})\b", re.I)
FRONT_MATTER_CHUNKS = 3
# Chunks kept per clause/division in the batched exact lane: enough to cover a
# short section without letting one clause monopolise the lane's budget.
EXACT_PER_CLAUSE = 10
EXACT_PER_DIVISION = 8
_IDENTIFIER = re.compile(r"\b[A-Za-z]{1,10}[-/][A-Za-z0-9]{1,10}(?:[-/][A-Za-z0-9]{1,10}){0,4}\b")
# A code that carries a number: "BNK-126", "HL-2025-01", "RBI/2025-26/12".
# Found in chunk text as written (FTS splits "BNK-126" into 'bnk' and '-126').
_CODE = re.compile(r"\b[A-Za-z]{1,10}[-/]?\d[A-Za-z0-9]*(?:[-/][A-Za-z0-9]+){0,4}\b")
# "page 500", "pages 12-14", "p. 7", "pg 30"
_PAGE = re.compile(
    r"\b(?:pages?|pg\.?|p\.)\s*(?:no\.?\s*|number\s*|#\s*)?(\d{1,6})(?:\s*(?:-|–|to)\s*(\d{1,6}))?\b", re.I
)
MAX_PAGE_SPAN = 5
# A question that names an uploaded file ("what does policy_document.pdf cover?").
_FILE_EXTENSION = re.compile(r"\.(?:pdf|docx?|txt|rtf)\b", re.I)
EXACT_PER_PAGE = 12
EXACT_PER_CODE = 20
# IDF statistics: document frequencies are counted per organisation, capped, and
# cached briefly. A capped count keeps a very common term cheap to measure; its
# weight is then only an upper bound, which still ranks it below rare terms.
DF_CAP = 20_000
DF_TTL_SECONDS = 600
_DF_CACHE: dict[tuple, tuple[float, int]] = {}
_DF_LOCK = threading.Lock()
# Keep the acronym anchored to a line start so a word merely *ending* in the
# letters (e.g. "CATHV" inside a longer token) cannot match a glossary row.
_PHRASE_GUARD = r"(?:^|\n)\s*"

VersionMode = Literal["current", "as_of", "versions", "all"]

# Every retrieval statement is bounded: a pathological plan or a missing index
# must fail fast instead of holding a worker thread and a pooled connection
# open indefinitely. A slow lane degrades the answer; it never hangs the API.
STATEMENT_TIMEOUT_MS = 5_000
# One shared pool for the whole process. Sized for the lanes of a single
# request, not for the number of requests: lanes are short database calls, so
# threads are recycled rather than grown.
LANE_POOL_SIZE = 8


_POOL_LOCK = threading.Lock()
_POOL: ThreadPoolExecutor | None = None


def lane_pool() -> ThreadPoolExecutor:
    """The process-wide lane executor, created on first use.

    Long-lived rather than per-call: a pool per `retrieve()` multiplied threads
    by (lanes x query variants) for every question, and short-lived threads pay
    the creation cost each time. Recreated if it was shut down (app restart
    within one process, or a test that tears the app down between cases),
    because a shut-down executor refuses all further work.
    """
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = ThreadPoolExecutor(max_workers=LANE_POOL_SIZE, thread_name_prefix="gx-lane")
            atexit.register(shutdown_lane_pool, wait=False, cancel_futures=True)
        return _POOL


def shutdown_lane_pool(*_args, **_kwargs) -> None:
    """Release the shared pool. A later call to `lane_pool` builds a fresh one."""
    global _POOL
    with _POOL_LOCK:
        pool, _POOL = _POOL, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


@dataclass
class VersionScope:
    mode: VersionMode = "current"
    as_of: date | None = None
    version_ids: list[uuid.UUID] = field(default_factory=list)

    def effective_date(self) -> date | None:
        if self.mode == "current":
            return datetime.now(UTC).date()
        if self.mode == "as_of":
            return self.as_of
        return None


@dataclass
class SearchFilters:
    version_scope: VersionScope = field(default_factory=VersionScope)
    category_ids: list[uuid.UUID] = field(default_factory=list)
    policy_ids: list[uuid.UUID] = field(default_factory=list)
    document_ids: list[uuid.UUID] = field(default_factory=list)
    branch_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None


@dataclass
class NamedDocument:
    document_id: uuid.UUID
    version_id: uuid.UUID | None
    filename: str


@dataclass
class Candidate:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    policy_id: uuid.UUID | None
    version_id: uuid.UUID | None
    section_id: uuid.UUID | None
    chunk_index: int
    text: str
    section_path: str
    section_number: str | None
    page_start: int
    page_end: int
    ranks: dict[str, int] = field(default_factory=dict)
    scores: dict[str, float] = field(default_factory=dict)
    fused: float = 0.0
    rerank_score: float | None = None


@dataclass
class RetrievalResult:
    candidates: list[Candidate]
    timings_ms: dict[str, float]
    lane_counts: dict[str, int]
    referenced_policy_ids: list[uuid.UUID]


def query_terms(query: str) -> list[str]:
    return [t for t in _TOKEN.findall(query.lower()) if t not in STOPWORDS and len(t) > 1]


def search_terms(query: str) -> list[str]:
    """Distinct query terms as the parser will see them."""
    return list(dict.fromkeys(query_terms(query)))


def term_tsquery(term: str):
    """One term parsed exactly like document text ("bnk-126" -> 'bnk' & '-126')."""
    return func.plainto_tsquery("english", term)


def or_tsquery(query: str):
    """OR of stemmed lexemes: recall-oriented; ts_rank_cd rewards matching more terms."""
    terms = [re.sub(r"[^a-z0-9]", " ", t).strip() for t in query_terms(query)]
    terms = [t for t in terms if t]
    if not terms:
        return None
    lexemes = " | ".join(f"({' & '.join(t.split())})" for t in dict.fromkeys(terms))
    return func.to_tsquery("english", lexemes)


def scope_conditions(principal: Principal, filters: SearchFilters) -> list:
    """The permission + version + metadata predicate shared by every lane."""
    conditions = [
        visible_clause(principal, Chunk.organization_id, Chunk.branch_id, Chunk.department_id, Chunk.policy_id),
        Document.status == DocumentStatus.READY,
        Policy.status == PolicyStatus.ACTIVE,
        PolicyVersion.status == VersionStatus.ACTIVE,
    ]
    scope = filters.version_scope
    if scope.mode == "versions":
        conditions.append(Chunk.version_id.in_(scope.version_ids or [uuid.uuid4()]))
    elif (as_of := scope.effective_date()) is not None:
        conditions.append(effective_on(as_of))
    if filters.category_ids:
        conditions.append(Chunk.category_id.in_(filters.category_ids))
    if filters.policy_ids:
        conditions.append(Chunk.policy_id.in_(filters.policy_ids))
    if filters.document_ids:
        conditions.append(Chunk.document_id.in_(filters.document_ids))
    if filters.branch_id:
        conditions.append(Chunk.branch_id == filters.branch_id)
    if filters.department_id:
        conditions.append(Chunk.department_id == filters.department_id)
    return conditions


def _base(columns, principal: Principal, filters: SearchFilters) -> Select:
    return (
        select(*columns)
        .join(Document, Document.id == Chunk.document_id)
        .join(PolicyVersion, PolicyVersion.id == Chunk.version_id)
        .join(Policy, Policy.id == Chunk.policy_id)
        .where(*scope_conditions(principal, filters))
    )


CHUNK_COLUMNS = (
    Chunk.id, Chunk.document_id, Chunk.policy_id, Chunk.version_id, Chunk.section_id, Chunk.chunk_index,
    Chunk.text, Chunk.section_path, Chunk.section_number, Chunk.page_start, Chunk.page_end,
)


def _candidate(row) -> Candidate:
    return Candidate(*row[: len(CHUNK_COLUMNS)])


class HybridRetriever:
    def __init__(self, session_factory: sessionmaker[Session], embedder: EmbeddingProvider | None) -> None:
        self.session_factory = session_factory
        self.embedder = embedder

    def _session(self):
        """A session whose statements are time-bounded. SET LOCAL needs a transaction."""
        session = self.session_factory()
        session.begin()
        session.execute(text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))
        return session

    def _read(self, statement) -> list:
        """Execute a read under the timeout and always release the connection."""
        try:
            with self._session() as session:
                return list(session.execute(statement))
        except OperationalError:
            # A timed-out or deadlane query degrades this lane only; the other
            # lanes and the fusion still produce an answer.
            logger.warning("Retrieval lane statement failed or timed out", exc_info=True)
            return []

    # --- term statistics ----------------------------------------------------------

    def term_weights(self, principal: Principal, terms: list[str]) -> dict[str, float]:
        """BM25-style IDF per term, from document frequencies within the caller's organisation.

        Statistics are organisation-wide rather than per user: they only order
        results the caller is already allowed to see.
        """
        if not terms or principal.organization_id is None:
            return {}
        org = principal.organization_id
        now = time.monotonic()
        wanted = ["__total__", *terms]
        with _DF_LOCK:
            cached = {t: v for t in wanted if (v := _DF_CACHE.get((org, t))) and v[0] > now}
        missing = [t for t in wanted if t not in cached]
        if missing:
            fresh = self._document_frequencies(org, missing)
            with _DF_LOCK:
                for term, count in fresh.items():
                    cached[term] = _DF_CACHE[(org, term)] = (now + DF_TTL_SECONDS, count)
        total = max(cached.get("__total__", (0, 1))[1], 1)
        weights = {}
        for term in terms:
            df = min(cached.get(term, (0, 0))[1], total)
            weights[term] = math.log(1 + (total - df + 0.5) / (df + 0.5))
        return weights

    def visible_term_weights(self, principal: Principal, terms: list[str]) -> dict[str, float]:
        """IDF of each term over the searchable chunks the caller can see.

        A term that occurs nowhere the caller can see gets the highest weight: it
        names something the caller's documents do not cover ("FIU-IND" asked of a
        bank with no KYC policy), which is the strongest sign that no answer
        exists. Only the caller's own visible chunks are counted, so this reveals
        nothing about documents outside their scope.
        """
        if not terms or principal.organization_id is None:
            return {}
        scope = ("visible", principal.organization_id, principal.permission_scope)
        now = time.monotonic()
        wanted = ["__total__", *dict.fromkeys(terms)]
        with _DF_LOCK:
            cached = {t: v[1] for t in wanted if (v := _DF_CACHE.get((*scope, t))) and v[0] > now}
        if missing := [t for t in wanted if t not in cached]:
            fresh = self._visible_frequencies(principal, missing)
            with _DF_LOCK:
                for term, count in fresh.items():
                    _DF_CACHE[(*scope, term)] = (now + DF_TTL_SECONDS, count)
            cached.update(fresh)
        total = max(cached.get("__total__", 1), 1)
        return {
            term: math.log(1 + (total - df + 0.5) / (df + 0.5))
            for term in dict.fromkeys(terms)
            for df in (min(cached.get(term, 0), total),)
        }

    def _visible_frequencies(self, principal: Principal, terms: list[str]) -> dict[str, int]:
        ready = (
            select(Chunk.id)
            .join(Document, Document.id == Chunk.document_id)
            .where(
                visible_clause(principal, Chunk.organization_id, Chunk.branch_id, Chunk.department_id, Chunk.policy_id),
                Document.status == DocumentStatus.READY,
            )
        )
        counts: dict[str, int] = {}
        try:
            with self._session() as session:
                for term in terms:
                    if term == "__total__":
                        query, cap = ready, DF_CAP * 50
                    else:
                        query, cap = ready.where(Chunk.tsv.op("@@")(func.plainto_tsquery("english", term))), DF_CAP
                    counts[term] = session.scalar(select(func.count()).select_from(query.limit(cap).subquery())) or 0
        except OperationalError:
            logger.warning("Visible term statistics failed; the salient-term gate is skipped", exc_info=True)
            return {}
        return counts

    def _document_frequencies(self, organization_id: uuid.UUID, terms: list[str]) -> dict[str, int]:
        """Capped match counts, one bounded statement for all terms (GIN-indexed)."""
        parts = []
        params = {"org": organization_id, "cap": DF_CAP}
        for index, term in enumerate(terms):
            if term == "__total__":
                parts.append(f"select '__total__' as term, (select count(*) from (select 1 from chunks "
                             f"where organization_id = :org limit :cap_total) s) as n")
                params["cap_total"] = DF_CAP * 50
                continue
            params[f"t{index}"] = term
            parts.append(
                f"select :t{index} as term, (select count(*) from (select 1 from chunks where organization_id = :org "
                f"and tsv @@ plainto_tsquery('english', :t{index}) limit :cap) s) as n"
            )
        try:
            with self._session() as session:
                rows = session.execute(text(" union all ".join(parts)), params).all()
        except OperationalError:
            logger.warning("Term statistics query failed; ranking without IDF", exc_info=True)
            return {t: 0 for t in terms}
        return {term: int(n) for term, n in rows}

    def _weighted_rank(self, tsv_column, terms: list[str], weights: dict[str, float], or_query):
        """BM25-like: sum over terms of IDF x the term's own field-weighted, saturating rank.

        ts_rank_cd of a single term honours the tsvector weights (section heading
        'A' outranks body text 'B') and saturates as rank/(rank+1); the IDF factor
        makes a rare term outweigh common ones. Cover density of the whole query
        is added so that terms occurring close together still score higher.
        """
        parts = [func.ts_rank_cd(tsv_column, term_tsquery(term), 32) * weights.get(term, 1.0) for term in terms]
        return sum(parts[1:], parts[0]) + func.ts_rank_cd(tsv_column, or_query, 32)

    # --- lanes -----------------------------------------------------------------

    def _keyword(self, principal, query, filters) -> list[tuple[Candidate, float]]:
        terms = search_terms(query)
        if not terms:
            return []
        or_query = _or_of(terms)
        rank = self._weighted_rank(Chunk.tsv, terms, self.term_weights(principal, terms), or_query).label("score")
        statement = (
            _base((*CHUNK_COLUMNS, rank), principal, filters)
            .where(Chunk.tsv.op("@@")(or_query))
            .order_by(rank.desc())
            .limit(LANE_LIMITS["keyword"])
        )
        return [(_candidate(r), float(r.score)) for r in self._read(statement)]

    def _vector(self, principal, vector, filters) -> list[tuple[Candidate, float]]:
        if vector is None:
            return []
        distance = Chunk.embedding.cosine_distance(vector).label("score")
        statement = (
            _base((*CHUNK_COLUMNS, distance), principal, filters)
            .where(Chunk.embedding.is_not(None), Chunk.embedding_model == self.embedder.model_id)
            .order_by(distance)
            .limit(LANE_LIMITS["vector"])
        )
        try:
            with self._session() as session:
                # Iterative scans keep returning neighbours until enough rows pass the filters.
                session.execute(text(f"SET LOCAL hnsw.ef_search = {HNSW_EF_SEARCH}"))
                session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
                rows = [(_candidate(r), 1.0 - float(r.score)) for r in session.execute(statement)]
        except OperationalError:
            logger.warning("Vector lane statement failed or timed out", exc_info=True)
            return []
        rows.sort(key=lambda item: item[1], reverse=True)  # relaxed order -> exact order
        return rows

    def _exact(self, principal, query, filters, referenced: list[uuid.UUID]) -> list[tuple[Candidate, float]]:
        """Exact-structure matches: quoted phrases, clause numbers, named divisions.

        Each match class is one statement. A question naming three clauses used
        to cost three round trips; a UNION ALL keeps the per-clause ordering
        while collapsing the whole lane to a single query.
        """
        results: list[tuple[Candidate, float]] = []
        for phrase in _PHRASE.findall(query):
            tsquery = func.phraseto_tsquery("english", phrase)
            rank = func.ts_rank_cd(Chunk.tsv, tsquery, 32).label("score")
            statement = (
                _base((*CHUNK_COLUMNS, rank), principal, filters)
                .where(Chunk.tsv.op("@@")(tsquery)).order_by(rank.desc()).limit(10)
            )
            results += [(_candidate(r), 2.0 + float(r.score)) for r in self._read(statement)]
        results += self._exact_clauses(principal, filters, referenced, _CLAUSE.findall(query), 1.5)
        results += self._exact_divisions(principal, filters, _DIVISION.findall(query))
        results += self._exact_pages(principal, filters, requested_pages(query))
        results += self._exact_codes(principal, filters, requested_codes(query))
        seen: set[uuid.UUID] = set()
        unique = []
        for candidate, score in sorted(results, key=lambda i: i[1], reverse=True):
            if candidate.chunk_id not in seen:
                seen.add(candidate.chunk_id)
                unique.append((candidate, score))
        return unique[: LANE_LIMITS["exact"]]

    def _exact_clauses(self, principal, filters, referenced, clauses, score: float) -> list[tuple[Candidate, float]]:
        """Every referenced clause ("5.2", "para 12") in one statement."""
        clauses = list(dict.fromkeys(clauses))
        if not clauses:
            return []
        condition = or_(*[
            or_(Chunk.section_number == clause, Chunk.section_number.like(f"{clause}.%"))
            for clause in clauses
        ])
        if referenced:
            condition = and_(condition, Chunk.policy_id.in_(referenced))
        # One ranked window per clause, not one global top-N: a question naming
        # 5.1 and 5.9 must not have 5.9 crowded out by many 5.1 rows.
        ranked = func.row_number().over(
            partition_by=Chunk.section_number, order_by=Chunk.chunk_index
        ).label("rn")
        inner = (
            _base((*CHUNK_COLUMNS, ranked, literal_column(str(score)).label("score")), principal, filters)
            .where(condition)
        ).subquery()
        statement = select(*[inner.c[c.key] for c in CHUNK_COLUMNS], inner.c.score).where(
            inner.c.rn <= EXACT_PER_CLAUSE
        ).order_by(inner.c.chunk_index)
        return [(_candidate(r), float(r.score)) for r in self._read(statement)]

    def _exact_divisions(self, principal, filters, divisions) -> list[tuple[Candidate, float]]:
        """Named divisions ("Chapter 5", "Annexure II") by heading and by body text."""
        divisions = list(dict.fromkeys((k.lower(), n.upper()) for k, n in divisions))
        if not divisions:
            return []
        results: list[tuple[Candidate, float]] = []
        # The division's own opening text. A chapter heading usually has no body
        # of its own (its label is carried into the first sub-section), so match
        # the section path "Chapter 8 > ..." as well as the section number.
        labels = [f"{k.title()} {n}" for k, n in divisions]
        statement = (
            _base((*CHUNK_COLUMNS, literal_column("1.9").label("score")), principal, filters)
            .where(or_(
                Chunk.section_number.in_(labels),
                *[or_(Chunk.section_path == label, Chunk.section_path.like(f"{label} >%")) for label in labels],
            ))
            .order_by(Chunk.chunk_index).limit(3 * len(divisions))
        )
        results += [(_candidate(r), 1.9) for r in self._read(statement)]
        # The contents row and heading text ("Chapter 5 | Analysis and Studies for
        # 2026-27"). One regex alternation for all divisions, not one per division.
        # Backed by ix_chunks_text_trgm (gin_trgm_ops) instead of a seq scan.
        pattern = "|".join(rf"\m{re.escape(k)}\s+{re.escape(n)}\M" for k, n in divisions)
        statement = (
            _base((*CHUNK_COLUMNS, literal_column("1.7").label("score")), principal, filters)
            .where(Chunk.text.op("~*")(pattern))
            .order_by(Chunk.chunk_index).limit(EXACT_PER_DIVISION * len(divisions))
        )
        results += [(_candidate(r), 1.7) for r in self._read(statement)]
        return results

    def _exact_pages(self, principal, filters, pages: list[int]) -> list[tuple[Candidate, float]]:
        """Every chunk on a page the question names ("What does page 500 contain?")."""
        if not pages:
            return []
        ranked = func.row_number().over(partition_by=Chunk.page_start, order_by=Chunk.chunk_index).label("rn")
        inner = (
            _base((*CHUNK_COLUMNS, ranked), principal, filters)
            .where(or_(*[and_(Chunk.page_start <= page, Chunk.page_end >= page) for page in pages]))
        ).subquery()
        statement = select(*[inner.c[c.key] for c in CHUNK_COLUMNS]).where(
            inner.c.rn <= EXACT_PER_PAGE
        ).order_by(inner.c.page_start, inner.c.chunk_index)
        return [(_candidate(r), 1.8) for r in self._read(statement)]

    def _exact_codes(self, principal, filters, codes: list[str]) -> list[tuple[Candidate, float]]:
        """Chunks containing an identifier exactly as written ("BNK-126"). Trigram-indexed."""
        if not codes:
            return []
        pattern = "|".join(rf"\m{re.escape(code)}\M" for code in codes)
        statement = (
            _base(CHUNK_COLUMNS, principal, filters)
            .where(Chunk.text.op("~*")(pattern))
            .order_by(Chunk.chunk_index).limit(EXACT_PER_CODE * len(codes))
        )
        return [(_candidate(r), 1.8) for r in self._read(statement)]

    def _section(self, principal, query, filters) -> list[tuple[Candidate, float]]:
        terms = search_terms(query)
        if not terms:
            return []
        tsquery = _or_of(terms)
        weights = self.term_weights(principal, terms)
        section_rank = self._weighted_rank(DocumentSection.tsv, terms, weights, tsquery)
        chunk_rank = self._weighted_rank(Chunk.tsv, terms, weights, tsquery)
        score = (section_rank * 0.6 + chunk_rank * 0.4).label("score")
        statement = (
            _base((*CHUNK_COLUMNS, score), principal, filters)
            .join(DocumentSection, DocumentSection.id == Chunk.section_id)
            .where(DocumentSection.tsv.op("@@")(tsquery))
            .order_by(score.desc())
            .limit(LANE_LIMITS["section"])
        )
        return [(_candidate(r), float(r.score)) for r in self._read(statement)]

    # --- orchestration -------------------------------------------------------------

    def glossary_rows(self, principal: Principal, acronym: str, filters: SearchFilters, *, limit: int = 10) -> list[Candidate]:
        """Chunks where `acronym` is immediately followed by its expansion.

        Acronym tables are split mid-table by the chunker, so the row a person
        can see in the PDF often lives in a tiny chunk of its own. Relevance
        ranking cannot surface it: prose that merely *mentions* the acronym ties
        on the rerank score and wins on fused rank. This is an exact-pattern
        lookup instead, still inside the same ACL + version predicate.

        The pattern is only a cheap SQL prefilter; the caller re-reads the rows
        and applies the strict expansion check.
        """
        acronym = re.escape(acronym)
        # "HVDC - High...", "HVDC\nHigh..." and table rows "HVDC | High..." / "Acronyms: HVDC | Expansion: High...".
        row = rf"{_PHRASE_GUARD}(?:[^|\n]{{0,40}}:\s*)?{acronym}\s*(?:[-—–:|]\s*|\n)\s*\S"
        inline = rf"\(\s*{acronym}s?\s*\)"  # "State Transmission Utilities (STUs)"
        # One statement for both patterns (OR'd) instead of two round trips.
        # ix_chunks_text_trgm keeps this off a sequential scan.
        statement = (
            _base(CHUNK_COLUMNS, principal, filters)
            .where(or_(Chunk.text.op("~")(row), Chunk.text.op("~")(inline)))
            .order_by(func.char_length(Chunk.text))
            .limit(limit)
        )
        found = [_candidate(r) for r in self._read(statement)]
        # Inline definitions first: a "(STUs)" mention is a weaker signal than a
        # glossary row, but the caller re-reads every row and applies the strict
        # expansion check, so ordering here only breaks ties between equal rows.
        return found

    def front_matter(self, principal: Principal, document_ids: list[uuid.UUID], filters: SearchFilters) -> list[Candidate]:
        """The opening chunks (cover, notification) of each document, inside the same ACL + scope."""
        if not document_ids:
            return []
        numbered = (
            _base((*CHUNK_COLUMNS, func.row_number().over(
                partition_by=Chunk.document_id, order_by=Chunk.chunk_index).label("n")), principal, filters)
            .where(Chunk.document_id.in_(document_ids))
            .subquery()
        )
        statement = select(*[numbered.c[c.key] for c in CHUNK_COLUMNS]).where(
            numbered.c.n <= FRONT_MATTER_CHUNKS
        ).order_by(numbered.c.document_id, numbered.c.chunk_index)
        return [_candidate(r) for r in self._read(statement)]

    def referenced_policies(self, principal: Principal, query: str) -> list[uuid.UUID]:
        """Policies the question names explicitly (by number, or by name inside the question)."""
        identifiers = [i.upper() for i in _IDENTIFIER.findall(query)]
        normalized = " ".join(query_terms(query))
        conditions = [func.word_similarity(Policy.normalized_name, normalized) >= 0.8] if normalized else []
        if identifiers:
            conditions += [func.upper(Policy.policy_number).in_(identifiers), func.upper(Policy.document_number).in_(identifiers)]
        if not conditions:
            return []
        statement = select(Policy.id).where(
            visible_clause(principal, Policy.organization_id, Policy.branch_id, Policy.department_id, Policy.id),
            Policy.status == PolicyStatus.ACTIVE,
            or_(*conditions),
        ).limit(10)
        return [row[0] for row in self._read(statement)]

    def referenced_documents(self, principal: Principal, query: str) -> list[NamedDocument]:
        """Documents the question names by their uploaded file name."""
        if not _FILE_EXTENSION.search(query):
            return []
        statement = select(Document.id, Document.policy_version_id, Document.original_filename).where(
            visible_clause(principal, Document.organization_id, Document.branch_id, Document.department_id, Document.policy_id),
            Document.status == DocumentStatus.READY,
            func.strpos(func.lower(query), func.lower(Document.original_filename)) > 0,
        ).limit(20)
        return [NamedDocument(*row) for row in self._read(statement) if names_file(query, row[2])]

    def retrieve(
        self,
        principal: Principal,
        query: str,
        filters: SearchFilters,
        *,
        limit: int = 60,
        query_vector: list[float] | None = None,
    ) -> RetrievalResult:
        timings: dict[str, float] = {}

        def timed(name, fn, *args):
            started = time.perf_counter()
            try:
                return fn(*args)
            finally:
                timings[name] = round((time.perf_counter() - started) * 1000, 1)

        referenced = timed("policy_lookup", self.referenced_policies, principal, query)
        if query_vector is None and self.embedder is not None:
            # Embed before fanning out: the network round trip is the longest
            # step and there is nothing for it to overlap with.
            query_vector = timed("embed_query", self.embedder.query_vector, query)
        # One shared pool for the whole process, not one per call: a pool per
        # retrieve() multiplied threads by (lanes x query variants) per question.
        pool = lane_pool()
        jobs = {
            "keyword": (timed, "keyword", self._keyword, principal, query, filters),
            "section": (timed, "section", self._section, principal, query, filters),
            "exact": (timed, "exact", self._exact, principal, query, filters, referenced),
            "vector": (timed, "vector", self._vector, principal, query_vector, filters),
        }
        futures = {lane: pool.submit(*args) for lane, args in jobs.items()}
        lanes = {lane: future.result() for lane, future in futures.items()}
        routed = routing_documents(lanes, filters)
        if routed and query_vector is not None:
            # Same metric as the global pass, so the two merge into ONE vector
            # ranking: the routed pass adds what the graph missed, it does not
            # get a second vote in the fusion.
            scoped = replace(filters, document_ids=routed)
            extra = timed("vector_routed", self._vector, principal, query_vector, scoped)
            lanes["vector"] = merge_by_similarity(lanes["vector"], extra, LANE_LIMITS["vector"])

        started = time.perf_counter()
        fused: dict[uuid.UUID, Candidate] = {}
        for lane, results in lanes.items():
            for rank, (candidate, score) in enumerate(results, start=1):
                existing = fused.setdefault(candidate.chunk_id, candidate)
                existing.ranks[lane] = rank
                existing.scores[lane] = round(score, 5)
                existing.fused += LANE_WEIGHTS[lane] / (RRF_K + rank)
        referenced_set = set(referenced)
        for candidate in fused.values():
            if candidate.policy_id in referenced_set:
                candidate.fused *= POLICY_BOOST
        ranked = sorted(fused.values(), key=lambda c: c.fused, reverse=True)[:limit]
        timings["fusion"] = round((time.perf_counter() - started) * 1000, 1)
        return RetrievalResult(
            candidates=ranked,
            timings_ms=timings,
            lane_counts={lane: len(results) for lane, results in lanes.items()},
            referenced_policy_ids=referenced,
        )


def routing_documents(lanes: dict[str, list[tuple[Candidate, float]]], filters: SearchFilters) -> list[uuid.UUID]:
    """The documents that lead the first-pass lanes, for the routed vector pass.

    Skipped when the caller already scoped the search to documents or policies:
    the first pass was then already restricted.
    """
    if filters.document_ids or filters.policy_ids:
        return []
    score: dict[uuid.UUID, float] = {}
    for lane, results in lanes.items():
        for rank, (candidate, _score) in enumerate(results, start=1):
            score[candidate.document_id] = score.get(candidate.document_id, 0.0) + LANE_WEIGHTS[lane] / (RRF_K + rank)
    return sorted(score, key=score.get, reverse=True)[:ROUTED_DOCUMENTS]


def merge_by_similarity(first: list[tuple[Candidate, float]], second: list[tuple[Candidate, float]],
                        limit: int) -> list[tuple[Candidate, float]]:
    best: dict[uuid.UUID, tuple[Candidate, float]] = {}
    for candidate, similarity in first + second:
        if candidate.chunk_id not in best or similarity > best[candidate.chunk_id][1]:
            best[candidate.chunk_id] = (candidate, similarity)
    return sorted(best.values(), key=lambda item: item[1], reverse=True)[:limit]


def _or_of(terms: list[str]):
    """OR of per-term queries, each parsed like the document text (tsquery || tsquery)."""
    query = term_tsquery(terms[0])
    for term in terms[1:]:
        query = query.op("||")(term_tsquery(term))
    return query


def names_file(query: str, filename: str) -> bool:
    """The whole file name, not the tail of a longer one ("a.pdf" inside "data.pdf")."""
    return re.search(rf"(?<![\w.-]){re.escape(filename)}(?![\w-])", query, re.I) is not None


def requested_pages(query: str) -> list[int]:
    pages: list[int] = []
    for start, end in _PAGE.findall(query):
        first = int(start)
        last = int(end) if end else first
        if last < first or last - first >= MAX_PAGE_SPAN:
            last = first
        pages += range(first, last + 1)
    return list(dict.fromkeys(p for p in pages if p > 0))


def requested_codes(query: str) -> list[str]:
    """Identifiers with a digit, excluding bare version labels ("v2") handled by the planner."""
    return list(dict.fromkeys(
        code for code in _CODE.findall(query)
        if not re.fullmatch(r"v(?:ersion)?\d+(?:\.\d+)*", code, re.I) and not code.isdigit()
    ))


def expand_context(session: Session, candidates: list[Candidate], window: int = 1) -> dict[uuid.UUID, list[Candidate]]:
    """Neighbouring chunks of the same section (the evidence's surrounding text).

    Neighbours come from the same, already-authorised document and version.

    One statement for all candidates. The previous loop issued a query per
    candidate, so an 8-item evidence set cost 8 round trips on the request's
    critical path; a self-join on the window predicate returns the same rows in
    a single round trip, attributed back to their anchor.
    """
    anchored = [c for c in candidates if c.section_id is not None]
    if not anchored:
        return {}
    anchors = select(
        Chunk.id.label("anchor_id"),
        Chunk.section_id.label("anchor_section_id"),
        Chunk.chunk_index.label("anchor_index"),
    ).where(Chunk.id.in_({c.chunk_id for c in anchored})).subquery()
    rows = session.execute(
        select(
            anchors.c.anchor_id, anchors.c.anchor_index, *CHUNK_COLUMNS
        )
        .select_from(Chunk)
        .join(
            anchors,
            and_(
                Chunk.section_id == anchors.c.anchor_section_id,
                Chunk.chunk_index.between(
                    anchors.c.anchor_index - window, anchors.c.anchor_index + window
                ),
                Chunk.id != anchors.c.anchor_id,
            ),
        )
        .order_by(anchors.c.anchor_id, Chunk.chunk_index)
    ).all()
    expansions: dict[uuid.UUID, list[Candidate]] = {}
    for row in rows:
        anchor_id, anchor_index, *columns = row
        expansions.setdefault(anchor_id, []).append(_candidate(tuple(columns)))
    return expansions
