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


def test_appended_citation_marks_are_dropped_not_the_claim():
    for text in ("For loans above Rs. 75 lakh the LTV shall not exceed 70% (E1).",
                 "For loans above Rs. 75 lakh the LTV shall not exceed 70% [E1, E2].",
                 "For loans above Rs. 75 lakh the LTV shall not exceed 70%. E1"):
        result = check(text, ["E1"])
        assert result.valid, (text, result.problems)
        assert "E1" not in result.text


def test_a_claim_about_an_evidence_id_is_still_rejected():
    assert not check("E2 says the LTV shall not exceed 70%.", ["E1"]).valid


def test_a_negation_taken_from_another_cited_sentence_is_not_a_reversal():
    evidence = {"E1": EvidenceText("E1", (
        "Where the OVD furnished by the customer does not have updated address, the following documents shall be "
        "deemed to be OVDs. The customer shall submit OVD with current address within a period of three months."
    ))}
    [result] = validate_claims([{
        "text": "Where the OVD does not have updated address, the customer shall submit OVD with current address within a period of three months.",
        "evidence_ids": ["E1"],
    }], evidence)
    assert result.valid, result.problems


def test_a_figure_on_its_own_table_row_passes_without_the_column_heading_beside_it():
    evidence = {"E1": EvidenceText("E1", (
        "7. PROCESSING FEES:\nProduct | Processing Fee\n"
        "SBI Saral | 2.02% - 3.03% of the Loan Amount\n"
        "Xpress Credit | 1.01% of the Loan Amount\n"
        "SBI Career Loan | 0.51% of the Loan Amount"
    ))}
    subjects = ["processing", "fee", "xpress", "credit"]
    ok = validate_claims([{"text": "The processing fee for Xpress Credit is 1.01% of the loan amount.", "evidence_ids": ["E1"]}], evidence, subjects)
    wrong = validate_claims([{"text": "The processing fee for Xpress Credit is 0.51% of the loan amount.", "evidence_ids": ["E1"]}], evidence, subjects)
    assert ok[0].valid, ok[0].problems
    assert not wrong[0].valid


def test_the_policy_name_counts_as_mentioning_its_subject():
    evidence = {"E1": EvidenceText("E1", "This policy is applicable to all employees of the Bank.",
                                   label="Equal Employment Opportunity Policy")}
    [result] = validate_claims([{"text": "The Equal Employment Opportunity Policy applies to all employees of the Bank.",
                                 "evidence_ids": ["E1"]}], evidence, ["equal", "employment", "opportunity"])
    assert result.valid, result.problems


def test_a_figure_glued_to_a_rupee_glyph_is_found_in_the_evidence():
    evidence = {"E1": EvidenceText("E1", "A customer requesting I4,00,000 against 28 grams of gold may be eligible for up to I3,50,000.")}
    [result] = validate_claims([{"text": "The customer may be eligible for up to Rs 3,50,000.", "evidence_ids": ["E1"]}], evidence)
    assert result.valid, result.problems


def test_a_statement_about_what_the_documents_lack_is_not_a_claim():
    from app.modules.rag.validation import ABSENCE_PROBLEM

    [result] = validate_claims([{"text": "The document does not provide the current RBI repo rate.", "evidence_ids": ["E2"]}], EVIDENCE)
    assert not result.valid and result.problems == [ABSENCE_PROBLEM]


def test_a_hedged_statement_about_what_the_documents_lack_is_not_a_claim():
    from app.modules.rag.validation import ABSENCE_PROBLEM

    for text in ("The processing fee on a personal loan is not explicitly stated in the Unsecured Personal Loan Policy.",
                 "The Unsecured Personal Loan Policy in Version 1.0 does not specify a processing fee."):
        [result] = validate_claims([{"text": text, "evidence_ids": ["E2"]}], EVIDENCE)
        assert result.problems == [ABSENCE_PROBLEM], text


