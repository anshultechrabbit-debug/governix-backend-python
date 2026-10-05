"""A policy is recognised by any name its own data gives it, and only when the name picks out one policy."""
import uuid

from app.modules.search.policy_names import build_names, find_mentions, without_references

HOME, PERSONAL, KYC = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
CATALOGUE = [
    build_names(HOME, "Home Loan Credit & Operations Policy",
                front_matter=["Home Loan Credit & Operations Policy\n\nDocument ID: HLP-v3.0   Owner: Chief Risk Officer"]),
    build_names(PERSONAL, "Unsecured Personal Loan Policy",
                front_matter=["Unsecured Personal Loan Policy\n\nDocument ID: PLP-v3.0"]),
    build_names(KYC, "Know Your Customer and Anti-Money Laundering Policy",
                front_matter=["Know Your Customer and Anti-Money Laundering Policy\n\nDocument ID: KAP-v3.0"]),
]


def named(question):
    return [(m.policy_id, m.text, m.explicit) for m in find_mentions(question, CATALOGUE)]


def test_initials_and_declared_codes_name_a_policy():
    assert named("What does clause KAP-KEY-01 say in KYC/AML Policy v1.0?")[-1] == (KYC, "KYC/AML Policy", True)
    assert named("AML policy STR deadline") == [(KYC, "AML policy", True)]
    assert named("What is the KYC refresh period?") == [(KYC, "KYC", False)]
    # A version label after the name makes it a reference; the label itself is the planner's.
    assert named("HLP v2 prepayment charge") == [(HOME, "HLP", True)]


def test_distinctive_title_words_name_a_policy():
    assert named("What is the maximum FOIR in the Home Loan Policy v1.0?") == [(HOME, "Home Loan Policy", True)]
    # The subject ("a personal loan") and the reference ("the Personal Loan Policy") are two mentions.
    assert named("What is the processing fee on a personal loan in the Personal Loan Policy v2.0?") == [
        (PERSONAL, "personal loan", False), (PERSONAL, "Personal Loan Policy", True)]
    assert named("Summarise the Know Your Customer and Anti-Money Laundering Policy") == [
        (KYC, "Know Your Customer and Anti-Money Laundering Policy", True)]


def test_a_name_that_fits_several_policies_or_none_is_not_resolved():
    assert named("What is the loan policy on prepayment?") == []        # home or personal
    assert named("How should we verify your customer address?") == []  # too little of the KYC title
    assert named("What is the maximum LTV for a gold loan?") == []      # no such policy
    assert named("hl ltv") == []                                        # two lowercase letters: not a name


def test_two_letter_and_lower_case_aliases_need_a_document_word():
    assert named("HL policy LTV") == [(HOME, "HL policy", True)]
    assert named("kyc policy str deadline") == [(KYC, "kyc policy", True)]
    assert named("kyc str deadline") == []


def test_explicit_references_leave_the_subject():
    question = "When is a home loan account classified as an NPA as per the current Home Loan Policy?"
    assert without_references(question, find_mentions(question, CATALOGUE)) == (
        "When is a home loan account classified as an NPA as per the current ?")
