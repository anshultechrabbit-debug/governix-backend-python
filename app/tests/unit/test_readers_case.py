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
