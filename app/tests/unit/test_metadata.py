from datetime import date

import pytest

from app.modules.ingestion.analysis.dates import parse_date, parse_month_year
from app.modules.ingestion.analysis.metadata import (
    detect_amendments,
    extract_metadata,
    normalize_title,
    repair_title,
    smart_title_case,
)


@pytest.mark.parametrize("text, expected", [
    ("Effective Date: 01/07/2026", date(2026, 7, 1)),
    ("w.e.f. 1st July, 2026", date(2026, 7, 1)),
    ("dated July 1, 2026", date(2026, 7, 1)),
    ("from 2026-07-01 onwards", date(2026, 7, 1)),
    ("on 01-Jul-2026", date(2026, 7, 1)),
    ("on 13/05/2025", date(2025, 5, 13)),
    ("31/02/2026", None),
    ("no date here", None),
])
def test_parse_date(text, expected):
    assert parse_date(text) == expected


def test_parse_date_respects_order():
    assert parse_date("03/04/2026", "MDY") == date(2026, 3, 4)
    assert parse_date("03/04/2026", "DMY") == date(2026, 4, 3)
    assert parse_date("25/12/2026", "MDY") == date(2026, 12, 25)  # unambiguous swap


def test_parse_month_year():
    assert parse_month_year("What was the LTV in March 2025?") == (2025, 3)


@pytest.mark.parametrize("text, expected", [
    ("HOME LOAN CREDIT POLICY", "Home Loan Credit Policy"),
    ("KYC AND AML POLICY FOR NRI CUSTOMERS", "KYC and AML Policy for NRI Customers"),
    ("POLICY ON THE BANK'S LTV NORMS", "Policy on the Bank's LTV Norms"),
    ("Already Mixed Case", "Already Mixed Case"),
])
def test_smart_title_case(text, expected):
    assert smart_title_case(text) == expected


def test_normalize_title_strips_noise():
    assert normalize_title("Home-Loan Policy (Final) v7 2026") == "home loan policy"


def lines(*items):
    return [[i, text, size, False, 0.0] for i, (text, size) in enumerate(items)]


def test_extracts_policy_identity_from_first_page():
    first_page = lines(
        ("HOME LOAN", 18), ("CREDIT POLICY", 18),
        ("Policy No: HL-2025-01", 10), ("Version: 4", 10), ("Revision No. 2", 10),
        ("Issued by: Credit Department", 10), ("Effective Date: 01/07/2026", 10),
    )
    text = "\n".join(line[1] for line in first_page) + "\nDated 15/06/2026"
    meta = extract_metadata(first_page, text, 10.0, "loan_policy_final_v7")
    assert meta.title.value == "Home Loan Credit Policy"
    assert meta.title.source == "largest_font" and meta.title.confidence >= 0.8
    assert meta.policy_number.value == "HL-2025-01"
    assert meta.version_label.value == "4"
    assert meta.revision_number.value == "2"
    assert meta.effective_date == date(2026, 7, 1)
    assert meta.issue_date == date(2026, 6, 15)
    assert meta.issuer.value == "Credit Department"
    assert meta.department.value == "Credit Department"


@pytest.mark.parametrize("separator", [" | ", "\n", "\t", ": "])
def test_effective_date_in_table_does_not_come_from_superseded_version(separator):
    text = ("Mortgage Loan Policy\nVersion 2.0\n"
            f"Effective date{separator}01-Jan-2020\n"
            "Supersedes | Version 1.0 (effective 01-Apr-2019)")
    meta = extract_metadata([], text, 10.0, None)
    assert meta.effective_date == date(2020, 1, 1)
    assert "01-Jan-2020" in meta.effective_date_evidence
    assert "2019" not in meta.effective_date_evidence


@pytest.mark.parametrize("reference", [
    "Supersedes | Version 1.0 (effective 01-Apr-2019)",
    "Supersedes\nVersion 1.0 (effective 01-Apr-2019)",
    "Previous version effective 01-Apr-2019",
])
def test_historical_effective_date_is_skipped_even_when_it_comes_first(reference):
    text = reference + "\nEffective date | 01-Jan-2020"
    meta = extract_metadata([], text, 10.0, None)
    assert meta.effective_date == date(2020, 1, 1)


def test_historical_date_alone_is_not_the_new_documents_effective_or_cover_date():
    text = "Supersedes | Version 1.0 (effective 01-Apr-2019)"
    meta = extract_metadata(lines((text, 10)), text, 10.0, None)
    assert meta.effective_date is None
    assert meta.issue_date is None


