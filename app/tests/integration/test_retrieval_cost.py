"""Retrieval cost: bounded round trips, batched lanes, a shared pool.

These exist because the retrieval path degraded silently before. A thread pool
per `retrieve()` call, an N+1 in context expansion, and one query per referenced
clause all pass every correctness test while making latency a function of
corpus size. None of it fails a functional assertion, so it is asserted here.
"""

import hashlib
import threading
import time
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, insert, select, text

from app.modules.auth.permissions import Principal, Role
from app.modules.search.model import Chunk
from app.modules.search.retrieval import (
    HybridRetriever,
    SearchFilters,
    expand_context,
    lane_pool,
    shutdown_lane_pool,
)
from app.tests.factories import login, make_tenant
from app.tests.flows import V3, V4, build, confirm_new_policy, confirm_new_version, process
from app.tests.pipeline import drain
from app.workers.runtime import get_runtime

pytestmark = pytest.mark.integration


@pytest.fixture
def alembic(test_database_url):
    """Bring the test database to head so new indexes are actually present.

    The session-scoped fixture in conftest runs `upgrade head` once per session,
    but this file may run after another test already left the database at an
    earlier revision, so the index under test has to be asserted rather than
    assumed.
    """
    from alembic import command

    from app.tests.conftest import alembic_config

    config = alembic_config(test_database_url)
    command.upgrade(config, "head")
    return config


@pytest.fixture
def indexed(client, app, db, alembic):
    """A tenant with one policy in two versions, fully chunked and embedded."""
    tenant = make_tenant(db)
    admin = login(client, tenant.admin)
    v3_doc, analysis = process(client, app, tenant.admin, build(V3))
    policy_id = confirm_new_policy(admin, v3_doc, analysis).json()["data"]["policy_id"]
    drain(app)
    v4_doc, analysis = process(client, app, tenant.admin, build(V4))
    confirm_new_version(admin, v4_doc, analysis, policy_id)
    drain(app)
    return tenant


def principal_of(tenant, user) -> Principal:
    return Principal(
        user_id=user.id,
        role=Role(user.role),
        organization_id=tenant.org.id,
        branch_id=user.branch_id,
        department_id=user.department_id,
    )


@pytest.fixture
def counting_factory(app, monkeypatch):
    """A session factory that tallies application statements, not SET LOCAL."""
    real = app.state.session_factory
    counter = {"n": 0}

    class CountingSession:
        def __init__(self, session):
            self._session = session

        def begin(self):
            self._session.begin()
            return self

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._session.close()
            return False

        def execute(self, statement, *args, **kwargs):
            if not str(statement).upper().lstrip().startswith(("SET ", "SHOW ")):
                counter["n"] += 1
            return self._session.execute(statement, *args, **kwargs)

        def scalars(self, *args, **kwargs):
            return self._session.scalars(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._session, name)

    def factory():
        return CountingSession(real())

    monkeypatch.setattr(app.state, "session_factory", factory)
    return counter


def test_a_question_issues_a_bounded_number_of_queries(indexed, app, counting_factory, alembic):
    """Retrieval latency is a function of round trips, so the count is the contract.

    Before batching this was roughly 45-60 round trips per question. The
    migration is applied here because the regex lanes are only index-backed, and
    therefore only fast, once `ix_chunks_text_trgm` exists.
    """
    principal = principal_of(indexed, indexed.admin)
    retriever = HybridRetriever(app.state.session_factory, get_runtime().embedder)
    result = retriever.retrieve(principal, "loan interest rate for a home loan", SearchFilters())

    assert result.candidates, "expected candidates for a question the corpus answers"
    assert counting_factory["n"] <= 12, f"too many round trips: {counting_factory['n']}"


def test_a_multi_clause_question_costs_one_statement(indexed, app, counting_factory):
    """Five referenced clauses must not cost five sequential round trips."""
    principal = principal_of(indexed, indexed.admin)
    retriever = HybridRetriever(app.state.session_factory, get_runtime().embedder)

    retriever._exact_clauses(principal, SearchFilters(), [], ["5.1", "5.2", "5.3", "5.4", "5.5"], 1.5)

    assert counting_factory["n"] == 1, f"clauses should be batched, ran {counting_factory['n']} queries"


def test_a_multi_division_question_costs_two_statements(indexed, app, counting_factory):
    """Divisions batch into one heading query and one body-text query."""
    principal = principal_of(indexed, indexed.admin)
    retriever = HybridRetriever(app.state.session_factory, get_runtime().embedder)

    retriever._exact_divisions(principal, SearchFilters(), [("chapter", "5"), ("annexure", "II")])

    assert counting_factory["n"] == 2, f"divisions should batch to 2, ran {counting_factory['n']}"


