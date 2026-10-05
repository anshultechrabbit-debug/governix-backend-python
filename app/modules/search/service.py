import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.infrastructure.ai.embeddings.base import EmbeddingProvider
from app.infrastructure.cache.base import Cache, CacheScope, build_cache_key
from app.modules.audit.service import record_event
from app.modules.auth.acl import visible_clause
from app.modules.auth.permissions import Principal
from app.modules.auth.scope import tenant_id
from app.modules.documents.model import Document
from app.modules.organizations.model import Organization
from app.modules.policies.model import Policy, PolicyStatus, PolicyVersion
from app.modules.search.retrieval import (
    Candidate,
    HybridRetriever,
    SearchFilters,
    VersionScope,
    query_terms,
)
from app.modules.search.query_intelligence import query_variants
from app.modules.search.rephrase import MAX_REPHRASINGS, rephraser
from app.modules.search.schema import Passage, PolicyHit, Provenance, SearchRequest, SearchResponse

QUERY_VECTOR_TTL = 24 * 3600
# Upper bound on concurrent lexical variants. Each variant drives 4 lanes, so
# this caps a single question at VARIANT_POOL_SIZE x 4 in-flight statements.
VARIANT_POOL_SIZE = 3


def cache_scope(session: Session, principal: Principal) -> CacheScope:
    organization = session.get(Organization, tenant_id(principal))
    return CacheScope(
        organization_id=principal.organization_id,
        branch_id=principal.branch_id,
        department_id=principal.department_id,
        permission_scope=principal.permission_scope,
        knowledge_version=str(organization.knowledge_version),
    )


def embed_query_cached(embedder: EmbeddingProvider | None, cache: Cache, query: str) -> list[float] | None:
    """Query vectors depend only on (model, text): safe to share across tenants."""
    if embedder is None:
        return None
    key = build_cache_key("qvec", CacheScope.system(), embedder.model_id, " ".join(query.lower().split()))
    if (vector := cache.get(key)) is not None:
        return vector
    vector = embedder.query_vector(query)
    if vector is not None:  # a stand-in (the model is unavailable) is neither used nor cached
        cache.set(key, vector, ttl_seconds=QUERY_VECTOR_TTL)
    return vector


def build_filters(request) -> SearchFilters:
    return SearchFilters(
        version_scope=VersionScope(request.mode, request.as_of, list(request.version_ids)),
        category_ids=list(request.category_ids),
        policy_ids=list(request.policy_ids),
        document_ids=list(request.document_ids),
        branch_id=request.branch_id,
        department_id=request.department_id,
    )


def retrieve_with_variants(principal, retriever, embedder, cache, query, filters, *, limit: int, rephrase=None):
    """Fuse independently retrieved lexical variants with RRF.

    Every individual retrieval retains its SQL ACL/version predicate; fusion
    only combines already-authorised candidates.

    `rephrase` (see search.rephrase) rewords the query for documents whose vectors
    are still being written; each rewording runs the keyword lanes over those only.
    """
    jobs = [(variant, filters, True) for variant in query_variants(query)]
    if rephrase is not None and (pending := retriever.documents_still_embedding(principal, filters)):
        scoped = replace(filters, document_ids=pending)
        jobs += [(reworded, scoped, False) for reworded in rephrase(query)]
    if len(jobs) == 1:
        return retriever.retrieve(
            principal, jobs[0][0], filters, limit=limit,
            query_vector=embed_query_cached(embedder, cache, jobs[0][0]),
        )
    # Variants run concurrently on the shared lane pool. Running them in series
    # tripled wall-clock latency for a recall gain that does not need it.
    with ThreadPoolExecutor(max_workers=min(len(jobs), VARIANT_POOL_SIZE + MAX_REPHRASINGS)) as pool:
        futures = [
            pool.submit(
                retriever.retrieve, principal, variant, scope, limit=limit, semantic=semantic,
                query_vector=embed_query_cached(embedder, cache, variant) if semantic else None,
            )
            for variant, scope, semantic in jobs
        ]
        results = [future.result() for future in futures]
    fused = {}
    for variant_index, result in enumerate(results):
        for rank, candidate in enumerate(result.candidates, start=1):
            existing = fused.setdefault(candidate.chunk_id, candidate)
            existing.ranks[f"query_{variant_index + 1}"] = rank
            existing.fused += 1 / (60 + rank)
    merged = sorted(fused.values(), key=lambda c: c.fused, reverse=True)[:limit]
    base = results[0]
    base.candidates = merged
    base.timings_ms["query_variants"] = len(jobs)
    base.lane_counts["query_variants"] = len(jobs)
    return base


