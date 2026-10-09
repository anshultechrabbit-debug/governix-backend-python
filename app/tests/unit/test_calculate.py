"""Arithmetic an answer states is recomputed from figures the evidence and the question give."""
from decimal import Decimal

import pytest

from app.modules.rag.calculate import (
    asked_quantity, asks_for_calculation, checked_calculations, has_calculated_answer, restated_result, wrongly_stated,
)
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


CASE = ("A borrower has net monthly income Rs 1,20,000 and existing monthly obligations Rs 25,000. "
        "What is the maximum permissible EMI under the policy formula?")
CASE_EVIDENCE = {"E1": EvidenceText("E1", "Maximum permissible EMI = 50% of net monthly income minus "
                                  "existing monthly obligations.")}

# The actual retrieved Loan Quantum passage contains other limits and table rows as well as the formula.
QUANTUM = (
    "3 Loan Quantum\n\n3.1 Minimum loan amount: Rs 10 lakh. Maximum loan amount: Rs 5.0 crore per borrower.\n\n"
    "3.2 The maximum Loan-to-Value (LTV) ratio depends on the loan amount requested: up to Rs 30 lakh: 80%; "
    "above Rs 30 lakh and up to Rs 75 lakh: 75%; above Rs 75 lakh: 70%.\n\n"
    "Table 2: Maximum LTV by loan amount\n\nLoan amount requested | Maximum LTV\n\n"
    "Loan amount requested: Up to Rs 30 lakh | Maximum LTV: 80%\n\n"
    "Loan amount requested: Above Rs 30 lakh and up to Rs 75 lakh | Maximum LTV: 75%\n\n"
    "Loan amount requested: Above Rs 75 lakh | Maximum LTV: 70%\n\n"
    "3.3 The maximum Fixed Obligation to Income Ratio (FOIR) is 60% of net monthly income. "
    "Maximum permissible EMI = FOIR x net monthly income - existing monthly obligations."
)


@pytest.mark.parametrize("expression", ["0.6 * 120000 - 25000", "60 / 100 * 120000 - 25000"])
@pytest.mark.parametrize("claim", [
    "The maximum permissible EMI is Rs 47,000.",
    "Maximum permissible EMI: 60% × Rs 1,20,000 − Rs 25,000 = Rs 47,000.",
    "The maximum permissible EMI is Rs 47,000 (60% × Rs 1,20,000 − Rs 25,000).",
])
def test_retrieved_formula_accepts_correct_result_in_both_answer_checks(expression, claim):
    from app.modules.rag.evidence import key_terms

    evidence = {"E1": EvidenceText("E1", QUANTUM, versions=frozenset({"7.0"}))}
    content = {"calculations": [{"expression": expression, "evidence_ids": ["E1"]}],
               "claims": [{"text": claim, "evidence_ids": ["E1"]}],
               "summary": "The maximum permissible EMI is Rs 47,000."}
    assert has_calculated_answer(content, evidence, CASE)
    checked = checked_calculations(content["calculations"], evidence, CASE)
    [result] = validate_claims(content["claims"], evidence, key_terms(CASE), CASE, checked)
    assert result.valid, result.problems


