"""Why do content questions return INSUFFICIENT_EVIDENCE? Show retrieved evidence."""
import sys
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import create_db_engine, create_session_factory
from app.core.model_registry import import_all_models
import_all_models()
from app.infrastructure.ai.embeddings.factory import create_embedder
from app.infrastructure.ai.llm.factory import create_llm
from app.infrastructure.ai.reranker.factory import create_reranker
from app.infrastructure.cache.factory import create_cache
from app.modules.auth.permissions import Principal
from app.modules.rag.evidence import build_evidence
from app.modules.rag.prompts import OUTPUT_SCHEMA, SYSTEM_PROMPT, build_user_prompt
from app.modules.rag.query_plan import plan_query
from app.modules.rag.validation import EvidenceText, validate_claims
from app.modules.search.retrieval import HybridRetriever, SearchFilters, VersionScope
from app.modules.search.service import retrieve_with_variants
from app.modules.users.model import User

QS = sys.argv[1:] or ["What is the planned rooftop solar capacity for 2032?"]

s = get_settings()
engine = create_db_engine(s)
sf = create_session_factory(engine)

with sf() as ses:
    u = ses.scalars(select(User).where(User.is_active.is_(True), User.organization_id.is_not(None)).order_by(User.role)).first()
    p = Principal(user_id=u.id, role=u.role, organization_id=u.organization_id,
                  branch_id=u.branch_id, department_id=u.department_id)
    embedder, reranker, cache, llm = create_embedder(s), create_reranker(s), create_cache(s), create_llm(s)
    retr = HybridRetriever(sf, embedder)
    for Q in QS:
        print("=" * 100)
        print("Q:", Q)
        plan = plan_query(Q, date_order=s.DATE_ORDER)
        print("plan:", plan.query_class, plan.mode, plan.as_of)
        filters = SearchFilters(version_scope=VersionScope("as_of", as_of=plan.as_of))
        res = retrieve_with_variants(p, retr, embedder, cache, Q, filters, limit=s.RAG_RETRIEVAL_CANDIDATES)
        print("candidates:", len(res.candidates), "lanes:", res.lane_counts)
        ev = build_evidence(ses, p, Q, res.candidates, reranker=reranker, retriever=retr,
                            filters=filters, rerank_top_n=s.RAG_RERANK_TOP_N, limit=s.RAG_EVIDENCE_LIMIT)
        print("evidence:", len(ev.items), "top:", ev.top_score, "coverage:", ev.coverage, "missing:", ev.missing_terms)
        for it in ev.items:
            print(f"  {it.id} p{it.source.page_start} score={it.score} rr={it.rerank_score} :: {it.candidate.text[:120]!r}")
        out = llm.generate_json(SYSTEM_PROMPT, build_user_prompt(Q, plan, ev), OUTPUT_SCHEMA,
                                context={"question": Q,
                                         "evidence": [{"id": i.id, "text": i.full_text} for i in ev.items],
                                         "conflicts": ev.conflicts})
        print("claims:", out.content.get("claims"))
        print("insufficient_evidence:", out.content.get("insufficient_evidence"))
        texts = {i.id: EvidenceText(i.id, i.full_text, set()) for i in ev.items}
        for r in validate_claims(out.content.get("claims", []), texts):
            print("  valid:", r.valid, "problems:", r.problems, "::", r.text[:200])
