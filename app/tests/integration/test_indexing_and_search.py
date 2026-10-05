import pytest
from sqlalchemy import func, select, update

from app.infrastructure.ai.embeddings.local import LocalEmbedding
from app.modules.audit.model import AuditEvent
from app.modules.documents.model import Document
from app.modules.ingestion.model import IngestionStage, Stage
from app.modules.ingestion.recovery import resume_ready_embeddings
from app.modules.organizations.model import Organization
from app.modules.search.model import Chunk, EmbeddingCache
from app.tests.factories import login, make_tenant
from app.tests.flows import V3, V4, build, confirm_new_policy, confirm_new_version, process
from app.tests.pipeline import drain
from app.workers.runtime import get_runtime

pytestmark = pytest.mark.integration


class CountingEmbedder(LocalEmbedding):
    def __init__(self):
        super().__init__(1536)
        self.embedded = 0

    def embed_documents(self, texts):
        self.embedded += len(texts)
        return super().embed_documents(texts)


@pytest.fixture
def embedder():
    counting = CountingEmbedder()
    get_runtime().overrides["embedder"] = counting
    return counting


@pytest.fixture
def tenant(db):
    return make_tenant(db)


@pytest.fixture
def admin(client, tenant):
    return login(client, tenant.admin)


@pytest.fixture
def home_loan(client, app, tenant, admin, embedder):
    v3_doc, analysis = process(client, app, tenant.admin, build(V3))
    policy_id = confirm_new_policy(admin, v3_doc, analysis).json()["data"]["policy_id"]
    drain(app)
    v4_doc, analysis = process(client, app, tenant.admin, build(V4))
    confirm_new_version(admin, v4_doc, analysis, policy_id)
    drain(app)
    return policy_id, v3_doc, v4_doc


def search(session, query, **body):
    response = session.post("/search", json={"query": query, **body})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_confirmed_documents_become_ready_and_indexed(db, admin, home_loan, embedder):
    policy_id, v3_doc, v4_doc = home_loan
    for document_id in (v3_doc, v4_doc):
        document = db.get(Document, document_id)
        assert document.status == "ready" and document.ready_at is not None
        chunks = db.scalars(select(Chunk).where(Chunk.document_id == document_id)).all()
        assert chunks and all(c.embedding is not None and c.embedding_model == embedder.model_id for c in chunks)
        assert all(c.version_id == document.policy_version_id and c.policy_id == document.policy_id for c in chunks)
        stages = {s["stage"]: s["status"] for s in admin.get(f"/documents/{document_id}/progress").json()["data"]["stages"]}
        assert all(status in ("completed", "skipped") for status in stages.values()), stages

    high_value = db.scalar(select(Chunk).where(Chunk.document_id == v4_doc, Chunk.section_number == "5.2"))
    assert "70%" in high_value.text and high_value.page_start == 1
    assert high_value.section_path == "5 Loan to Value > 5.2 LTV for High Value Loans"
    assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "document.ready")) == 2
    assert db.scalar(select(Organization.knowledge_version)) >= 2


def test_unchanged_text_is_never_embedded_twice(db, home_loan, embedder):
    _, v3_doc, v4_doc = home_loan
    v3_chunks = db.scalar(select(func.count()).select_from(Chunk).where(Chunk.document_id == v3_doc))
    v4_chunks = db.scalar(select(func.count()).select_from(Chunk).where(Chunk.document_id == v4_doc))
    # v4 repeats most of v3's sections verbatim; only new/changed text reached the provider.
    assert embedder.embedded < v3_chunks + v4_chunks
    assert db.scalar(select(func.count()).select_from(EmbeddingCache)) == embedder.embedded


def test_current_search_uses_the_version_in_force(admin, home_loan):
    result = search(admin, "What is the LTV for loans above 75 lakh?")
    top = result["passages"][0]
    assert "70%" in top["text"]
    assert top["source"]["version_label"] == "4"
    assert top["source"]["section_number"] == "5.2"
    assert top["source"]["policy_name"] == "Home Loan Credit Policy"
    # Superseded text is never returned for a "current" question.
    assert all(p["source"]["version_label"] == "4" for p in result["passages"])