def test_a_rule_that_something_is_not_needed_is_still_a_claim():
    from app.modules.rag.validation import ABSENCE_PROBLEM

    evidence = {"E1": EvidenceText("E1", "Borrowers do not need to provide collateral for loans up to Rs. 5 lakh.")}
    [result] = validate_claims([{"text": "Borrowers do not need to provide collateral for loans up to Rs. 5 lakh.",
                                 "evidence_ids": ["E1"]}], evidence)
    assert ABSENCE_PROBLEM not in result.problems and result.valid, result.problems


def test_a_percent_sign_answers_a_question_about_a_percentage():
    from app.modules.rag.validation import TermIndex

    assert TermIndex("The maximum LTV for gold loans is 75%.").mentions("percentage")


def test_a_limit_written_as_a_phrase_states_the_limit():
    from app.modules.rag.validation import TermIndex

    index = TermIndex("The tenure is up to 15 years. Income shall be not less than Rs. 25,000 a month.")
    assert index.mentions("maximum") and index.mentions("minimum")
    assert not TermIndex("Set up tomorrow.").mentions("maximum")


def test_the_readers_word_for_the_documents_word_is_the_same_subject():
    evidence = {"E1": EvidenceText("E1", "We fund up to 65% of the agreed property value.")}
    [result] = validate_claims([{"text": "The lender finances a maximum of 65% of the property value.",
                                 "evidence_ids": ["E1"]}], evidence)
    assert result.valid, result.problems


def test_the_first_figure_of_a_range_takes_its_unit():
    evidence = {"E1": EvidenceText("E1", "Applicant age: 28 to 65 years (70 for business owners). Fee: 2-3% of the amount.")}
    for claim in ("The minimum applicant age is 28 years.", "The fee is at least 2% of the amount."):
        [result] = validate_claims([{"text": claim, "evidence_ids": ["E1"]}], evidence)
        assert result.valid, result.problems
    [wrong] = validate_claims([{"text": "The minimum applicant age is 70 years.", "evidence_ids": ["E1"]}],
                              {"E1": EvidenceText("E1", "Applicant age: 28 to 65 years.")})
    assert not wrong.valid


def test_a_figure_in_a_table_column_is_stated_for_the_columns_heading():
    evidence = {"E1": EvidenceText("E1", (
        "A nominal fee applies for duplicate statements.\n\nCredit score | Interest rate (p.a.) | Processing fee\n\n"
        "750 and above | 10.05% | 1.75% + GST\n\n700 - 749 | 10.45% | 1.75% + GST\n\n650 - 699 | 11.15% | 2.00% + GST"))}
    subjects = ["processing", "fee"]
    ok = validate_claims([{"text": "The processing fee is 1.75% + GST for scores of 750 and above.", "evidence_ids": ["E1"]}],
                         evidence, subjects)
    wrong = validate_claims([{"text": "The processing fee is 10.45% for scores of 700 - 749.", "evidence_ids": ["E1"]}],
                            evidence, subjects)
    assert ok[0].valid, ok[0].problems
    assert not wrong[0].valid


def test_a_claim_about_another_variant_than_the_one_asked_is_rejected():
    evidence = {"E1": EvidenceText("E1", (
        "Tier 2 / Islands Zone | Single-borrower exposure | Max | INR 180 lakh\n"
        "Tier 3 / Islands Zone | Single-borrower exposure | Max | INR 229 lakh"
    ))}
    question = "For Personal Loan (Tier 2 / Islands Zone), what is the maximum single-borrower exposure?"
    wrong, right = validate_claims([
        {"text": "For Tier 3 / Islands Zone the maximum single-borrower exposure is INR 229 lakh.", "evidence_ids": ["E1"]},
        {"text": "For Tier 2 / Islands Zone the maximum single-borrower exposure is INR 180 lakh.", "evidence_ids": ["E1"]},
    ], evidence, question=question)
    assert not wrong.valid and "is about Tier 3, not Tier 2" in wrong.problems
    assert right.valid, right.problems


def test_a_question_comparing_variants_does_not_pin_one():
    from app.modules.rag.validation import qualifiers

    assert qualifiers("Compare Tier 2 and Tier 3 exposure limits") == {}
    assert qualifiers("What is the limit for Tier 2 in Grade B?") == {"tier": "2", "grade": "b"}


