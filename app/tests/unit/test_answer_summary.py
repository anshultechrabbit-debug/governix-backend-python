from app.modules.rag.service import RAGService
from app.modules.rag.validation import EvidenceText, validate_claims

EVIDENCE = {"E1": EvidenceText("E1", "This KYC Policy is issued as per RBI's Master Direction on Know Your Customer "
                                     "(updated up to 04.05.2023).")}


def claims():
    return validate_claims([{"text": "The KYC Policy is issued as per RBI's Master Direction on KYC.", "evidence_ids": ["E1"]}], EVIDENCE)


def test_a_supported_summary_is_kept():
    summary = "It is the Bank's Know Your Customer policy, issued under RBI's Master Direction."
    assert RAGService._summary(summary, claims(), EVIDENCE) == summary


def test_a_summary_with_an_unsupported_number_is_dropped():
    assert RAGService._summary("It is a KYC policy updated up to 04.05.2024.", claims(), EVIDENCE) is None


def test_an_empty_summary_is_none():
    assert RAGService._summary("", claims(), EVIDENCE) is None