def test_historical_search_uses_the_version_in_force_then(admin, home_loan):
    result = search(admin, "What is the LTV for loans above 75 lakh?", mode="as_of", as_of="2025-06-01")
    assert "75%" in result["passages"][0]["text"]
    assert {p["source"]["version_label"] for p in result["passages"]} == {"3"}


def test_all_versions_mode_and_specific_versions(admin, home_loan):
    labels = {p["source"]["version_label"] for p in search(admin, "LTV high value loans", mode="all")["passages"]}
    assert labels == {"3", "4"}
    policy = admin.get(f"/policies/{home_loan[0]}").json()["data"]
    v3_id = policy["versions"][0]["id"]
    only_v3 = search(admin, "LTV high value loans", mode="versions", version_ids=[v3_id])
    assert {p["source"]["version_label"] for p in only_v3["passages"]} == {"3"}


def test_clause_reference_hits_the_exact_section(admin, home_loan):
    result = search(admin, "what does clause 5.2 say")
    top = result["passages"][0]
    assert top["source"]["section_number"] == "5.2" and "exact" in top["lanes"]


def test_policy_lookup_by_name(admin, home_loan):
    result = search(admin, "home loan credit policy interest rate")
    assert result["policies"][0]["name"] == "Home Loan Credit Policy"
    assert result["passages"][0]["source"]["section_number"] == "6"


def test_search_results_are_cached_per_scope_and_invalidated_on_change(client, db, app, tenant, admin, home_loan):
    first = search(admin, "interest rate spread")
    assert first["cache_hit"] is False
    assert search(admin, "interest rate spread")["cache_hit"] is True
    # Another user (different permission scope) never gets the admin's cached result.
    assert search(login(client, tenant.user_a1), "interest rate spread")["cache_hit"] is False
    # Knowledge changes invalidate cached results.
    admin.post(f"/policies/{home_loan[0]}/versions/{admin.get(f'/policies/{home_loan[0]}').json()['data']['versions'][1]['id']}/withdraw",
               json={"reason": "withdrawn for test"})
    assert search(admin, "interest rate spread")["cache_hit"] is False


def test_withdrawn_versions_and_archived_documents_leave_search(admin, home_loan):
    policy_id, _, v4_doc = home_loan
    v4_id = admin.get(f"/policies/{policy_id}").json()["data"]["versions"][1]["id"]
    admin.post(f"/policies/{policy_id}/versions/{v4_id}/withdraw", json={"reason": "issued in error"})
    result = search(admin, "What is the LTV for loans above 75 lakh?")
    assert {p["source"]["version_label"] for p in result["passages"]} == {"3"}  # v3 is current again

    admin.post(f"/documents/{result['passages'][0]['source']['document_id']}/archive")
    assert search(admin, "What is the LTV for loans above 75 lakh?")["passages"] == []


def test_missing_embedding_provider_leaves_the_document_searchable_then_resumes(client, db, app, tenant, admin):
    """Embedding finishes after a document is searchable by keyword; its failure does not undo that."""
    from app.infrastructure.ai.embeddings.base import EmbeddingUnavailableError

    class Broken:
        model_id = "broken"

        def embed_documents(self, texts):
            raise EmbeddingUnavailableError("OPENAI_API_KEY is not configured.")

    runtime = get_runtime()
    runtime.__dict__.pop("embedder", None)
    runtime.overrides.pop("embedder", None)
    runtime.settings.EMBEDDING_PROVIDER = "openai"
    runtime.settings.OPENAI_API_KEY = None
    document_id, analysis = process(client, app, tenant.admin, build(V3))
    confirm_new_policy(admin, document_id, analysis)
    drain(app)
    document = db.get(Document, document_id)
    assert document.status == "ready"
    stage = stage_row(db, document_id, Stage.EMBEDDING)
    assert stage.status == "failed" and stage.detail["code"] == "EMBEDDINGS_UNAVAILABLE"
    assert "OPENAI_API_KEY" in stage.detail["error"]

    # The key is configured: the document, still without vectors, is found by keyword.
    runtime.overrides["embedder"] = LocalEmbedding(1536)
    assert search(admin, "home loan credit policy interest rate")["passages"]
    # The worker restarts: only the missing vectors are written.
    with app.state.session_factory() as session:
        assert [d.id for d in resume_ready_embeddings(session, runtime.queue, 256)] == [document_id]
    drain(app)
    db.expire_all()
    embedded, total = db.execute(
        select(func.count(Chunk.embedding), func.count()).where(Chunk.document_id == document_id)
    ).one()
    assert embedded == total > 0
    assert stage_row(db, document_id, Stage.EMBEDDING).status == "completed"
    assert stage_row(db, document_id, Stage.INDEXING).status == "completed"