def test_concurrent_retrieval_reuses_one_pool(app):
    """One shared executor, not one per request: threads must not scale with load."""
    shared = lane_pool()
    observed = set()

    def grab():
        observed.add(id(lane_pool()))

    threads = [threading.Thread(target=grab) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert observed == {id(shared)}, "concurrent callers must share the same pool"


def test_the_pool_is_recreated_after_shutdown():
    first = lane_pool()
    assert lane_pool() is first, "the pool must be reused, not rebuilt per request"
    shutdown_lane_pool()
    second = lane_pool()
    assert second is not first, "a shut-down pool must be replaced, not reused"


def test_context_expansion_is_one_query_regardless_of_candidate_count(indexed, db):
    """The N+1: an 8-item evidence set used to cost 8 round trips here."""
    counter = {"n": 0}

    class CountingSession:
        def __init__(self, session):
            self._session = session

        def execute(self, statement, *args, **kwargs):
            counter["n"] += 1
            return self._session.execute(statement, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._session, name)

    with db.begin():
        rows = db.execute(
            select(Chunk.id, Chunk.section_id, Chunk.chunk_index, Chunk.text, Chunk.document_id)
            .where(Chunk.section_id.is_not(None)).limit(8)
        ).all()
    if len(rows) < 2:
        pytest.skip("corpus has too few sectioned chunks to exercise expansion")

    from app.modules.search.retrieval import Candidate

    candidates = [
        Candidate(chunk_id=r.id, document_id=r.document_id, policy_id=None, version_id=None,
                  section_id=r.section_id, chunk_index=r.chunk_index, text=r.text,
                  section_path="", section_number=None, page_start=1, page_end=1)
        for r in rows
    ]
    with db.begin_nested():
        result = expand_context(CountingSession(db), candidates)

    assert counter["n"] == 1, f"expansion should be one query, ran {counter['n']}"
    # Correctness must survive the rewrite: every anchor keeps its own neighbours.
    anchor_ids = {c.chunk_id for c in candidates}
    assert set(result) <= anchor_ids
    for chunk_id, neighbours in result.items():
        anchor = next(c for c in candidates if c.chunk_id == chunk_id)
        for neighbour in neighbours:
            assert neighbour.chunk_id != anchor.chunk_id
            assert abs(neighbour.chunk_index - anchor.chunk_index) <= 1
            assert neighbour.section_id == anchor.section_id
        assert [n.chunk_index for n in neighbours] == sorted(n.chunk_index for n in neighbours)


def test_the_regex_lanes_have_a_trigram_index(app):
    """The exact and glossary lanes match chunks.text by regex; a seq scan there
    would dominate the request path as the corpus grows."""
    with app.state.engine.connect() as conn:
        indexes = set(conn.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = 'chunks'")
        ).scalars())
    assert "ix_chunks_text_trgm" in indexes


@pytest.mark.parametrize("pattern", [r"\mBESS\s", r"\mHVDC\s", "National Electricity Plan"])
def test_the_regex_predicate_is_served_by_the_trigram_index(indexed, app, db, pattern):
    """The regex lanes must be index-backed, not a scan of the whole table.

    Measured on the predicate itself, which is what the index makes indexable.
    Asserting the full lane query would instead be asserting one particular plan
    for one particular join shape, which is the planner's decision, not this
    codebase's -- and it changes with statistics and table shape.
    """
    _bulk_chunks(db, rows=20_000)
    db.commit()
    # The planner's choice between a scan and the trigram index depends on column
    # statistics; without ANALYZE it is guessing, and the test would measure the
    # guess rather than the index.
    db.execute(text("ANALYZE chunks"))

    with app.state.engine.connect() as conn:
        rows = conn.execute(
            text(f"EXPLAIN (ANALYZE, FORMAT TEXT) SELECT id FROM chunks WHERE text ~ '{pattern}'")
        ).scalars()
        plan = "\n".join(str(row) for row in rows)

    assert "ix_chunks_text_trgm" in plan, f"planner did not choose the trigram index:\n{plan}"
    assert "Seq Scan on chunks" not in plan, f"chunks was still scanned sequentially:\n{plan}"


