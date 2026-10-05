"""Claims are released exactly when their JSON object is complete, however the text is split."""
import json

import pytest

from app.modules.rag.claim_stream import ClaimStream

ANSWER = {
    "claims": [
        {"text": 'KPTCL said "raise the margin {to +10%}" [sic]', "evidence_ids": ["E1"]},
        {"text": "A back\\slash and a } brace in text.", "evidence_ids": ["E2", "E3"]},
        {"text": "Third claim.", "evidence_ids": ["E4"]},
    ],
    "insufficient_evidence": False,
    "conflicts": [{"description": "not a claim {", "evidence_ids": ["E1"]}],
}
TEXT = json.dumps(ANSWER)


@pytest.mark.parametrize("size", [1, 2, 3, 7, 16, 64, len(TEXT)])
def test_claims_come_out_whole_for_any_chunking(size):
    stream = ClaimStream()
    released = []
    for start in range(0, len(TEXT), size):
        released += stream.feed(TEXT[start:start + size])
    assert released == ANSWER["claims"]  # conflicts are never mistaken for claims


def test_a_claim_is_released_as_soon_as_it_closes():
    stream = ClaimStream()
    first = json.dumps(ANSWER["claims"][0])
    first_end = TEXT.index(first) + len(first)  # the "}" inside the quoted text must not count
    assert stream.feed(TEXT[:first_end - 1]) == []
    assert stream.feed(TEXT[first_end - 1:first_end]) == [ANSWER["claims"][0]]


def test_no_claims():
    stream = ClaimStream()
    assert stream.feed(json.dumps({"claims": [], "insufficient_evidence": True, "conflicts": []})) == []


def test_a_declared_lack_of_evidence_is_seen_before_any_claim():
    # The schema puts insufficient_evidence first, so it is known before a claim could be shown.
    stream = ClaimStream()
    stream.feed('{"insufficient_evidence": true, "claims": [{"text": "Nearby fact.", "evid')
    assert stream.declared_insufficient
    sufficient = ClaimStream()
    sufficient.feed('{"insufficient_evidence": false, "claims": [')
    assert not sufficient.declared_insufficient
