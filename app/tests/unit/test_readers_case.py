"""A question about the reader's own case: "I am 27, earn Rs 65,000 and have a 760 score. Can I get a loan?"."""
import pytest

from app.modules.rag.evidence import key_terms, readers_situation
from app.modules.rag.schema import Claim
from app.modules.rag.service import _unchecked_figures
from app.modules.rag.validation import EvidenceText, validate_claims

QUESTION = "I am 27, earn Rs 65,000 and have a 760 score. Can I get a loan?"
RULES = EvidenceText("E1", (
    "Who Can Apply?\n\n• Applicant age: 28 to 65 years (70 for business owners)\n\n"
    "• Net monthly income of at least Rs. 60,000\n\n• Credit score of 720 or above preferred\n\n"
    "No foreclosure charge applies to individual borrowers on floating-rate loans."
), versions=frozenset({"8"}))


def _valid(text: str) -> bool:
    [result] = validate_claims([{"text": text, "evidence_ids": ["E1"]}], {"E1": RULES}, key_terms(QUESTION), QUESTION)
    return result.valid


def test_a_statement_about_the_reader_runs_on_until_the_question():
    situation, rest = readers_situation(QUESTION)
    assert situation == "I am 27 earn Rs 65,000 and have a 760 score" and rest == "Can I get a loan"
    assert "760" not in key_terms(QUESTION)


@pytest.mark.parametrize("claim", [
    "You are 27, which is below the minimum applicant age of 28.",
    "At 27 years, you do not meet the applicant age requirement of 28 to 65 years.",
    "Applicants must be between 28 and 65 years old; at 27 you do not qualify.",
])
def test_a_rule_the_reader_fails_is_kept(claim):
    # The reader's own figure, set against the rule: the "not" is their result, not a reversed rule.
    assert _valid(claim)


def test_dropping_the_rules_own_negation_is_still_caught():
    assert not _valid("A foreclosure charge applies to individual borrowers on floating-rate loans.")


def test_a_figure_the_answer_does_not_check_is_reported():
    checked = [Claim(text="Your net monthly income of Rs. 65,000 meets the minimum of Rs. 60,000.", citations=[1]),
               Claim(text="Your credit score of 760 is above the preferred 720.", citations=[1])]
    assert _unchecked_figures(QUESTION, checked) == ["27"]
    assert _unchecked_figures(QUESTION, checked + [
        Claim(text="At 27, you are below the minimum applicant age of 28 years.", citations=[1])]) == []


@pytest.mark.parametrize("question, claim", [
    # The question's own yes/no words ("meet", "criterion", "additional") are not asked of the passage.
    ("If my income is ₹49,000/month, do I meet the stated minimum-income criterion?",
     "No, your income of Rs. 49,000 does not meet the minimum income criterion of Rs. 60,000."),
    ("If I am self-employed, what additional financial documents are required",
     "As a self-employed applicant, you additionally need 3 years' audited financials and GST returns."),
    # The reader's figure said to be theirs, beside the rule's figure.
    ("If my income is ₹50,000 and my total EMIs are 60% of income, do I satisfy the EMI criterion?",
     "Your total EMIs are 60% of your income, which exceeds the 50% limit, so you do not satisfy the EMI criterion."),
])
def test_a_rule_applied_to_the_readers_case_keeps_the_questions_wording(question, claim):
    evidence = {"E1": EvidenceText("E1", RULES.text + "\n\n• Total EMIs must not exceed 50% of net income\n\n"
                                   "• Self-employed: 3 years' audited financials and GST returns", versions=frozenset({"8"}))}
    [result] = validate_claims([{"text": claim, "evidence_ids": ["E1"]}], evidence, key_terms(question), question)
    assert result.valid, result.problems


def test_eligibility_words_stay_terms_when_no_case_is_given():
    assert {"eligibility", "criteria"} <= set(key_terms("What are the eligibility criteria for a gold loan?"))


def test_arithmetic_on_the_readers_own_figures_is_shown_as_a_calculation():
    from app.modules.rag.calculate import checked_calculations

    question = "If my income is ₹50,000 and my total EMIs are 60% of income, do I satisfy the EMI criterion?"
    evidence = {"E1": EvidenceText("E1", "• Total EMIs must not exceed 50% of net income", versions=frozenset({"8"}))}
    calculations = checked_calculations([{"expression": "60 / 100 * 50000", "evidence_ids": ["E1"]}], evidence, question)
    [result] = validate_claims([{"text": "Your EMIs of Rs. 30,000 (60% of Rs. 50,000) exceed the 50% limit.",
                                 "evidence_ids": ["E1"]}], evidence, key_terms(question), question, calculations)
    assert result.valid, result.problems