def provenance(session: Session, candidates: list[Candidate]) -> dict[uuid.UUID, Provenance]:
    """Document/policy/version details for candidates (all already ACL-filtered).

    Loads only the columns needed for Provenance, not full ORM objects, to
    reduce per-request memory and serialisation overhead.
    """
    if not candidates:
        return {}
    doc_ids = {c.document_id for c in candidates}
    policy_ids = {c.policy_id for c in candidates if c.policy_id}
    version_ids = {c.version_id for c in candidates if c.version_id}

    # Scalar column selects: 3 round trips, loading only the columns we actually use.
    doc_rows = session.execute(
        select(Document.id, Document.title, Document.category_id)
        .where(Document.id.in_(doc_ids))
    ).all()
    docs = {row.id: row for row in doc_rows}

    pol_rows = session.execute(
        select(Policy.id, Policy.name)
        .where(Policy.id.in_(policy_ids))
    ).all() if policy_ids else []
    policies = {row.id: row for row in pol_rows}

    ver_rows = session.execute(
        select(
            PolicyVersion.id, PolicyVersion.version_label,
            PolicyVersion.effective_from, PolicyVersion.effective_to,
            PolicyVersion.effective_date_source,
        )
        .where(PolicyVersion.id.in_(version_ids))
    ).all() if version_ids else []
    versions = {row.id: row for row in ver_rows}

    result = {}
    for c in candidates:
        document = docs[c.document_id]
        policy = policies.get(c.policy_id)
        version = versions.get(c.version_id)
        result[c.chunk_id] = Provenance(
            document_id=c.document_id,
            document_title=document.title,
            policy_id=c.policy_id,
            policy_name=policy.name if policy else None,
            category_id=document.category_id,
            version_id=c.version_id,
            version_label=version.version_label if version else None,
            # A placeholder date (no effective date was stated) is not shown or given
            # to the model as if it were a business date.
            effective_from=version.effective_from if version and _real_date(version) else None,
            effective_to=version.effective_to if version and _real_date(version) else None,
            section_number=c.section_number,
            section_path=c.section_path,
            page_start=c.page_start,
            page_end=c.page_end,
        )
    return result


class SearchService:
    def __init__(
        self,
        session: Session,
        session_factory: sessionmaker[Session],
        embedder: EmbeddingProvider | None,
        cache: Cache,
        llm_factory=None,
    ) -> None:
        self.session = session
        self.rephrase = rephraser(llm_factory)
        self.cache = cache
        self.embedder: EmbeddingProvider | None = embedder
        self.retriever = HybridRetriever(session_factory, embedder)

    def search(self, principal: Principal, request: SearchRequest) -> SearchResponse:
        scope = cache_scope(self.session, principal)
        key = build_cache_key("search", scope, request.model_dump(mode="json"))
        if (cached := self.cache.get(key)) is not None:
            response = SearchResponse.model_validate(cached)
            response.cache_hit = True
            self._audit(principal, request, len(response.passages), cache_hit=True)
            return response

        filters = build_filters(request)
        result = retrieve_with_variants(
            principal, self.retriever, self.embedder, self.cache, request.query, filters, limit=request.limit,
            rephrase=self.rephrase,
        )
        sources = provenance(self.session, result.candidates)
        response = SearchResponse(
            query=request.query,
            mode=request.mode,
            as_of=filters.version_scope.effective_date(),
            passages=[
                Passage(chunk_id=c.chunk_id, text=c.text, score=round(c.fused, 5), lanes=c.ranks, source=sources[c.chunk_id])
                for c in result.candidates
            ],
            policies=self._policy_hits(principal, request.query),
            terms=query_terms(request.query),
            timings_ms=result.timings_ms,
        )
        self.cache.set(key, response.model_dump(mode="json"))
        self._audit(principal, request, len(response.passages), cache_hit=False)
        return response

    def _policy_hits(self, principal: Principal, query: str) -> list[PolicyHit]:
        normalized = " ".join(query_terms(query))
        if not normalized:
            return []
        rows = self.session.scalars(
            select(Policy).where(
                visible_clause(principal, Policy.organization_id, Policy.branch_id, Policy.department_id, Policy.id),
                Policy.status == PolicyStatus.ACTIVE,
                or_(
                    func.word_similarity(normalized, Policy.normalized_name) >= 0.5,
                    func.upper(Policy.policy_number) == query.strip().upper(),
                ),
            ).order_by(func.word_similarity(normalized, Policy.normalized_name).desc()).limit(5)
        )
        return [PolicyHit(id=p.id, name=p.name, policy_number=p.policy_number, category_id=p.category_id) for p in rows]

    def _audit(self, principal: Principal, request: SearchRequest, results: int, *, cache_hit: bool) -> None:
        record_event(
            self.session, "search.query", actor=principal, resource_type="search",
            details={"query": request.query, "mode": request.mode, "results": results, "cache_hit": cache_hit,
                     "permission_scope": principal.permission_scope},
        )
        # flush() only: the outer transaction (FastAPI dependency) commits at
        # request end. A direct commit here would end the dependency's transaction
        # early and risk committing the audit row while other writes roll back.
        self.session.flush()


def _real_date(version) -> bool:
    from app.modules.policies.model import AUTO_DATE_SOURCES

    return version.effective_date_source not in AUTO_DATE_SOURCES
