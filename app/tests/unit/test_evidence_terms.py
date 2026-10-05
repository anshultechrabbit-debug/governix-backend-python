"""Which question words the evidence must contain before an answer is attempted."""
import pytest

from app.modules.rag.evidence import coverage_of, is_document_question, key_terms


def test_question_framing_words_are_not_required_but_numbers_are():
    terms = key_terms("What was the estimated cost of the 170 transmission schemes mentioned in the comments section?")
    assert "170" in terms and "transmission" in terms
    assert not {"mentioned", "section"} & set(terms)


@pytest.mark.parametrize("question, framing", [
    ("When is a home loan account classified as an NPA as per the current policy?", "per"),
    ("What does clause 4.2 say, and on which page is it?", "page"),
    ("Give the maximum tenure in all three versions.", "all"),
    ("Give the maximum tenure in all three versions.", "three"),
    ("Which version first changed the processing fee?", "first"),
    ("Does the policy contain a provision on top-up loans?", "provision"),
    ("Home loan vs personal loan tenure", "vs"),
])
def test_closed_class_words_never_name_the_subject(question, framing):
    # Evaluation Q099, Q307, Q319, Q151, Q189: these words were "missing" from every passage.
    assert framing not in key_terms(question)


def test_an_abbreviation_is_covered_by_the_words_it_abbreviates():
    # Q030/Q099: "NPA" asked, "Non-Performing Asset" written; Q063: "FIU" and "Financial Intelligence Unit".
    text = ("A home loan account shall be classified as a Non-Performing Asset when any amount remains overdue. "
            "Cash deposits of Rs. 10 lakh or more shall be reported to the Financial Intelligence Unit. "
            "The Loan-to-Value ratio shall not exceed 80%.")
    coverage, missing = coverage_of("When is an account an NPA, and when is a report sent to the FIU? What is the LTV?", [text])
    assert not {"npa", "fiu", "ltv"} & set(missing)


def test_ordinary_words_are_not_read_as_an_abbreviation():
    from app.modules.rag.validation import TermIndex

    index = TermIndex("The officer shall take reasonable steps. The Regional Credit Officer and staff shall review it.")
    assert not index.mentions("str")          # lower-case words are not an expansion
    assert not index.mentions("oas")          # a run ends before a trailing connector
    assert index.mentions("rco")


def test_single_digit_is_not_a_key_term():
    assert "5" not in key_terms("What is Chapter 5 about?")


@pytest.mark.parametrize("question, expected", [
    ("What is the name of this document?", True),
    ("What is the exact title, issuing authority, publication date and legal basis of this document?", True),
    ("Who published the National Electricity Plan?", True),
    ("Under which Act was the plan prepared?", True),
    ("Which section of the Electricity Act is mentioned?", True),
    ("What does BESS stand for?", False),
    ("What is the name of the top approval body for Loss Event Reporting exposures?", False),
    ("Why are planning margins used?", False),
])
def test_document_questions(question, expected):
    assert is_document_question(question) is expected


def test_content_questions_about_the_plan_are_not_document_questions():
    assert not is_document_question("What does the plan say about transmission lines passing through forests?")


def test_repeated_passages_keep_only_the_best_ranked_copy():
    import uuid

    from app.modules.rag.evidence import distinct_passages
    from app.modules.search.retrieval import Candidate

    def cand(text, page):
        return Candidate(uuid.uuid4(), uuid.uuid4(), None, None, None, page, text, "", None, page, page)

    ranked = [cand("Review and Audit: retain evidence.", 5), cand("review and audit — retain evidence", 9),
              cand("Different clause.", 2)]
    assert [c.page_start for c in distinct_passages(ranked)] == [5, 2]


def test_a_hyphenated_term_matches_its_parts_even_when_the_line_broke_at_the_hyphen():
    coverage, missing = coverage_of("How is delay in reporting to FIU-IND treated?",
                                    ["Delay in reporting to the Director, FIU- IND is treated as a separate violation."])
    assert "fiu-ind" not in missing


def test_greetings_and_answer_style_are_not_search_terms():
    question = "Hello i want to know about the kyc policy can you please explain me in short i just want a short script"
    assert key_terms(question) == ["kyc"]


@pytest.mark.parametrize("text, greeting", [
    ("hi", True), ("Hello there!", True), ("thank you so much", True), ("good morning team", True),
    ("hi what is the kyc policy", False), ("What is the retention period?", False),
])
def test_greetings_are_recognised(text, greeting):
    from app.modules.rag.service import is_greeting

    assert is_greeting(text) is greeting


def test_places_the_question_names_are_recognised_but_titles_are_not_required_words():
    from app.modules.rag.service import named_entities

    assert named_entities("Why can't Kerala and Goa meet their electricity demand?") == ["kerala", "goa"]
    assert named_entities("What is the processing fee for SBI Saral?") == []
    assert named_entities("What is the gold loan LTV?") == []


def test_questions_that_ask_several_things_are_recognised():
    from app.modules.rag.service import asks_several

    assert asks_several("What is the title and who is the issuing authority?")
    assert asks_several("What is the LTV? Who approves it?")
    assert not asks_several("What are the terms and conditions of the loan?")


def test_a_passage_label_carries_its_version_and_period():
    """ "Version 2.0 has the later effective date" is supported by the evidence header."""
    from datetime import date
    from types import SimpleNamespace

    from app.modules.rag.service import _label

    source = SimpleNamespace(policy_name="Credit Manual", document_title=None, section_path="Cover",
                             version_label="2.0", effective_from=date(2024, 4, 1), effective_to=None)
    assert _label(source) == "Credit Manual Cover Version 2.0 effective date 2024-04-01 to present"


def test_version_numbers_and_comparison_words_are_not_required_terms():
    assert key_terms("Compare the policy owner in Version 1.0 and Version 2.0") == ["owner"]
    assert key_terms("Has the policy owner changed between the versions?") == ["owner"]
    # Outside a version comparison the same words can be the subject of a rule.
    assert "changes" in key_terms("What are the KYC requirements on changes in address?")
    assert "difference" in key_terms("What reading of the unreconciled position difference is a warning sign?")


def test_a_long_diff_is_capped_for_the_model():
    from app.modules.rag.prompts import MAX_DIFF_LINES, comparison_text

    modified = [{"new": {"label": f"Section {n}"}, "numeric_changes": {"changed": [
        {"old": "1%", "new": "2%", "old_context": "1%", "new_context": "2%"}]}} for n in range(500)]
    diff = {
        "from_version": {"label": "1.0", "effective_from": "2023-04-01"},
        "to_version": {"label": "2.0", "effective_from": "2024-04-01"},
        "summary_lines": [f"Changed: Section {n} (1% → 2%)" for n in range(500)],
        "modified": modified, "stats": {"modified": 500, "added": 0, "removed": 0, "unchanged": 3},
    }
    text = comparison_text(diff)
    assert len(text.splitlines()) <= 2 * MAX_DIFF_LINES + 4
    assert "460 more" in text and "version 2.0 differs from version 1.0 in 500 sections" in text


@pytest.mark.parametrize("question, terms", [
    ("Give the Version 2.0 answer for the monitoring buffer with exact section reference.", ["monitoring", "buffer"]),
    ("Which chapters appear in both versions?", ["chapters"]),
    ("Compare the two version definitions of Pricing and cite both.", ["definitions", "pricing"]),
])
def test_how_to_answer_is_not_what_to_find(question, terms):
    assert key_terms(question) == terms
