"""Pure parts of retrieval: page/identifier parsing, routed-vector document choice, merging."""
import uuid

from app.modules.search.retrieval import (
    Candidate,
    SearchFilters,
    merge_by_similarity,
    requested_codes,
    requested_pages,
    routing_documents,
)


def cand(document_id=None, chunk_id=None):
    return Candidate(chunk_id or uuid.uuid4(), document_id or uuid.uuid4(), None, None, None, 0, "", "", None, 1, 1)


def test_page_references():
    assert requested_pages("What does page 5000 contain?") == [5000]
    assert requested_pages("pages 12-14 and p. 7") == [12, 13, 14, 7]
    assert requested_pages("pages 1-900") == [1]  # an unbounded range is not a lookup
    assert requested_pages("What is the plan for 2026-27?") == []


def test_identifier_references():
    assert requested_codes("Find policy BNK-100") == ["BNK-100"]
    assert requested_codes("RBI/2025-26/12 circular") == ["RBI/2025-26/12"]
    assert requested_codes("section 12 of BNK-111 v2.0") == ["BNK-111"]  # version labels go to the planner
    assert requested_codes("time-of-day tariffs for 2026-27") == []


def test_routed_documents_follow_the_first_pass_and_skip_scoped_searches():
    a, b = uuid.uuid4(), uuid.uuid4()
    lanes = {"keyword": [(cand(a), 1.0), (cand(b), 0.5)], "vector": [(cand(b), 0.9)], "exact": [], "section": []}
    assert routing_documents(lanes, SearchFilters())[0] == b  # two lanes agree on b
    assert routing_documents(lanes, SearchFilters(document_ids=[a])) == []
    assert routing_documents(lanes, SearchFilters(policy_ids=[uuid.uuid4()])) == []


def test_routed_results_merge_into_one_vector_ranking():
    shared = uuid.uuid4()
    first = [(cand(chunk_id=shared), 0.70)]
    second = [(cand(chunk_id=shared), 0.72), (cand(), 0.80)]
    merged = merge_by_similarity(first, second, limit=10)
    assert [round(score, 2) for _, score in merged] == [0.80, 0.72]  # one entry per chunk, best similarity
