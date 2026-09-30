from app.modules.ingestion.text import sha256_text
from app.modules.versions.comparison import SectionView, compare_sections


def section(number, title, content, page=1, level=1):
    return SectionView(number, title, content, page, page, sha256_text(title + content), level)


OLD = [
    section(None, "Front matter", "HOME LOAN CREDIT POLICY Version: 3", level=0),
    section("5", "Loan to Value", "The maximum LTV shall be 80%."),
    section("5.2", "LTV for High Value Loans", "Above Rs. 75 lakh the LTV shall not exceed 75%.", level=2),
    section("8", "Prepayment", "Prepayment charges of 2% apply.", page=2),
    section("9", "Review", "The policy is reviewed annually."),
]
NEW = [
    section(None, "Front matter", "HOME LOAN CREDIT POLICY Version: 4", level=0),
    section("5", "Loan to Value", "The maximum LTV shall be 80%."),
    section("5.2", "LTV for High Value Loans", "Above Rs. 75 lakh the LTV shall not exceed 70%.", level=2),
    section("7", "NRI Eligibility", "NRIs may apply with a resident co-applicant.", page=2),
    section("10", "Policy Review", "The policy is reviewed annually by the Board."),
]


def test_compare_versions():
    result = compare_sections(OLD, NEW)
    assert [s["label"] for s in result["added"]] == ["7 NRI Eligibility"]
    assert [s["label"] for s in result["removed"]] == ["8 Prepayment"]
    modified = {m["new"]["label"]: m for m in result["modified"]}
    assert set(modified) == {"Front matter", "5.2 LTV for High Value Loans", "10 Policy Review"}
    assert result["unchanged_count"] == 1

    ltv = modified["5.2 LTV for High Value Loans"]
    assert ltv["numeric_changes"]["changed"][0]["old"] == "75%"
    assert ltv["numeric_changes"]["changed"][0]["new"] == "70%"
    assert {"op": "replace", "old": "75%.", "new": "70%."} in ltv["diff"]
    # Original text is never hidden.
    assert ltv["old"]["content"].endswith("75%.") and ltv["new"]["content"].endswith("70%.")

    review = modified["10 Policy Review"]  # renumbered and renamed, matched by fuzzy title
    assert review["title_changed"] is True

    assert "Changed: 5.2 LTV for High Value Loans (75% → 70%)" in result["summary_lines"]
    assert "Added: 7 NRI Eligibility" in result["summary_lines"]
    assert "Removed: 8 Prepayment" in result["summary_lines"]


def test_identical_versions_have_no_changes():
    result = compare_sections(OLD, OLD)
    assert result["stats"] == {"added": 0, "removed": 0, "modified": 0, "unchanged": 5, "numeric_changes": 0}