@pytest.mark.parametrize("question, claim", [
    ("Is age 25 eligible", "No, age 25 is not eligible, as the applicant age must be 28 to 65 years."),
    ("Is age 24", "No, at 24 you are below the minimum applicant age of 28."),
    ("Is age 30 eligible?", "Yes, age 30 is eligible, as the applicant age is 28 to 65 years."),
])
def test_a_yes_no_question_tests_its_figure_against_the_rule(question, claim):
    assert "25" not in key_terms(question) and "eligible" not in key_terms(question)
    [result] = validate_claims([{"text": claim, "evidence_ids": ["E1"]}], {"E1": RULES}, key_terms(question), question)
    assert result.valid, result.problems


def test_a_yes_no_answer_that_never_applies_the_figure_is_reported():
    rule_only = [Claim(text="Applicant age must be 28 to 65 years (70 for business owners).", citations=[1])]
    assert _unchecked_figures("Is age 24", rule_only) == ["24"]
    assert _unchecked_figures("What is the applicant age?", rule_only) == []


def test_a_year_in_a_yes_no_question_still_names_a_subject():
    assert "2050" in key_terms("Is the demand in 2050 covered?")


PRICING = EvidenceText("E1", (
    "Pricing and Fee Schedule\n\nCredit score | Interest rate (p.a.) | Processing fee\n\n"
    "750 and above | 10.05% | 1.75% + GST\n\n700 - 749 | 10.45% | 1.75% + GST\n\n"
    "650 - 699 | 11.15% | 2.00% + GST\n\nBelow 650 | 11.95% | 2.00% + GST"
), versions=frozenset({"8"}))


@pytest.mark.parametrize("question, claim", [
    ("Is credit score 650 in the 650–699 bracket?",
     "Yes, a score of 650 falls in the 650 - 699 bracket, which carries an interest rate of 11.15%."),
    ("Which band does a score of 680 fall in?", "A score of 680 falls in the 650 - 699 band, at 11.15%."),
])
def test_a_figure_placed_in_a_band_is_answered_from_the_tables_rows(question, claim):
    # The table lists "650 - 699"; "bracket" and "band" are the reader's words for that row.
    assert not {"bracket", "band", "650", "680"} & set(key_terms(question))
    [result] = validate_claims([{"text": claim, "evidence_ids": ["E1"]}], {"E1": PRICING}, key_terms(question), question)
    assert result.valid, result.problems


FORM = ("Context:\n\nAge: 35\nIncome: ₹60,000\nCredit score: 750\nExisting EMI: ₹35,000\nVersion: 6\n\n"
        "Question:\n\nDo I satisfy the stated EMI-to-income condition?\n\nExpected:\nNo. 58.33%")


def test_a_filled_in_form_is_read_as_the_readers_case():
    from app.modules.rag.query_plan import plan_query
    from app.modules.rag.query_rewrite import from_form

    question = from_form(FORM)
    assert question == ("My age is 35, my income is ₹60,000, my credit score is 750 and my existing EMI is ₹35,000. "
                        "Do I satisfy the stated EMI-to-income condition in Version 6?")  # "Expected:" dropped
    assert plan_query(question).version_labels == ["6"]
    assert from_form("What is the LTV?\nWhat is the processing fee?") == "What is the LTV?\nWhat is the processing fee?"


def test_a_share_of_the_readers_income_is_checked_arithmetic():
    from app.modules.rag.query_rewrite import from_form

    question = from_form(FORM)
    evidence = {"E1": EvidenceText("E1", "• Total EMIs must not exceed 50% of net income", versions=frozenset({"6"}))}

    def valid(claim: str) -> bool:
        [result] = validate_claims([{"text": claim, "evidence_ids": ["E1"]}], evidence, key_terms(question), question)
        return result.valid

    assert valid("No, your EMIs of Rs. 35,000 are 58.33% of your Rs. 60,000 income, above the 50% maximum.")
    assert not valid("No, your EMIs are 62% of your income, above the 50% maximum.")
