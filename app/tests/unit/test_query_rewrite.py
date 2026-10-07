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
        self.calls: list[str] = []  # the user message of each call

    def generate_json(self, _system, user, *_args, **_kwargs):
        from app.infrastructure.ai.llm.base import LLMResult
        self.calls.append(user)
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
    llm.content = {"questions": [f"Question {n}?" for n in range(12)]}
    assert restated_questions(llm, "Twelve questions") == [f"Question {n}?" for n in range(10)]


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


def test_the_same_between_things_the_question_names_is_not_a_follow_up():
    assert not refers_to_earlier_turn("Do the two versions have the same policy owner?")
    assert not refers_to_earlier_turn("Is the LTV the same in v1 and v2?")
    assert refers_to_earlier_turn("What did the same policy say about prepayment?")


# --- a long chat (evaluation of a 115-turn conversation) ---------------------------------

def test_several_questions_in_one_message_are_never_collapsed_by_a_pronoun():
    # Turn 74: "... Answer both without mixing them.\n... What does the Gold Loan Policy actually say?"
    # was rewritten into the second question only. "them" points inside the message.
    history = [ConversationTurn(question="Is gold purity verification required?", answer="Gold purity must be verified.")]
    message = ("A customer asks about gold-loan LTV and mortgage LTV. Answer both without mixing them.\n"
               "A customer has an existing personal loan and wants a gold loan. What does the Gold Loan Policy say?")
    llm = _Rewriter("What does the Gold Loan Policy say about an existing personal loan?", True)
    rewrite = standalone_question(llm, message, history)
    assert rewrite.question == message and not llm.calls


def test_the_rewrite_keeps_every_question():
    from app.modules.rag.query_rewrite import SYSTEM_PROMPT

    assert "Never drop, merge or replace one of them" in SYSTEM_PROMPT


def test_which_questions_depend_on_the_conversation():
    from app.modules.rag.query_rewrite import depends_on_history

    assert depends_on_history("What is the limit?", HISTORY)                 # short: leans on the earlier turn
    assert depends_on_history("What about mortgage?", HISTORY)
    assert depends_on_history("Who approves it for salaried applicants?", HISTORY)
    assert not depends_on_history("What is the limit?", [])                  # nothing to lean on
    assert not depends_on_history("What is the maximum LTV for residential mortgages above Rs. 75 lakh?", HISTORY)


def test_a_short_follow_up_that_found_nothing_is_restated_from_the_conversation():
    from app.modules.rag.query_rewrite import restated_questions

    llm = _Rewriter("", True)
    llm.content = {"questions": ["What LTV is allowed for gold loans?"]}
    history = [ConversationTurn(question="What is the gold-loan LTV?", answer="The maximum LTV for gold loans is 75%.")]
    assert restated_questions(llm, "How much is allowed?", history) == ["What LTV is allowed for gold loans?"]
    assert "What is the gold-loan LTV?" in llm.calls[-1]                     # the model saw the earlier turn
    restated_questions(llm, "How much is allowed?")
    assert "Earlier conversation" not in llm.calls[-1]                       # and none when none is given


def test_an_oversized_earlier_turn_is_cut_not_refused():
    from app.modules.rag.schema import AskRequest

    request = AskRequest(question="What is the limit?", history=[{"question": "q" * 3000, "answer": "a" * 20000}])
    assert (len(request.history[0].question), len(request.history[0].answer)) == (2000, 8000)


@pytest.mark.parametrize("question, foreign", [
    ("LTV kitna hai gold loan ka?", True),                     # Hinglish in Latin letters
    ("¿Cuál es el LTV máximo para préstamos de oro?", True),
    ("Quel est le taux maximum du prêt ?", True),
    ("गोल्ड लोन का एलटीवी क्या है?", True),
    ("What is the LTV for gold loans?", False),
    ("wat is max tenur of home lon v3", False),                # informal English stays English
    ("Hi", False),
])
def test_questions_not_in_english_are_recognised_in_any_script(question, foreign):
    assert is_non_english(question) is foreign


def test_the_rewrite_reports_the_readers_language():
    llm = FakeLLM({"standalone_question": "What is the maximum LTV for gold loans?", "resolvable": True,
                   "language": "Spanish"})
    rewrite = standalone_question(llm, "¿Cuál es el LTV máximo para préstamos de oro?", [])
    assert rewrite.reason == "translation" and rewrite.language == "Spanish"


def test_a_translation_may_reword_but_never_refigure():
    from app.modules.rag.service import _same_figures

    assert _same_figures("The fee of 2% is Rs. 10,000.", "La comisión del 2% es de Rs. 10,000.")
    assert not _same_figures("The fee of 2% is Rs. 10,000.", "La comisión del 3% es de Rs. 10,000.")