@pytest.mark.parametrize("rate, amount", [(50, 35000), (60, 47000)])
@pytest.mark.parametrize("summary", ["correct", "wrong", "missing"])
def test_calculated_result_survives_final_answer_assembly(monkeypatch, rate, amount, summary):
    from datetime import date
    from types import SimpleNamespace
    from app.infrastructure.ai.llm.base import LLMResult
    from app.modules.rag import service as service_module
    from app.modules.rag.calculate import indian
    from app.modules.rag.query_plan import plan_query
    from app.modules.rag.schema import AskRequest, Source

    passage = QUANTUM.replace("60%", f"{rate}%")
    texts = {"E1": EvidenceText("E1", passage, versions=frozenset({"7.0"}))}
    source = SimpleNamespace(policy_name="Mortgage Loan Policy", document_title="Mortgage Loan Policy",
                             policy_id=None, version_id=None, version_label="7.0", effective_from=date(2024, 10, 1))
    item = SimpleNamespace(id="E1", source=source)
    evidence = SimpleNamespace(by_id=lambda: {"E1": item}, items=[item], comparison=None, injections=[],
                               conflicts=[], unrelated=set(), top_score=1.0)
    service = service_module.RAGService.__new__(service_module.RAGService)
    service.settings = SimpleNamespace(RAG_ANSWER_MODE="generate", LLM_PROVIDER="local")
    service._subjects = {}
    monkeypatch.setattr(service_module, "_evidence_texts", lambda _e: texts)
    monkeypatch.setattr(service, "_verify_citations", lambda _p, ids, _items: ids)
    monkeypatch.setattr(service, "_judge", lambda *_args: {})
    monkeypatch.setattr(service, "_off_topic", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "_close_in_meaning", lambda _items: True)
    monkeypatch.setattr(service, "_version_warnings", lambda *_args: [])
    monkeypatch.setattr(service, "_earlier_version_notes", lambda *_args: [])
    monkeypatch.setattr(service, "_source", lambda n, e, *_args: Source(number=n, evidence_id=e, excerpt=passage))
    result = indian(Decimal(amount))
    content = {"calculations": [{"expression": f"{rate / 100} * 120000 - 25000", "evidence_ids": ["E1"]}],
               "claims": [
                   {"text": f"The maximum FOIR is {rate}% of net monthly income.", "evidence_ids": ["E1"]},
                   {"text": f"Maximum permissible EMI: {rate}% × Rs 1,20,000 − Rs 25,000 = Rs {result}.",
                    "evidence_ids": ["E1"]}],
               "summary": (f"The maximum permissible EMI is Rs {result}." if summary == "correct" else
                           "The maximum permissible EMI is Rs 25,000." if summary == "wrong" else "")}
    request = AskRequest(question=CASE)
    response = service._validated_answer(None, request, plan_query(CASE), evidence,
                                         LLMResult(content=content, model="fake"))
    assert response.status == "answered"
    assert f"= Rs {result}" in response.claims[0].text
    assert response.summary is None or response.summary == f"The maximum permissible EMI is Rs {result}."


@pytest.mark.parametrize("question", [CASE, TOTAL, SAVING,
    "Income Rs 90,000, obligations Rs 11,000. Calculate the remaining allowance.",
])
def test_numeric_case_questions_require_a_computed_answer(question):
    assert asks_for_calculation(question)


@pytest.mark.parametrize("question", [
    "What is the maximum permissible EMI?", "Is age 25 eligible?", "What changed in Version 2?",
])
def test_policy_lookups_and_eligibility_are_not_forced_to_calculate(question):
    assert not asks_for_calculation(question)


@pytest.mark.parametrize("first, summary, accepted", [
    ("The maximum permissible EMI is Rs 35,000: 50% × Rs 1,20,000 − Rs 25,000 = Rs 35,000.",
     "The maximum permissible EMI is Rs 35,000.", True),
    ("The maximum permissible EMI is Rs 25,000.", "The maximum permissible EMI is Rs 25,000.", False),
    # Summary validation handles the bad summary separately; it must not hide the valid claim.
    ("The maximum permissible EMI is Rs 35,000.", "The maximum permissible EMI is Rs 25,000.", True),
    ("Maximum permissible EMI: 50% × Rs 1,20,000 − Rs 25,000 = Rs 35,000.", "", True),
    ("The cap is Rs 60,000.", "The maximum permissible EMI is Rs 35,000.", False),
])
def test_direct_answer_must_use_the_result_not_an_operand_or_intermediate(first, summary, accepted):
    content = {"calculations": [{"expression": "50 / 100 * 120000 - 25000", "evidence_ids": ["E1"]}],
               "claims": [{"text": first, "evidence_ids": ["E1"]}], "summary": summary}
    assert has_calculated_answer(content, CASE_EVIDENCE, CASE) is accepted


def test_result_claim_can_follow_the_rule_and_intermediate_cap():
    content = {"calculations": [{"expression": "0.5 * 120000 - 25000", "evidence_ids": ["E1"]}],
               "claims": [
                   {"text": "The total obligations cap is 50% of income.", "evidence_ids": ["E1"]},
                   {"text": "The cap is Rs 60,000.", "evidence_ids": ["E1"]},
                   {"text": "Maximum permissible EMI: 50% × Rs 1,20,000 − Rs 25,000 = Rs 35,000.",
                    "evidence_ids": ["E1"]},
               ], "summary": ""}
    assert has_calculated_answer(content, CASE_EVIDENCE, CASE)


