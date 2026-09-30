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
