from types import SimpleNamespace

from app.infrastructure.ai.llm.base import LLMResult, LLMUnavailableError
from app.modules.rag.schema import Claim
from app.modules.rag.service import RAGService
from app.modules.rag.validation import EvidenceText, validate_claims

QUESTION = "Under what is the KYC Policy issued?"
EVIDENCE = {"E1": EvidenceText("E1", "This KYC Policy is issued as per RBI's Master Direction on Know Your Customer "
                                     "(updated up to 04.05.2023).")}
CLAIMS = [Claim(text="The KYC Policy is issued as per RBI's Master Direction on KYC.", citations=[1])]


class _Checker:
    def __init__(self, verdict=None, error=False):
        self.verdict, self.error, self.calls = verdict, error, 0

    def generate_json(self, *_args, **_kwargs):
        self.calls += 1
        if self.error:
            raise LLMUnavailableError("down")
        return LLMResult(content=self.verdict, model="test")


def summary(text, checker):
    service = SimpleNamespace(_llm_factory=lambda: checker)
    valid = validate_claims([{"text": CLAIMS[0].text, "evidence_ids": ["E1"]}], EVIDENCE)
    return RAGService._summary(service, QUESTION, text, valid, EVIDENCE, CLAIMS)


def test_a_summary_the_checker_supports_is_kept():
    text = "It is the Bank's Know Your Customer policy, issued under RBI's Master Direction."
    assert summary(text, _Checker({"supported": True, "unsupported": []})) == text


def test_a_summary_the_checker_rejects_is_dropped():
    text = "It is the KYC policy, issued under RBI's Master Direction, which is the agency that analyses suspicious activity."
    checker = _Checker({"supported": False, "unsupported": ["the agency that analyses suspicious activity"]})
    assert summary(text, checker) is None


def test_a_summary_with_an_unsupported_number_is_dropped_before_the_check():
    checker = _Checker({"supported": True, "unsupported": []})
    assert summary("It is a KYC policy updated up to 04.05.2024.", checker) is None
    assert checker.calls == 0


def test_no_checker_means_no_summary():
    assert summary("It is the KYC policy issued under RBI's Master Direction.", _Checker(error=True)) is None
    assert summary("It is the KYC policy issued under RBI's Master Direction.", _Checker({})) is None


def test_an_empty_summary_is_none():
    assert summary("", _Checker({"supported": True, "unsupported": []})) is None