def stage_row(db, document_id, stage):
    return db.scalar(select(IngestionStage).where(
        IngestionStage.document_id == document_id, IngestionStage.stage == stage))


def test_a_numbered_clause_inside_a_section_is_found_by_its_number(client, app, tenant, admin, embedder):
    """Manuals number clauses inside a section's text ("4.25.9 ..." under "Section 4.25")."""
    from app.tests.pdfs import PolicySpec, Section

    spec = PolicySpec(sections=[
        Section("Section 4.25", "Deposit Products and Pricing", [
            "4.25.8 The Bank shall maintain a liquidity buffer of not less than 12%.",
            "4.25.9 The Bank shall maintain a monitoring buffer of not less than 16%.",
            "4.25.19 The Bank shall maintain a reconciliation buffer of not less than 22%.",
        ]),
        Section("Section 4.26", "Collateral", ["4.26.1 Collateral shall be valued annually."]),
    ])
    document_id, analysis = process(client, app, tenant.admin, build(spec))
    confirm_new_policy(admin, document_id, analysis)
    drain(app)
    top = search(admin, "what does section 4.25.9 say")["passages"][0]
    assert "4.25.9 The Bank shall maintain a monitoring buffer" in top["text"] and "exact" in top["lanes"]


class Rewording:
    """Stands in for the model: rewords the question in the document's terms."""

    def __init__(self, queries):
        self.queries, self.calls = queries, 0

    def generate_json(self, system, user, schema, *, context=None):
        from app.infrastructure.ai.llm.base import LLMResult

        self.calls += 1
        return LLMResult(content={"queries": self.queries}, model="fake")


def test_a_reworded_question_finds_a_document_still_being_embedded(db, admin, home_loan):
    _policy_id, _v3_doc, v4_doc = home_loan
    llm = Rewording(["LTV high value loans above 75 lakh"])
    get_runtime().overrides["llm"] = llm
    get_runtime().__dict__.pop("llm", None)
    # The document is searchable, but its vectors have not been written yet.
    db.execute(update(Chunk).where(Chunk.document_id == v4_doc).values(embedding=None))
    db.execute(update(IngestionStage).where(IngestionStage.document_id == v4_doc,
                                            IngestionStage.stage == Stage.EMBEDDING).values(status="running"))
    db.commit()
    question = "ceiling on borrowing relative to property worth for big mortgages"

    def found(text):
        passages = search(admin, text, mode="all", document_ids=[str(v4_doc)])["passages"]
        return any("70%" in p["text"] for p in passages)

    llm.queries = []
    assert not found(question + "?")  # as asked, the keyword lanes miss it
    llm.queries = ["LTV high value loans above 75 lakh"]
    assert found(question)
    assert llm.calls == 2

    # Once the vectors are written, the question is searched as asked.
    db.execute(update(IngestionStage).where(IngestionStage.document_id == v4_doc,
                                            IngestionStage.stage == Stage.EMBEDDING).values(status="completed"))
    db.commit()
    search(admin, question + " please", mode="all", document_ids=[str(v4_doc)])
    assert llm.calls == 2