def test_there_is_no_stated_value_is_a_statement_about_the_documents():
    from app.modules.rag.validation import ABSENCE_PROBLEM

    [result] = validate_claims([{"text": "There is no stated maximum exposure for Tier 2 in the provided evidence.",
                                 "evidence_ids": ["E1"]}], EVIDENCE)
    assert result.problems == [ABSENCE_PROBLEM]


def test_a_figure_in_a_long_tiered_sentence_belongs_to_its_subject():
    evidence = {"E1": EvidenceText("E1", (
        "DS-10.03.036 Approval authority for Premature Withdrawal (Tier 1 / South Zone) in respect of customers with "
        "more than three years of history is tiered by exposure: up to INR 8 lakh, the Assistant Vice President; above "
        "INR 8 lakh and up to INR 24 lakh, the Chief Risk Officer; above INR 24 lakh, the Product Approval Committee."
    ))}
    [result] = validate_claims([{"text": "Premature Withdrawal (Tier 1 / South Zone) above INR 24 lakh is approved by the Product Approval Committee.",
                                 "evidence_ids": ["E1"]}], evidence, ["premature", "withdrawal", "tier", "south", "zone"])
    assert result.valid, result.problems


def test_restating_the_other_half_of_a_sentence_with_a_negation_is_not_a_reversal():
    evidence = {"E1": EvidenceText("E1", (
        "The Trade Pricing Review shall be submitted to the Credit Risk Committee by working day 6 of each month, "
        "and must include the advance remittances without evidence of import together with its trend."
    ))}
    [result] = validate_claims([{"text": "The Trade Pricing Review shall be submitted to the Credit Risk Committee by working day 6 of each month.",
                                 "evidence_ids": ["E1"]}], evidence)
    assert result.valid, result.problems


# Definitions and tables of contents of two versions, as in a 1,000-page manual.
DEFINITIONS_V1 = (
    "'Reporting' means the reporting activity, measured fortnightly against threshold 17.8.\n"
    "'Exposure' means the exposure activity, measured daily against threshold 69.7.\n"
    "'Documentation' means the documentation activity, measured monthly against threshold 37.1.\n"
    "'Review' means the review activity, measured monthly against threshold 44.7."
)
VERSIONS = {
    "E1": EvidenceText("E1", "Table of Contents\nChapter 1 Digital Banking and Payments\nChapter 7 Vendor Onboarding",
                       versions=frozenset({"2.0"})),
    "E2": EvidenceText("E2", "Table of Contents\nChapter 1 Fraud Prevention and Reporting\nChapter 3 Vendor Onboarding",
                       versions=frozenset({"1.0"})),
    "E3": EvidenceText("E3", DEFINITIONS_V1, versions=frozenset({"1.0"})),
}


def check_versions(text, ids, question=""):
    from app.modules.rag.evidence import key_terms

    [result] = validate_claims([{"text": text, "evidence_ids": ids}], VERSIONS, key_terms(question), question)
    return result


def test_a_measured_value_must_belong_to_the_term_it_is_stated_for():
    question = "How does the Review definition differ between versions?"
    assert check_versions("In Version 1.0, Review is measured monthly against threshold 44.7.", ["E3"], question).valid
    swapped = check_versions("In Version 1.0, Review is measured fortnightly against threshold 17.8.", ["E3"], question)
    assert not swapped.valid and "17.8 is not stated for review" in swapped.problems[0]


def test_a_claim_about_a_version_must_cite_that_version():
    wrong = check_versions("Chapter 1 Digital Banking and Payments is in Version 1.0.", ["E1"])
    assert not wrong.valid and "is about version 1.0 but cites only version 2.0" in wrong.problems
    both = check_versions("Both versions have a chapter called Digital Banking and Payments.", ["E1"])
    assert not both.valid and "is about every version" in both.problems[0]
    assert check_versions("Vendor Onboarding appears in both versions.", ["E1", "E2"]).valid


def test_saying_a_version_lacks_something_is_checked_against_that_version():
    assert check_versions("Chapter 1 Fraud Prevention and Reporting is in Version 1.0 but not in Version 2.0.", ["E2"]).valid
    wrong = check_versions("Chapter 3 Vendor Onboarding is in Version 1.0 but not in Version 2.0.", ["E2"])
    assert not wrong.valid and "says version 2.0 lacks it, but E1 (version 2.0) has it" in wrong.problems


