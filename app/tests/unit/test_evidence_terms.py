"""Which question words the evidence must contain before an answer is attempted."""
import pytest

from app.modules.rag.evidence import coverage_of, is_document_question, key_terms


def test_question_framing_words_are_not_required_but_numbers_are():
    terms = key_terms("What was the estimated cost of the 170 transmission schemes mentioned in the comments section?")
    assert "170" in terms and "transmission" in terms
    assert not {"mentioned", "section"} & set(terms)


def test_single_digit_is_not_a_key_term():
    assert "5" not in key_terms("What is Chapter 5 about?")


@pytest.mark.parametrize("question, expected", [
    ("What is the name of this document?", True),
    ("What is the exact title, issuing authority, publication date and legal basis of this document?", True),
    ("Who published the National Electricity Plan?", True),
    ("Under which Act was the plan prepared?", True),
    ("Which section of the Electricity Act is mentioned?", True),
    ("What does BESS stand for?", False),
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
