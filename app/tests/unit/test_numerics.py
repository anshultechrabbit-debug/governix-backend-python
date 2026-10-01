import pytest

from app.modules.citations.numerics import extract_numeric_facts, numeric_changes


def keys(text):
    return [(f.kind, f.value) for f in extract_numeric_facts(text)]


@pytest.mark.parametrize("text, expected", [
    ("LTV shall not exceed 75%", [("percent", "75")]),
    ("a spread of 2.50 per cent", [("percent", "2.5")]),
    ("loans above Rs. 75 lakh", [("amount", "7500000")]),
    ("₹75,00,000", [("amount", "7500000")]),
    ("INR 7.5 million", [("amount", "7500000")]),
    ("Rs 1.2 crore", [("amount", "12000000")]),
    ("75 lakh", [("quantity", "7500000")]),
    ("tenure up to 30 years", [("duration", "30 year")]),
    ("increase of 25 bps", [("duration", "25 bps")]),
    ("effective from 01/07/2026", [("date", "2026-07-01")]),
    ("from 1st July, 2026", [("date", "2026-07-01")]),
    ("aged between 21 and 65", [("number", "21"), ("number", "65")]),
    ("Clause 5.2 applies", [("number", "5.2")]),
])
def test_extraction(text, expected):
    assert keys(text) == expected


def test_equivalent_amounts_normalise_identically():
    values = {extract_numeric_facts(t)[0].value for t in ("Rs. 75 lakh", "₹75,00,000", "INR 7.5 million", "Rs 7500000")}
    assert values == {"7500000"}


def test_numeric_changes_pairs_by_kind():
    old = "For loans above Rs. 75 lakh the LTV shall not exceed 75%. Tenure up to 30 years."
    new = "For loans above Rs. 75 lakh the LTV shall not exceed 70%. Tenure up to 25 years. Fee 0.5%."
    changes = numeric_changes(old, new)
    changed = {(c["kind"], c["old"], c["new"]) for c in changes["changed"]}
    assert changed == {("percent", "75%", "70%"), ("duration", "30 years", "25 years")}
    assert [a["value"] for a in changes["added"]] == ["0.5%"]
    assert changes["removed"] == []
    assert "LTV shall not exceed 70%" in changes["changed"][0]["new_context"]


def test_grouped_figures_are_read_whole():
    from app.modules.citations.numerics import extract_numeric_facts

    values = [f.value for f in extract_numeric_facts("requesting I4,00,000 for up to I3,50,000 and Rs. 75,000")]
    assert "400000" in values and "350000" in values and "0" not in values and "50000" not in values


def test_a_figure_set_off_with_hyphens_in_a_scan_keeps_its_unit():
    from app.modules.citations.numerics import extract_numeric_facts

    facts = extract_numeric_facts("a minimum period of -7- days and maximum period of 10 Years")
    assert [(f.kind, f.value) for f in facts] == [("duration", "7 day"), ("duration", "10 year")]