def test_invalid_labelled_date_does_not_hide_a_later_valid_date():
    meta = extract_metadata([], "Effective date | 31-Feb-2020\nEffective date | 01-Mar-2020", 10.0, None)
    assert meta.effective_date == date(2020, 3, 1)


def test_filename_like_pdf_title_is_ignored():
    meta = extract_metadata(lines(("Some body text in a normal font.", 10)), "Body.", 10.0, "loan_policy_final_v7")
    assert meta.title.value is None


def test_good_pdf_metadata_title_is_used():
    meta = extract_metadata([], "Body text.", 10.0, "Fraud Risk Management Policy")
    assert meta.title.value == "Fraud Risk Management Policy"
    assert meta.title.source == "pdf_metadata"


def test_detects_amendments_and_supersession():
    text = (
        "Circular No: CRD/2026/45. All branches are informed that Clause 5.2 and 5.3 of the Home Loan "
        "Credit Policy is hereby amended as under. This circular supersedes the Circular CRD/2025/10 "
        "dated 01/01/2025. In partial modification of the Vehicle Loan Policy, the tenure is revised."
    )
    refs = detect_amendments(text)
    kinds = {(r.relation_type, r.target_text) for r in refs}
    assert ("AMENDS", "Home Loan Credit Policy") in kinds
    assert ("SUPERSEDES", "Circular CRD/2025/10") in kinds
    assert ("AMENDS", "Vehicle Loan Policy") in kinds
    amend = next(r for r in refs if r.target_text == "Home Loan Credit Policy")
    assert amend.clauses == ["5.2", "5.3"]
    assert "hereby amended" in amend.sentence


def test_cover_title_spans_near_equal_font_sizes_and_volume_subtitle():
    first_page = [
        [1, "NATIONAL", 38.0, True, 334.2], [1, "ELECTRICITY", 38.0, True, 377.9], [2, "PLAN", 38.1, True, 421.4],
        [3, "VOLUME II – TRANSMISSION", 17.0, False, 470.9],
        [4, "[In fulfilment of CEA's obligation under", 14.0, False, 490.4],
        [8, "OCTOBER 2024", 24.0, True, 632.3],
    ]
    meta = extract_metadata(first_page, "NATIONAL ELECTRICITY PLAN", 11.0, None)
    assert meta.title.value == "National Electricity Plan – Volume II – Transmission"
    assert meta.issue_date == date(2024, 10, 1)
    assert meta.effective_date == date(2024, 10, 1)  # a detection; the person still confirms it


def test_a_title_set_over_several_lines_is_read_whole():
    # The SBI cover: every line is the same size, and a stray line mentions "Policy".
    first_page = [
        [0, "Page 1 of 25", 11.0, False, 30.0],
        [1, "State Bank of India", 14.0, True, 80.0],
        [2, "Policy on", 14.0, True, 110.0],
        [3, "‘Microfinance Loans’", 14.0, True, 130.0],
        [4, "Version 1.0", 14.0, False, 160.0],
        [5, "Policy uploaded in in SBI Times (Path: SBI Times > Manuals/Master", 14.0, True, 190.0],
    ]
    opening = "\n".join(line[1] for line in first_page)
    assert extract_metadata(first_page, opening, 14.0, None).title.value == "Policy on Microfinance Loans"


@pytest.mark.parametrize("title, text, expected", [
    # A letter drawn as a graphic on the cover: the body spells the word out.
    ("OUR CODE OF E HICS", "OUR\nCODE OF\nE HICS\nThis Code of Ethics sets out our ethics.", "OUR CODE OF ETHICS"),
    ("Policy on Recrd Retention", "Policy on Recrd Retention\nEvery record is kept. Record owners review records.", "Policy on Record Retention"),
    # Words the document uses are never changed, even next to a close spelling.
    ("HOME LOAN POLICY", "HOME LOAN POLICY\nLoans and loan limits.", "HOME LOAN POLICY"),
    ("Schedule B", "Schedule B\nSchedules apply.", "Schedule B"),
    ("MANUAL ON LENDING", "MANUAL ON LENDING\nAnnual review of lending. The annual limit.", "MANUAL ON LENDING"),
    ("HOME LOAN POLICY", "HOME LOAN POLICY\nHomes and homes loans policy.", "HOME LOAN POLICY"),
    # Nothing close in the document: left as it is.
    ("Wah-G-Wah", "Wah-G-Wah\nA recipe book.", "Wah-G-Wah"),
])
def test_repair_title(title, text, expected):
    assert repair_title(title, text) == expected