def test_decimal_rate_is_grounded_only_when_the_percentage_is_stated():
    raw = [{"expression": "0.5 * 120000 - 25000", "evidence_ids": ["E1"]}]
    [calculation] = checked_calculations(raw, CASE_EVIDENCE, CASE)
    assert calculation.result == Decimal("35000")
    different_rate = {"E1": EvidenceText("E1", "Maximum obligations are 40% of income.")}
    assert checked_calculations(raw, different_rate, CASE) == []


@pytest.mark.parametrize("text", [
    "Maximum permissible EMI: 50% × Rs 1,20,000 − Rs 25,000 = Rs 25,000.",
    "Maximum permissible EMI: 50% × Rs 1,20,000 − Rs 35,000 = Rs 25,000.",
])
def test_wrong_working_is_not_accepted_just_because_it_mentions_the_right_result(text):
    content = {"calculations": [{"expression": "50 / 100 * 120000 - 25000", "evidence_ids": ["E1"]}],
               "claims": [{"text": text, "evidence_ids": ["E1"]}], "summary": ""}
    assert not has_calculated_answer(content, CASE_EVIDENCE, CASE)


def test_missing_or_invalid_final_calculation_cannot_be_rescued_by_an_input():
    content = {"claims": [{"text": "The maximum permissible EMI is Rs 25,000.", "evidence_ids": ["E1"]}],
               "summary": "The maximum permissible EMI is Rs 25,000."}
    assert not has_calculated_answer(content, CASE_EVIDENCE, CASE)
    content["calculations"] = [{"expression": "25000", "evidence_ids": ["E1"]}]
    assert not has_calculated_answer(content, CASE_EVIDENCE, CASE)
    content["calculations"] = [
        {"expression": "50 / 100 * 120000", "evidence_ids": ["E1"]},
        {"expression": "60000 - 26000", "evidence_ids": ["E1"]},
    ]
    content["claims"][0]["text"] = content["summary"] = "The maximum permissible EMI is Rs 60,000."
    assert not has_calculated_answer(content, CASE_EVIDENCE, CASE)


@pytest.mark.parametrize("corrected", [True, False])
def test_calculation_response_is_held_and_retried_only_once(corrected):
    from types import SimpleNamespace
    from app.infrastructure.ai.llm.base import LLMResult
    from app.modules.rag.service import RAGService, _Attempt, _NoAnswer

    calls = []

    def generate(*args, correction=False):
        calls.append(correction)
        yield {"text": "The maximum permissible EMI is Rs 25,000.", "citations": [1]}
        yield LLMResult(content={"corrected": correction and corrected}, model="fake")

    def validate(principal, request, plan, evidence, result):
        if not result.content["corrected"]:
            raise _NoAnswer("ANSWER_FAILED_VALIDATION")
        return "verified answer"

    service = SimpleNamespace(_generate_stream=generate, _validated_answer=validate)
    stream = RAGService._write(service, None, SimpleNamespace(question=CASE), None, None, _Attempt(), {}, 0)
    if corrected:
        with pytest.raises(StopIteration) as done:
            next(stream)  # no provisional answer is yielded
        assert done.value.value == "verified answer"
    else:
        with pytest.raises(_NoAnswer):
            next(stream)
    assert calls == [False, True]


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


def test_later_calculation_can_use_a_verified_result_with_its_evidence():
    evidence = {"E1": EvidenceText("E1", "Maximum monthly obligations are 40% of net income.")}
    question = "A borrower has income Rs 90,000 and obligations Rs 11,000. What is the remaining allowance?"
    calculations = checked_calculations([
        {"expression": "40 / 100 * 90000", "evidence_ids": ["E1"]},
        {"expression": "36000 - 11000", "evidence_ids": ["E1"]},
    ], evidence, question)
    assert [c.result for c in calculations] == [Decimal("36000"), Decimal("25000")]


