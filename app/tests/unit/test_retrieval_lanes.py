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
    # A code with several lettered parts is taken whole: "KEY-01" alone also names HLP-KEY-01.
    assert requested_codes("What does clause KAP-KEY-01 say in KYC/AML Policy v1.0?") == ["KAP-KEY-01"]
    assert requested_codes("Is HLP-4.2.1 still in force?") == ["HLP-4.2.1"]
    assert requested_codes("fixed-rate home loans and kyc/aml rules") == []


def test_a_named_code_must_appear_whole():
    from app.modules.search.retrieval import names_code

    assert names_code("KAP-KEY-01 All cash deposits ...", "KAP-KEY-01")
    assert names_code("see kap-key-01.", "KAP-KEY-01")
    assert not names_code("KAP-KEY-011 All cash deposits", "KAP-KEY-01")
    assert not names_code("HLP-KEY-01 The maximum LTV", "KAP-KEY-01")


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


def test_a_question_whose_words_occur_in_no_passage_searches_nothing(monkeypatch):
    """"Which edition requires earlier submission?" on documents without those words: no lane may fail."""
    from app.modules.search.retrieval import HybridRetriever, SearchFilters

    retriever = HybridRetriever(session_factory=None, embedder=None)
    monkeypatch.setattr(retriever, "selective_terms", lambda principal, terms: [])
    assert retriever._keyword(None, "Which edition requires earlier submission?", SearchFilters()) == []
    assert retriever._section(None, "Which edition requires earlier submission?", SearchFilters()) == []
