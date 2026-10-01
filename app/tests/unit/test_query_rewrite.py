"""Follow-ups and non-English questions become standalone English search questions."""
from types import SimpleNamespace

import pytest

from app.infrastructure.ai.llm.base import LLMUnavailableError
from app.modules.rag.query_rewrite import is_non_english, refers_to_earlier_turn, standalone_question
from app.modules.rag.schema import ConversationTurn


class FakeLLM:
    def __init__(self, content=None, error=False):
        self.content, self.error, self.calls = content or {}, error, []

    def generate_json(self, system, user, schema, *, context=None):
        self.calls.append(user)
        if self.error:
            raise LLMUnavailableError("down")
        return SimpleNamespace(content=self.content)


HISTORY = [ConversationTurn(
    question="What was the estimated cost of the 170 transmission schemes mentioned in the comments section?",
    answer="The addition in ISTS includes total 170 transmission schemes with estimated cost of Rs. 3,13,950 Crores.",
)]


@pytest.mark.parametrize("question", [
    "How many transmission schemes were mentioned in that discussion?",
    "What about the previous version?",
    "What changed?",
    "Who made that comment?",
])
def test_follow_ups_are_detected(question):
    assert refers_to_earlier_turn(question)


@pytest.mark.parametrize("question", [
    "What is the name of this document?",
    "Who suggested that rooftop solar could contribute more than 70-80% of annual electrical energy?",
    "What changed between version 1.0 and 2.0?",
    "Why are both BESS and pumped-storage plants considered?",
])
def test_standalone_questions_are_left_alone(question):
    assert not refers_to_earlier_turn(question)
    llm = FakeLLM()
    assert standalone_question(llm, question, HISTORY).question == question
    assert llm.calls == []  # no model call for an ordinary question


def test_follow_up_without_history_is_unresolvable():
    rewrite = standalone_question(FakeLLM(), "How many schemes were in that discussion?", [])
    assert rewrite.reason == "follow_up" and not rewrite.resolvable


def test_follow_up_with_history_is_rewritten_from_the_conversation():
    llm = FakeLLM({"standalone_question": "How many ISTS transmission schemes had an estimated cost of Rs. 3,13,950 crore?",
                   "resolvable": True})
    rewrite = standalone_question(llm, "How many schemes were in that discussion?", HISTORY)
    assert rewrite.resolvable and "3,13,950" in rewrite.question
    assert "170 transmission schemes" in llm.calls[0]  # the earlier turn was supplied


def test_model_says_unresolvable():
    llm = FakeLLM({"standalone_question": "How many schemes were in that discussion?", "resolvable": False})
    assert not standalone_question(llm, "How many schemes were in that discussion?", HISTORY).resolvable


def test_non_english_question_is_translated():
    assert is_non_english("बीईएसएस का पूर्ण रूप क्या है?") and not is_non_english("What does BESS stand for?")
    llm = FakeLLM({"standalone_question": "What is the full form of BESS?", "resolvable": True})
    rewrite = standalone_question(llm, "बीईएसएस का पूर्ण रूप क्या है?", [])
    assert rewrite.reason == "translation" and rewrite.question == "What is the full form of BESS?"


def test_unavailable_model_keeps_the_question_and_blocks_only_follow_ups():
    down = FakeLLM(error=True)
    assert standalone_question(down, "बीईएसएस क्या है?", []).resolvable
    assert not standalone_question(down, "What about that scheme?", HISTORY).resolvable


class _Rewriter:
    def __init__(self, question, resolvable):
        self.content = {"standalone_question": question, "resolvable": resolvable}

    def generate_json(self, *_args, **_kwargs):
        from app.infrastructure.ai.llm.base import LLMResult
        return LLMResult(content=self.content, model="test")


def test_a_pronoun_is_resolved_against_the_earlier_turn():


    history = [ConversationTurn(question="What is the retention period for MAP/SIR Reports?", answer="5 Years.")]
    rewrite = standalone_question(_Rewriter("What is the place of storage of MAP/SIR Reports?", True),
                                  "What is its place of storage?", history)
    assert rewrite.question == "What is the place of storage of MAP/SIR Reports?" and rewrite.reason == "follow_up"


def test_a_pronoun_without_an_earlier_turn_is_searched_as_asked():
    rewrite = standalone_question(None, "What is the loan scheme and its tenure?", [])
    assert rewrite.question == "What is the loan scheme and its tenure?" and rewrite.resolvable


def test_a_translation_is_never_unresolvable():
    rewrite = standalone_question(_Rewriter("How is the interest rate on microfinance loans decided?", False),
                                  "माइक्रोफाइनेंस ऋण की ब्याज दर कैसे तय की जाती है?", [])
    assert rewrite.resolvable and rewrite.reason == "translation"


def test_a_misspelt_chatty_question_is_clarified():
    from app.modules.rag.query_rewrite import restated_questions

    llm = _Rewriter("", True)
    llm.content = {"questions": ["What is the KYC policy?"]}
    assert restated_questions(llm, "Tell me about the kyc pokucy") == ["What is the KYC policy?"]


def test_a_question_about_two_subjects_is_split():
    from app.modules.rag.query_rewrite import restated_questions

    llm = _Rewriter("", True)
    llm.content = {"questions": ["How should KYC information be maintained?", "How are pet-product variants handled?", "x", "y"]}
    assert restated_questions(llm, "KYC and pet products?") == [
        "How should KYC information be maintained?", "How are pet-product variants handled?", "x",
    ]


def test_an_unchanged_or_missing_restatement_is_none():
    from app.modules.rag.query_rewrite import restated_questions

    llm = _Rewriter("", True)
    llm.content = {"questions": ["What is the KYC policy"]}
    assert restated_questions(llm, "What is the KYC policy?") is None
    llm.content = {"questions": []}
    assert restated_questions(llm, "What is the KYC policy?") is None
    assert restated_questions(None, "Tell me about the kyc pokucy") is None


def test_a_pronoun_inside_a_standalone_question_never_blocks_it():
    history = [ConversationTurn(question="What is the maximum tenor for auto loans?", answer="22 months.")]
    question = "For personal loans, what is the maximum exposure, and what happens if a proposal goes beyond it?"
    rewrite = standalone_question(_Rewriter(question, False), question, history)
    assert rewrite.resolvable and rewrite.question == question and rewrite.reason is None


def test_an_explicit_reference_that_cannot_be_resolved_still_asks_to_restate():
    history = [ConversationTurn(question="What is the maximum tenor for auto loans?", answer="22 months.")]
    rewrite = standalone_question(_Rewriter("How many schemes were in that discussion?", False),
                                  "How many schemes were in that discussion?", history)
    assert not rewrite.resolvable
