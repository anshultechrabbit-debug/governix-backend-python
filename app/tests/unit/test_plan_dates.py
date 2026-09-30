"""Undated versions in a bulk upload keep the order the person arranged."""
from datetime import date

from app.modules.uploads.service import plan_dates

TODAY = date(2026, 9, 30)


def test_all_undated_newest_gets_the_upload_date_older_ones_step_back():
    planned = plan_dates([None, None, None], [None, None, None], TODAY)
    assert [p.effective_from for p in planned] == [date(2026, 9, 28), date(2026, 9, 29), TODAY]
    assert [p.source for p in planned] == ["inferred", "inferred", "upload_date"]


def test_entered_and_stated_dates_are_kept_when_they_fit_the_order():
    planned = plan_dates([None, date(2025, 1, 1), None], [date(2023, 4, 1), None, date(2026, 7, 1)], TODAY)
    assert [(p.effective_from, p.source) for p in planned] == [
        (date(2023, 4, 1), "detected"), (date(2025, 1, 1), "entered"), (date(2026, 7, 1), "detected"),
    ]


def test_a_stated_date_that_contradicts_the_order_yields_to_the_order():
    planned = plan_dates([None, None], [date(2026, 8, 1), date(2026, 7, 1)], TODAY)
    assert planned[1].effective_from == date(2026, 7, 1)
    assert planned[0].effective_from == date(2026, 6, 30) and planned[0].source == "inferred"
    assert planned[0].note  # the person is told why the document's date was not used