def test_chained_calculation_cannot_borrow_an_unrelated_or_unverified_result():
    evidence = {"E1": EvidenceText("E1", "The cap is 40% of income."),
                "E2": EvidenceText("E2", "A separate policy applies.")}
    question = "Income Rs 90,000; obligations Rs 11,000."
    calculations = checked_calculations([
        {"expression": "40 / 100 * 90000", "evidence_ids": ["E1"]},
        {"expression": "36000 - 11000", "evidence_ids": ["E2"]},
        {"expression": "37000 - 11000", "evidence_ids": ["E1"]},
    ], evidence, question)
    assert [c.result for c in calculations] == [Decimal("36000")]


@pytest.mark.parametrize("claim, right", [
    ("No, ₹35,000 / ₹60,000 × 100 = 58.33%, which exceeds 50%.", True),
    ("Rs. 35,000 / Rs. 60,000 = 58.33%", True),
    ("35,000 / 60,000 x 100 = 70%", False),          # the stated result is wrong
])
def test_arithmetic_a_claim_writes_out_is_recomputed(claim, right):
    from app.modules.rag.calculate import figures_in, written_arithmetic

    grounded = figures_in("my income is ₹60,000 and my existing EMI is ₹35,000")
    assert (Decimal("58.33") in written_arithmetic(claim, grounded)) is right


ELIGIBILITY = EvidenceText("E2", "• Net monthly income of at least Rs. 60,000\n\n• Total EMIs must not exceed 50% of net "
                                 "income\n\n• We fund up to 65% of the agreed property value", versions=frozenset({"8"}))
FAMILY = {"E3": EMI_TABLE, "E2": ELIGIBILITY}


@pytest.mark.parametrize("question, evidence_id, claim", [
    # The "how much" family, worked as people write it: currency marks, units, lakh/crore, words for operators.
    (TOTAL, "E3", "Total interest on Rs. 1 crore over 15 years: Rs. 1,07,767 × 180 months − Rs. 1 crore = Rs. 93,98,060."),
    (TOTAL, "E3", "You would pay Rs. 1,07,767 x 15 x 12 - Rs. 1,00,00,000 = about Rs. 93.98 lakh in interest."),
    (TOTAL, "E3", "EMIs of Rs. 1,07,767 over 180 months come to Rs. 1,93,98,060."),
    (SAVING, "E3", "You save (Rs. 53,883 × 180) − (Rs. 1,06,358 × 60) = Rs. 33,17,460 in interest by choosing 5 years."),
    (SAVING, "E3", "Interest over 15 years: Rs. 53,883 × 180 months − Rs. 50 lakh = Rs. 46,98,940."),
    ("My income is Rs 60,000 and my existing EMI is Rs 10,000. How much more EMI can I take?", "E2",
     "You can take up to 50% × Rs. 60,000 − Rs. 10,000 = Rs. 20,000 more in EMIs."),
    ("How much can I borrow on a property worth Rs 80 lakh?", "E2",
     "You can borrow up to 65% of Rs. 80 lakh, which is Rs. 52 lakh."),
])
def test_how_much_arithmetic_written_in_a_claim_is_checked_and_kept(question, evidence_id, claim):
    from app.modules.rag.evidence import key_terms

    [result] = validate_claims([{"text": claim, "evidence_ids": [evidence_id]}], FAMILY, key_terms(question), question)
    assert result.valid, result.problems


@pytest.mark.parametrize("claim", [
    "Total interest is Rs. 1,07,767 × 180 − Rs. 1 crore = Rs. 85,00,000.",      # wrong result
    "Total interest is Rs. 1,07,767 × 200 − Rs. 1 crore = Rs. 1,15,53,400.",    # 200 is in neither source
])
def test_wrong_or_ungrounded_arithmetic_is_removed_and_never_judged_by_meaning(claim):
    from app.modules.rag.evidence import key_terms

    [result] = validate_claims([{"text": claim, "evidence_ids": ["E3"]}], FAMILY, key_terms(TOTAL), TOTAL)
    assert not result.valid and not result.wording_only


FOIR = {"E1": EvidenceText("E1", "3.3 The maximum Fixed Obligation to Income Ratio (FOIR) is 60% of net monthly "
                                 "income. Maximum permissible EMI = FOIR x net monthly income - existing monthly "
                                 "obligations.")}
