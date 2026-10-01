import uuid

from app.modules.rag.schema import AnswerResponse, Claim, Source
from app.modules.rag.service import _combine


def answer(claims, sources, status="answered", summary=None):
    return AnswerResponse(
        question="q", status=status, answer=None, summary=summary, claims=claims, sources=sources, conflicts=[],
        warnings=[], plan={"explanation": "", "checks": []}, evidence_score=0.5, model="m", usage={"input_tokens": 10},
        timings_ms={},
    )


def source(number, chunk):
    return Source(number=number, evidence_id=f"E{number}", chunk_id=chunk, excerpt="x")


def test_parts_are_combined_with_one_set_of_source_numbers():
    shared, kyc, pets = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    first = answer([Claim(text="KYC records are kept.", citations=[1, 2])], [source(1, kyc), source(2, shared)], summary="KYC.")
    second = answer([Claim(text="Variants are listed.", citations=[1, 2])], [source(1, pets), source(2, shared)], summary="Pets.")
    combined = _combine([("KYC?", first), ("Pets?", second)])
    assert [s.chunk_id for s in combined.sources] == [kyc, shared, pets]
    assert [c.citations for c in combined.claims] == [[1, 2], [3, 2]]
    assert combined.summary == "KYC. Pets." and combined.usage["input_tokens"] == 20
    assert len({s.evidence_id for s in combined.sources}) == 3


def test_an_unanswered_part_is_named_not_filled_in():
    first = answer([Claim(text="KYC records are kept.", citations=[1])], [source(1, uuid.uuid4())], summary="KYC.")
    combined = _combine([("KYC?", first), ("Gold loans?", answer([], [], status="no_answer"))])
    assert combined.warnings == ['Not found in your documents: "Gold loans?"']
    assert combined.summary == "KYC." and len(combined.claims) == 1


def test_nothing_answered_is_none():
    assert _combine([("A?", answer([], [], status="no_answer"))]) is None
