"""Inflection-insensitive matching for the evidence-coverage gate.

The gate asks "does the evidence contain this question term?". Matching exact
strings rejected correct answers whenever the document used a morphological
variant, so both sides are reduced to a stem first.
"""

import pytest

from app.core.stemming import stem
from app.modules.rag.evidence import coverage_of, key_terms


@pytest.mark.parametrize("question_form, evidence_form", [
    ("renewables", "renewable"),
    ("transmissions", "transmission"),
    ("schemes", "scheme"),
    ("technologies", "technology"),
    ("capacities", "capacity"),
    ("planned", "planning"),
    ("organization", "organisation"),
    ("analyses", "analysis"),
    ("criteria", "criterion"),
])
def test_inflected_forms_share_a_stem(question_form, evidence_form):
    assert stem(question_form) == stem(evidence_form)


def test_silent_e_before_d_still_matches_through_the_coverage_gate():
    # "estimated" reduces to "estimat" and "estimate" to "estimate"; no suffix rule
    # can join them ("limited" would have to behave the same way as "estimated",
    # and they differ). The coverage gate's shared-prefix fallback is what keeps
    # this from rejecting an answerable question, so that is what is asserted.
    coverage, missing = coverage_of(
        "What is the estimated cost?",
        ["The estimate of the scheme is Rs. 75 lakh."],
    )
    assert "estimated" not in missing
    assert "cost" in missing  # genuinely absent, so coverage is not simply 1.0


@pytest.mark.parametrize("word", [
    "university", "business", "analysis", "status", "address", "always", "this",
    "series", "process", "act", "is", "was", "has", "gas", "plus",
])
def test_words_ending_in_s_that_are_not_plurals_are_left_alone(word):
    assert stem(word) == word


def test_numbers_and_clause_ids_are_matched_exactly():
    assert stem("5.2") == "5.2"
    assert stem("2030") == "2030"


def test_coverage_counts_a_variant_as_present():
    coverage, missing = coverage_of(
        "What renewable capacity does the plan target?",
        ["The plan targets a renewable capacity of 500 GW by 2030."],
    )
    assert missing == []
    assert coverage == 1.0


def test_coverage_counts_a_derivational_pair_as_present():
    # "generation" (evidence) vs "generate" (question): no shared suffix, so this
    # is the prefix fallback doing the work.
    coverage, missing = coverage_of(
        "What technologies generate this capacity?",
        ["The technologies and the generation of this capacity."],
    )
    assert missing == []
    assert coverage == 1.0


def test_coverage_still_reports_a_genuinely_absent_term():
    coverage, missing = coverage_of(
        "What is the rooftop solar capacity?",
        ["The transmission plan covers 500 GW."],
    )
    assert set(missing) == {"rooftop", "solar", "capacity"}
    assert coverage == 0.0


def test_a_short_prefix_does_not_match_unrelated_words():
    # "rate"/"rates" share a stem by design (plural), but a 4-letter stem must
    # not be enough to relate two unrelated subjects.
    coverage, missing = coverage_of(
        "What is the loan interest rate?",
        ["The penalty for late payment is waived."],
    )
    assert "interest" in missing and "rate" in missing
    assert coverage < 0.5


def test_coverage_is_full_when_the_question_has_no_specific_terms():
    assert coverage_of("What is this about?", ["anything"]) == (1.0, [])


def test_key_terms_exclude_question_framing_words():
    terms = key_terms("What is the exact title of this document?")
    assert "document" not in terms