FOIR_QUESTION = ("A borrower has net monthly income of Rs 200,000 and existing monthly obligations of Rs 65,000. "
                 "Using the policy FOIR cap, what is the maximum permissible EMI?")


@pytest.mark.parametrize("claim, stated", [
    # The result after its working, in words: the calculation is the field's.
    ("After subtracting existing obligations of Rs 65,000 from 60% of Rs 2,00,000, the maximum permissible EMI "
     "is Rs 55,000.", True),
    ("60% of the Rs 2,00,000 income is Rs 1,20,000; less Rs 65,000 of obligations, the maximum EMI is Rs 55,000.", True),
    # An operand or an intermediate step is never the result, nor is a figure the recomputation does not give.
    ("Your existing obligations of Rs 65,000 are deducted from 60% of your income.", False),
    ("The FOIR cap of 60% on Rs 2,00,000 allows Rs 1,20,000 of EMIs.", False),
    ("After subtracting Rs 65,000, the maximum permissible EMI is Rs 56,000.", False),
])
def test_a_result_stated_after_its_working_counts_but_an_operand_never_does(claim, stated):
    content = {"calculations": [{"expression": "0.6 * 200000 - 65000", "evidence_ids": ["E1"]}],
               "claims": [{"text": claim, "evidence_ids": ["E1"]}]}
    assert has_calculated_answer(content, FOIR, FOIR_QUESTION) is stated


@pytest.mark.parametrize("expression", [
    "0.60 * 200000 - 65000 = 55000",  # the result written into the expression
    "0.6 * 200000 = 120000 - 65000 = 55000",  # a chain of steps
    "60% x 2,00,000 - 65,000",  # a percentage sign, "x" and Indian grouping
])
def test_an_expression_written_with_its_result_is_recomputed(expression):
    content = {"calculations": [{"expression": expression, "evidence_ids": ["E1"]}],
               "claims": [{"text": "The maximum permissible EMI is Rs. 55,000.", "evidence_ids": ["E1"]}]}
    assert has_calculated_answer(content, FOIR, FOIR_QUESTION)


def test_a_wrong_result_written_into_an_expression_is_reported_with_the_right_one():
    raw = [{"expression": "0.60 × 200000 - 65000 = 65000", "evidence_ids": ["E1"]}]
    assert not checked_calculations(raw, FOIR, FOIR_QUESTION)
    [(shown, stated)] = wrongly_stated(raw, FOIR, FOIR_QUESTION)
    assert shown.endswith("= 55,000") and stated == Decimal("65000")


@pytest.mark.parametrize("claims", [
    [],  # the result never stated
    ["The maximum permissible EMI is Rs 35,000: 60% × Rs 200,000 − Rs 65,000 = Rs 35,000."],  # arithmetic wrong
    ["The maximum permissible EMI is Rs. 65,000."],  # an input given as the result
])
def test_a_valid_calculation_misstated_by_the_model_is_stated_from_its_recomputation(claims):
    content = {"calculations": [{"expression": "0.60 * 200000 - 65000", "evidence_ids": ["E1"]}],
               "claims": [{"text": c, "evidence_ids": ["E1"]} for c in
                          [*claims, "The maximum FOIR is 60% of net monthly income."]]}
    restated, final = restated_result(content, FOIR, FOIR_QUESTION)
    texts = [c["text"] for c in restated["claims"]]
    assert texts[0] == "The maximum permissible EMI is Rs 55,000: 0.60 × 2,00,000 − 65,000 = 55,000."
    assert not any("35,000" in t or "is Rs. 65,000" in t for t in texts)
    assert final.result == Decimal("55000")


def test_no_result_is_stated_for_an_ungrounded_calculation_or_a_non_money_quantity():
    ungrounded = {"calculations": [{"expression": "0.65 * 200000 - 65000", "evidence_ids": ["E1"]}], "claims": []}
    assert restated_result(ungrounded, FOIR, FOIR_QUESTION) is None
    assert asked_quantity("What is the remaining tenure if I prepay Rs 5 lakh?") == "remaining tenure"
    tenure = {"calculations": [{"expression": "0.60 * 200000 - 65000", "evidence_ids": ["E1"]}], "claims": []}
    assert restated_result(tenure, FOIR, "What is the remaining tenure for Rs 200,000 less Rs 65,000?") is None
