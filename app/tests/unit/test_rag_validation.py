from app.modules.rag.validation import EvidenceText, validate_claims

EVIDENCE = {
    "E1": EvidenceText("E1", "For loans above Rs. 75 lakh the LTV shall not exceed 70%. Effective from 01/07/2026.",
                       {("number", "5.2")}),
    "E2": EvidenceText("E2", "The interest rate is linked to the repo rate with a spread of 2.50% per annum."),
}


def check(text, ids):
    [result] = validate_claims([{"text": text, "evidence_ids": ids}], EVIDENCE)
    return result


def test_supported_claim_passes():
    result = check("For loans above Rs. 75 lakh the LTV must not exceed 70% (Section 5.2).", ["E1"])
    assert result.valid, result.problems
    assert result.numbers_checked == 3


def test_equivalent_number_formats_pass():
    assert check("Loans above ₹75,00,000 have a maximum LTV of 70 per cent.", ["E1"]).valid


def test_wrong_number_is_rejected():
    result = check("For loans above Rs. 75 lakh the LTV shall not exceed 80%.", ["E1"])
    assert not result.valid
    assert "'80%' is not in the cited evidence" in result.problems


def test_number_from_uncited_evidence_is_rejected():
    result = check("The LTV spread is 2.50% for loans above Rs. 75 lakh.", ["E1"])
    assert not result.valid


def test_invented_citation_is_rejected():
    result = check("The LTV shall not exceed 70%.", ["E9"])
    assert not result.valid and "invented citation(s) E9" in result.problems


def test_missing_citation_is_rejected():
    assert not check("The LTV shall not exceed 70%.", []).valid


def test_unsupported_content_is_rejected():
    result = check("Gold loans require hallmarked jewellery and insurance cover.", ["E2"])
    assert not result.valid and "weak support" in result.problems[0]


def test_wrong_date_is_rejected():
    assert not check("The new LTV is effective from 01/08/2026.", ["E1"]).valid
    assert check("The new LTV is effective from 1st July, 2026.", ["E1"]).valid


def test_framing_words_do_not_weaken_a_supported_claim():
    evidence = {"E1": EvidenceText("E1", "NATIONAL ELECTRICITY PLAN\n\nOCTOBER 2024")}
    [result] = validate_claims([{"text": "The document is dated OCTOBER 2024.", "evidence_ids": ["E1"]}], evidence)
    assert result.valid, result.problems


def test_claim_that_talks_about_evidence_ids_is_rejected():
    evidence = {"E2": EvidenceText("E2", "Chapter : Contents")}
    [result] = validate_claims([{"text": "E2 states 'Chapter : Contents'.", "evidence_ids": ["E2"]}], evidence)
    assert not result.valid


def test_metadata_echo_is_recognised_only_in_the_header_format():
    from app.modules.rag.service import _METADATA_ECHO

    assert _METADATA_ECHO.search("Version 1 of the Plan is effective 2024-10-01 to present.")
    assert not _METADATA_ECHO.search("The plan covers the period 2022-27 and 2027-32.")
    assert not _METADATA_ECHO.search("The circular is effective from 1 April 2025.")


RETENTION = {"E1": EvidenceText("E1", (
    "Sl. No.: 60 | TITLE OF THE RECORD: IRC User Control Register | PLACE OF STORAGE: GMU-K | "
    "PROPOSED RETENTION PERIOD: 5 Years | REMARKS: No Change\n\n"
    "Sl. No.: 61 | TITLE OF THE RECORD: L TO Backup Register | PLACE OF STORAGE: GMU-K | "
    "PROPOSED RETENTION PERIOD: 10 Years | REMARKS: No Change\n\n"
    "Sl. No.: 65 | TITLE OF THE RECORD: MAP /SIR Reports | PLACE OF STORAGE: GMU-K | "
    "PROPOSED RETENTION PERIOD: 5 Years | REMARKS: No Change"
))}
SUBJECTS = ["proposed", "retention", "period", "map/sir", "reports"]


def retention(text):
    [result] = validate_claims([{"text": text, "evidence_ids": ["E1"]}], RETENTION, SUBJECTS)
    return result


def test_a_figure_stated_beside_its_subject_passes():
    assert retention("The proposed retention period for MAP/SIR Reports is 5 Years.").valid


def test_a_figure_from_another_row_is_rejected():
    result = retention("The proposed retention period for MAP/SIR Reports is 10 Years.")
    assert not result.valid
    assert any("not stated for" in p for p in result.problems)


def test_a_subject_the_cited_evidence_never_names_is_rejected():
    evidence = {"E1": EvidenceText("E1", "PROPOSED RETENTION PERIOD: 10 Years for the L TO Backup Register.")}
    [result] = validate_claims(
        [{"text": "The retention period for MAP/SIR Reports is 10 Years.", "evidence_ids": ["E1"]}], evidence, SUBJECTS,
    )
    assert not result.valid and "does not mention map/sir" in result.problems[0]


def test_a_dropped_negation_is_rejected():
    evidence = {"E1": EvidenceText("E1", "No prepayment charges apply to floating rate home loans.")}
    [result] = validate_claims(
        [{"text": "Prepayment charges apply to floating rate home loans.", "evidence_ids": ["E1"]}], evidence,
    )
    assert not result.valid and "reverses the negation" in result.problems[-1]


def test_a_short_answer_after_the_question_keeps_its_negation():
    evidence = {"E1": EvidenceText("E1", "Q 6. Can expected income from an asset financed by a microfinance loan "
                                         "be included for estimation of household income? Ans. No.")}
    [result] = validate_claims([{
        "text": "No, expected income from an asset financed by a microfinance loan cannot be included for estimation of household income.",
        "evidence_ids": ["E1"],
    }], evidence)
    assert result.valid, result.problems


def test_a_paraphrase_is_not_judged_on_negation():
    assert check("Loans above Rs. 75 lakh have a maximum LTV of 70%.", ["E1"]).valid