def test_clause_numbers_are_whole_words():
    from app.modules.rag.validation import TermIndex

    index = TermIndex("4.25.19 The Bank shall maintain a monitoring buffer of not less than 16%.")
    assert index.mentions("4.25.19") and index.mentions("4.25")
    assert not index.mentions("4.25.1")  # a shared prefix joins word forms, never numbers


def test_saying_every_version_has_something_is_checked_in_each_version():
    assert check_versions("Chapter 3 Vendor Onboarding appears in both Version 1.0 and Version 2.0.", ["E1", "E2"]).valid
    wrong = check_versions("Chapter 1 Digital Banking and Payments appears in both versions.", ["E1", "E2"])
    assert not wrong.valid and "the version 1.0 passage does not" in wrong.problems[0]


# --- the reader's own figures and checked arithmetic ------------------------------------

def _one(text, evidence, question):
    [result] = validate_claims([{"text": text, "evidence_ids": ["E1"]}], {"E1": EvidenceText("E1", evidence)},
                               question=question)
    return result


FEE = "A processing fee of 2% of the sanctioned amount (minimum Rs. 5,000) is charged on every car loan."
LIMIT = "Disputes about a returned cheque must be raised within 60 days of the return."


def test_a_rule_applied_to_the_readers_figures_is_kept_when_the_arithmetic_checks_out():
    question = "A customer takes a Rs. 5 lakh car loan. What processing fee applies?"
    assert _one("For a Rs. 5 lakh car loan, the processing fee of 2% is Rs. 10,000.", FEE, question).valid
    wrong = _one("For a Rs. 5 lakh car loan, the processing fee of 2% is Rs. 12,000.", FEE, question)
    assert not wrong.valid and "'Rs. 12,000' is not in the cited evidence" in wrong.problems


def test_the_readers_figure_is_never_stated_as_the_rule():
    question = "A customer's cheque was returned 75 days ago. Can they still raise a dispute?"
    assert _one("A dispute after 75 days is beyond the 60-day limit, so it cannot be raised.", LIMIT, question).valid
    assert not _one("Disputes must be raised within 75 days of the return.", LIMIT, question).valid


def test_a_false_premise_is_corrected_and_never_confirmed():
    question = "The cheque dispute limit is 90 days, right?"
    assert _one("Disputes must be raised within 60 days of the return, not 90 days.", LIMIT, question).valid
    assert not _one("Disputes must be raised within 90 days of the return.", LIMIT, question).valid
    negated = _one("Disputes must not be raised within 60 days of the return.", LIMIT, question)
    assert not negated.valid  # a "not" on the rule's own figure reverses the rule


def test_a_figure_in_another_unit_of_time_is_the_same_figure():
    evidence = "The maximum repayment tenure for a car loan is 84 months."
    assert _one("The maximum repayment tenure for a car loan is 7 years.", evidence, "How many years?").valid
    assert not _one("The maximum repayment tenure for a car loan is 8 years.", evidence, "How many years?").valid
    # The reader's own figure is not "converted" into support for itself.
    assert not _one("The maximum repayment tenure for a car loan is 10 years.", evidence,
                    "Is the tenure 120 months?").valid


def test_the_readers_rate_on_the_readers_amount_is_not_the_documents_figure():
    evidence = {"E1": EvidenceText("E1", "We fund up to 65% of the agreed property value.")}
    question = "The latest version finances up to 80% of the property. How much loan can I get on a Rs. 1 crore property?"
    [premise] = validate_claims([{"text": "Financing of up to 80% permits a loan of up to Rs. 80 lakh.", "evidence_ids": ["E1"]}],
                                evidence, question=question)
    [applied] = validate_claims([{"text": "Up to 65% of the property value is funded: up to Rs. 65 lakh on a Rs. 1 crore property.", "evidence_ids": ["E1"]}],
                                evidence, question=question)
    assert not premise.valid
    assert applied.valid, applied.problems
