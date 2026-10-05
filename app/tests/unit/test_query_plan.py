import uuid
from datetime import date

import pytest

from app.modules.rag.query_plan import QueryClass, plan_query

TODAY = date(2026, 9, 28)


@pytest.mark.parametrize("question, cls, as_of, labels", [
    ("What is the current LTV for home loans?", QueryClass.CURRENT, TODAY, []),
    ("What was the LTV in March 2025?", QueryClass.HISTORICAL, date(2025, 3, 31), []),
    ("What was the LTV as of 15/02/2025?", QueryClass.HISTORICAL, date(2025, 2, 15), []),
    ("What was the LTV in 2024?", QueryClass.HISTORICAL, date(2024, 12, 31), []),
    ("What does v3 say about prepayment?", QueryClass.SPECIFIC_VERSION, None, ["3"]),
    ("What's changed between v3 and v4?", QueryClass.COMPARISON, None, ["3", "4"]),
    ("What changed in the latest home loan policy?", QueryClass.COMPARISON, None, []),
    ("What was the LTV earlier?", QueryClass.CURRENT, TODAY, []),
])
def test_routing(question, cls, as_of, labels):
    plan = plan_query(question, today=TODAY)
    assert plan.query_class is cls
    assert plan.as_of == as_of
    assert plan.version_labels == labels


@pytest.mark.parametrize("question", [
    "What is the target transmission capacity addition during 2022-27?",
    "How many MW were added in 2017-22?",
    "What is the demand projection for 2026-27?",
    "What does the plan say for FY 2022-23?",
    "Summarise schemes during 2017–2022.",
])
def test_year_range_describes_a_period_not_an_as_of_date(question):
    """A year range names a period the document covers; it must not scope the version.

    Otherwise every version is filtered out and the answer becomes a
    NO_RELEVANT_DOCUMENTS no-answer.
    """
    plan = plan_query(question, today=TODAY)
    assert plan.query_class is QueryClass.CURRENT
    assert plan.as_of == TODAY


def test_explicit_ui_choice_wins():
    ids = [uuid.uuid4(), uuid.uuid4()]
    assert plan_query("What is the LTV?", ui_mode="compare", version_ids=ids).query_class is QueryClass.COMPARISON
    historical = plan_query("What is the current LTV?", ui_mode="historical", as_of=date(2025, 3, 1), today=TODAY)
    assert historical.query_class is QueryClass.HISTORICAL and historical.as_of == date(2025, 3, 1)
    current = plan_query("What was the LTV in March 2025?", ui_mode="current", today=TODAY)
    assert current.as_of == TODAY


def test_a_difference_that_is_the_subject_of_a_rule_is_not_a_version_comparison():
    question = ("What reading of the unreconciled position difference for Proprietary Trading Limits "
                "(Tier 5 / North-East Zone) constitutes an early-warning signal?")
    assert plan_query(question).query_class is QueryClass.CURRENT
    assert plan_query("What are the KYC requirements on changes in address?").query_class is QueryClass.CURRENT
    assert plan_query("What is the difference between the current and the previous version?").query_class is QueryClass.COMPARISON


@pytest.mark.parametrize("question", [
    "For Collection Agency Conduct, what is the days past due limit in each edition, and which edition sets the higher figure?",
    "Compare the exposure level above which the top approval body applies in each edition.",
    "What is the quorum of the Model Governance Committee in both versions?",
    "Which edition sets the higher provisioning rate for substandard assets?",
    "Which version has the later effective date?",
    "Which edition requires earlier submission?",
    # Evaluation Q319-Q330: a count of versions, or "the", between the quantifier and "versions".
    "Give the maximum ltv ratio in all three versions of the Home Loan Policy.",
    "Give the minimum credit score in all 3 versions of the Personal Loan Policy.",
    "What did each of the three versions say about the STR filing deadline?",
    "List the processing fee in all the versions.",
])
def test_a_question_about_every_edition_searches_every_version(question):
    plan = plan_query(question, today=TODAY)
    assert plan.query_class is QueryClass.ACROSS_VERSIONS
    assert plan.mode == "all" and plan.as_of is None


def test_naming_versions_still_compares_them():
    assert plan_query("Compare the LTV in v1 and v2", today=TODAY).query_class is QueryClass.COMPARISON
    assert plan_query("Which version is in force?", today=TODAY).query_class is QueryClass.CURRENT


@pytest.mark.parametrize("question, labels", [
    ("Compare the policy owner in Version 1.0 and Version 2.0", ["1.0", "2.0"]),
    ("Who is the policy owner in Version 1.0 and Version 2.0?", ["1.0", "2.0"]),
    ("What is the difference between version 1 and version 2?", ["1", "2"]),
    ("What changed in version 2.0?", ["2.0"]),
    ("How does edition 3 differ from the current one?", ["3"]),
])
def test_naming_versions_to_compare_reads_each_of_them(question, labels):
    plan = plan_query(question, today=TODAY)
    assert plan.query_class is QueryClass.COMPARISON and plan.mode == "versions"
    assert plan.version_labels == labels


def test_a_comparison_without_versions_is_resolved_against_the_documents():
    plan = plan_query("Has the policy owner changed between the versions?", today=TODAY)
    assert plan.query_class is QueryClass.COMPARISON and plan.version_labels == [] and not plan.diff


@pytest.mark.parametrize("a, b", [("v1", "1.0"), ("1", "01.00"), ("Version 3", "3.0"), ("2.10", "2.10")])
def test_version_labels_written_differently_are_the_same_version(a, b):
    from app.modules.rag.query_plan import normal_label

    assert normal_label(a) == normal_label(b)


def test_minor_versions_stay_distinct():
    from app.modules.rag.query_plan import normal_label

    assert normal_label("2.1") != normal_label("2.10")