def _bulk_chunks(db, rows: int, documents: int = 400) -> None:
    """Grow the tenant's corpus so table size is realistic.

    The filler is spread over many documents on purpose. If every filler row
    shared one document_id, the planner would satisfy the join through
    ix_chunks_document_index and never need the trigram index, which would make
    the test pass for the wrong reason. Spreading them reproduces the real
    shape: a large tenant where the ACL predicate and the document join are both
    broad, leaving the regex as the only selective condition.
    """
    from app.modules.documents.model import Document, DocumentStatus
    from app.modules.search.model import Chunk as ChunkModel

    template = db.scalars(select(ChunkModel).limit(1)).first()
    assert template is not None, "index the corpus before growing it"

    existing = db.scalar(select(func.count()).select_from(ChunkModel)) or 0
    needed = max(0, rows - existing)
    if not needed:
        return

    real_documents = [d.id for d in db.scalars(select(Document).limit(documents))]
    while len(real_documents) < documents:
        new_id = uuid.uuid4()
        db.execute(
            insert(Document).values(
                id=new_id,
                organization_id=template.organization_id,
                branch_id=template.branch_id,
                department_id=template.department_id,
                policy_id=template.policy_id,
                policy_version_id=template.version_id,
                category_id=template.category_id,
                title=f"Filler document {len(real_documents)}",
                original_filename=f"filler-{len(real_documents)}.pdf",
                storage_key=f"filler/{len(real_documents)}.pdf",
                content_type="application/pdf",
                size_bytes=1024,
                file_sha256=hashlib.sha256(str(new_id).encode()).hexdigest(),
                page_count=1,
                status=DocumentStatus.READY,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        real_documents.append(new_id)

    now = datetime.now(UTC)
    payload = [{
        "id": uuid.uuid4(),
        "organization_id": template.organization_id,
        "branch_id": template.branch_id,
        "department_id": template.department_id,
        # Every filler row also points at a real policy/version so it passes the
        # same scope predicate as real content and competes for the same plans.
        "document_id": real_documents[index % len(real_documents)],
        "policy_id": template.policy_id,
        "version_id": template.version_id,
        "category_id": template.category_id,
        "section_id": template.section_id,
        "chunk_index": index,
        "section_number": None,
        "section_path": "Filler",
        "text": _filler_text(index),
        "page_start": 1,
        "page_end": 1,
        "char_start": 0,
        "token_count": 12,
        "chunk_hash": hashlib.sha256(str(index).encode()).hexdigest(),
        "created_at": now,
    } for index in range(needed)]
    db.execute(insert(ChunkModel), payload)


# Prose-like vocabulary so the filler has a realistic trigram distribution.
# Uniform filler text ("Filler passage N ...") would give the planner no
# selectivity signal for any pattern, and it would then correctly prefer a
# sequential scan -- which would make the index test pass or fail for the wrong
# reason.
_FILLER_WORDS = (
    "transmission planning capacity storage renewable grid tariff loan interest margin "
    "scenario demand generation evacuation corridor scheme approval committee review "
    "schedule chapter contents substation voltage reliability maintenance inspection "
    "contract procurement tender payment settlement reconciliation ledger audit"
).split()
# One row in 50 carries an acronym, mirroring a real glossary's density.
_FILLER_ACRONYMS = ("BESS", "HVDC", "ISTS", "SVC", "FACTS", "CTU", "PMU", "GEC")


def _filler_text(index: int) -> str:
    words = [_FILLER_WORDS[(index * 7 + offset * 13) % len(_FILLER_WORDS)] for offset in range(30)]
    text = " ".join(words)
    if index % 50 == 0:
        text = f"{_FILLER_ACRONYMS[index // 50 % len(_FILLER_ACRONYMS)]} - {text}"
    return text


def test_a_timed_out_lane_degrades_instead_of_raising(indexed, app, monkeypatch):
    """A statement timeout is raised by the database, not by the lane method.

    The guard lives in `_read`, so the timeout has to be provoked at the session
    level to prove the question still answers from the surviving lanes.
    """
    from sqlalchemy.exc import OperationalError

    principal = principal_of(indexed, indexed.admin)
    real_factory = app.state.session_factory

    def flaky_factory():
        session = real_factory()
        original = session.execute

        def execute(statement, *args, **kwargs):
            if "ts_rank_cd" in str(statement):
                raise OperationalError(
                    "SELECT 1", {}, Exception("canceling statement due to statement timeout")
                )
            return original(statement, *args, **kwargs)

        session.execute = execute
        return session

    retriever = HybridRetriever(flaky_factory, get_runtime().embedder)
    result = retriever.retrieve(principal, "loan interest rate for a home loan", SearchFilters())

    assert result.candidates, "the surviving lanes must still produce an answer"
    assert result.timings_ms["keyword"] >= 0


def test_repeated_questions_do_not_leak_threads(indexed, app):
    """Thread count must be flat across many questions, not growing per request."""
    principal = principal_of(indexed, indexed.admin)
    retriever = HybridRetriever(app.state.session_factory, get_runtime().embedder)
    before = threading.active_count()
    for _ in range(20):
        retriever.retrieve(principal, "home loan interest rate", SearchFilters())
    time.sleep(0.1)
    assert threading.active_count() <= before + 8, "threads accumulated across requests"
