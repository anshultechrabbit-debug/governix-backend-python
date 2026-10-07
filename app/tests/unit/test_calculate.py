"""Arithmetic an answer states is recomputed from figures the evidence and the question give."""
from decimal import Decimal

import pytest

from app.modules.rag.calculate import checked_calculations
from app.modules.rag.claim_stream import ClaimStream
from app.modules.rag.validation import EvidenceText, validate_claims

EMI_TABLE = EvidenceText("E3", (
    "Plan Your EMI\n\nUse the table below to estimate your monthly outgo at 10.05% per annum.\n\n"
    "A shorter tenure saves a lot of interest over the life of the loan, whereas a longer tenure keeps the "
    "monthly burden manageable.\n\n"
    "Loan (Rs. lakh) | 5 years | 10 years | 15 years | 15 years\n\n"
    "30 | Rs. 63,815 | Rs. 39,728 | Rs. 32,330 | Rs. 32,330\n\n"
    "50 | Rs. 106,358 | Rs. 66,214 | Rs. 53,883 | Rs. 53,883\n\n"
    "100 | Rs. 212,717 | Rs. 132,428 | Rs. 107,767 | Rs. 107,767"
), versions=frozenset({"8"}))
EVIDENCE = {"E3": EMI_TABLE}
TOTAL = "How much total interest on Rs 1 crore over 15 years?"
SAVING = "How much interest do I save with 5 years instead of 15 on Rs 50 lakh?"


def _calculate(expression: str, question: str):
    return checked_calculations([{"expression": expression, "evidence_ids": ["E3"]}], EVIDENCE, question)


def test_total_interest_is_recomputed_and_shown():
    [calculation] = _calculate("107767 * (15 * 12) - 10000000", TOTAL)
    assert calculation.result == Decimal("9398060")
    assert calculation.shown == "1,07,767 × (15 × 12) − 1,00,00,000 = 93,98,060"


def test_a_saving_between_two_tenures_is_recomputed():
    [calculation] = _calculate("(53883 * 15 * 12 - 5000000) - (106358 * 5 * 12 - 5000000)", SAVING)
    assert calculation.result == Decimal("3317460")


@pytest.mark.parametrize("expression", [
    "107767 * 180 - 10000000 + 5000",   # 5000 is in neither the evidence nor the question
    "__import__('os').system('ls')",    # only arithmetic
    "107767 ** 2",                      # powers are not one of + - * /
    "",
])
def test_anything_else_is_dropped(expression):
    assert _calculate(expression, TOTAL) == []


def test_a_claim_may_state_a_recomputed_result_rounded_but_not_another_figure():
    calculations = _calculate("107767 * (15 * 12) - 10000000", TOTAL)

    def valid(text: str) -> bool:
        [result] = validate_claims([{"text": text, "evidence_ids": ["E3"]}], EVIDENCE, ["total", "interest"], TOTAL,
                                   calculations)
        return result.valid

    assert valid("Total interest on Rs. 1 crore over 15 years is Rs. 93,98,060.")
    assert valid("Total interest on Rs. 1 crore over 15 years is about Rs. 93.98 lakh.")
    assert not valid("Total interest on Rs. 1 crore over 15 years is Rs. 85,00,000.")
    # Without the recomputed calculation, the figure is not in the evidence.
    [unchecked] = validate_claims([{"text": "Total interest on Rs. 1 crore over 15 years is Rs. 93,98,060.",
                                    "evidence_ids": ["E3"]}], EVIDENCE, ["total", "interest"], TOTAL)
    assert not unchecked.valid


def test_the_calculations_are_read_before_the_claims_stream():
    stream = ClaimStream()
    stream.feed('{"insufficient_evidence": false, "calculations": [{"expression": "107767 * 180 - 10000000", ')
    assert stream.preamble() == {}  # the claims have not begun
    stream.feed('"evidence_ids": ["E3"]}], "claims": [{"text": "Total interest is Rs. 93,98,060.", ')
    assert stream.preamble()["calculations"][0]["expression"] == "107767 * 180 - 10000000"
