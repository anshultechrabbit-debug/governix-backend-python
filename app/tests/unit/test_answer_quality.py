"""Regressions found by the RAG evaluation: wording, comparisons, ambiguity and versions."""
import uuid
from datetime import date
from types import SimpleNamespace

import pytest

from app.modules.rag.evidence import EvidenceSet, coverage_of, key_terms
from app.modules.rag.query_plan import QueryClass, plan_query
from app.modules.rag.query_rewrite import refers_to_earlier_turn
from app.modules.rag.schema import Claim
from app.modules.rag.service import (
    _NoAnswer, _magnitude_comparison, _refuse_if_ambiguous, _with_versions, _without_repeats,
)
from app.modules.rag.validation import TermIndex, validate_claims, EvidenceText


@pytest.mark.parametrize("question, evidence", [
    ("Who owns Version 2.0 of the manual?", "Policy owner of the manual: Legal Department"),
    ("Who is the policy custodian of the manual?", "Policy owner: Legal Department of the manual"),
    ("How long must the application file be preserved?", "The application file shall be retained for 12 years."),
    ("What benchmark value applies to approval?", "'Approval' is measured against threshold 48.3."),
    ("What is the maximum penal charge on approval?", "Penal charges on approval shall not exceed 4.3%."),
    ("What is the measurement frequency for escalation?", "Escalation is measured weekly against 75.6."),
])
def test_a_policy_word_for_what_the_question_asks_counts_as_present(question, evidence):
    assert coverage_of(question, [evidence])[1] == []


def test_a_shared_short_prefix_is_not_the_same_word():
    # "custo": a customer is not a custodian, while a real word form still matches.
    assert not TermIndex("The Customer Service Head").mentions("custodian")
    assert TermIndex("The estimate of the scheme").mentions("estimated")


def test_a_comparison_does_not_require_the_word_change():
    question = "How did the approval cycle change between Version 1.0 and Version 2.0?"
    assert plan_query(question).query_class is QueryClass.COMPARISON
    assert key_terms(question) == ["approval", "cycle"]


@pytest.mark.parametrize("question, follow_up", [
    ("What about Version 2.0?", True),
    ("And for v1.1?", True),
    ("Confirm that clause 1.1.8 of Version 2.0 says 10 years.", False),
    ("What does that clause say?", True),
])
def test_follow_ups_with_version_numbers_and_named_clauses(question, follow_up):
    assert refers_to_earlier_turn(question) is follow_up


def test_each_part_of_a_split_question_keeps_the_version_it_names():
    parts = _with_versions("Under Version 1.0, what does clause 1.1.6 say and how long is X kept?",
                           ["What does clause 1.1.6 say?", "How long is X kept in v2?"])
    assert parts == ["In Version 1.0: What does clause 1.1.6 say?", "How long is X kept in v2?"]


def _item(label: str, start: date):
    return SimpleNamespace(source=SimpleNamespace(version_label=label, effective_from=start))


def test_by_how_much_is_computed_from_the_two_cited_figures():
    items = {"E1": _item("2.0", date(2024, 4, 1)), "E2": _item("1.0", date(2023, 4, 1))}
    valid = [SimpleNamespace(text="Approval is measured against threshold 48.3 in Version 2.0.", evidence_ids=["E1"]),
             SimpleNamespace(text="Approval is measured against threshold 87.3 in Version 1.0.", evidence_ids=["E2"])]
    claim = _magnitude_comparison("Which version has the higher threshold, and by how much?", valid, items,
                                  {"E1": 1, "E2": 2})
    assert claim.text.startswith("Version 1.0 has the higher figure") and "decreased by 39.0" in claim.text
    assert sorted(claim.citations) == [1, 2]
    assert _magnitude_comparison("What is the threshold?", valid, items, {"E1": 1, "E2": 2}) is None


def test_rows_that_differ_by_one_word_are_not_merged_but_a_pure_repeat_is():
    rows = [Claim(text="Loans for IPOs have a 50% margin.", citations=[1]),
            Claim(text="Loans against Shares have a 50% margin.", citations=[1]),
            Claim(text="Loans against Shares have a 50% margin.", citations=[2])]
    kept = _without_repeats(rows)
    assert [c.text for c in kept] == ["Loans for IPOs have a 50% margin.", "Loans against Shares have a 50% margin."]
    assert kept[1].citations == [1, 2]


def _evidence(text: str) -> EvidenceSet:
    item = SimpleNamespace(candidate=SimpleNamespace(text=text), source=SimpleNamespace(
        policy_name="Manual", document_title="Manual", version_label="2.0", page_start=5))
    return EvidenceSet([item], 1.0, [], 1.0, [])


RULES = ("1.2.5 The approval note shall be retained in the Risk Registry for a minimum period of 7 years.\n"
         "1.4.4 The approval note shall be retained in the Document Management System for a minimum period of 10 years.\n"
         "2.1.3 The approval note shall be retained in the Treasury Platform for a minimum period of 14 years.")


def test_several_rules_with_different_values_get_a_clarifying_question():
    with pytest.raises(_NoAnswer) as raised:
        _refuse_if_ambiguous(_evidence(RULES), "What is the retention period for the approval note?")
    assert raised.value.reason == "AMBIGUOUS"
    assert any(s.startswith("Clause 1.2.5") for s in raised.value.suggestions)


@pytest.mark.parametrize("question", [
    "What does clause 1.2.5 say about the approval note?",       # names the rule
    "List all retention periods for the approval note.",          # asks for every one
])
def test_a_question_that_picks_a_rule_or_wants_all_is_answered(question):
    _refuse_if_ambiguous(_evidence(RULES), question)  # no exception


def test_a_dangling_citation_bracket_is_removed_from_a_claim():
    [result] = validate_claims(
        [{"text": "Income up to Rs. 3,00,000 counts as low income [", "evidence_ids": ["E1"]}],
        {"E1": EvidenceText("E1", "Income up to Rs. 3,00,000 counts as low income.")}, ["income"], "income",
    )
    assert result.text == "Income up to Rs. 3,00,000 counts as low income"
