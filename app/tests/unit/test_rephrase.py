from app.infrastructure.ai.llm.base import LLMResult, LLMUnavailableError
from app.modules.search.rephrase import MAX_REPHRASINGS, rephraser, rephrasings


class LLM:
    def __init__(self, content=None, error=None):
        self.content, self.error, self.calls = content, error, 0

    def generate_json(self, system, user, schema, *, context=None):
        self.calls += 1
        if self.error:
            raise self.error
        return LLMResult(content=self.content, model="fake")


def test_rewordings_drop_repeats_and_the_question_as_asked():
    llm = LLM({"queries": ["record retention period", "Record  retention period", "How long do we keep records?",
                           "preservation of records", "KYC retention", "a fourth"]})
    assert rephrasings(llm, "How long do we keep records") == [
        "record retention period", "Record retention period", "preservation of records",
    ][:MAX_REPHRASINGS]


def test_no_model_or_a_failing_one_gives_no_rewordings():
    assert rephrasings(None, "q") == []
    assert rephrasings(LLM(error=LLMUnavailableError("down")), "q") == []
    assert rephrasings(LLM({"queries": "not a list"}), "q") == []


def test_one_question_is_reworded_once():
    llm = LLM({"queries": ["retention period"]})
    rephrase = rephraser(lambda: llm)
    assert rephrase("keep records?") == rephrase("keep records?") == ["retention period"]
    assert llm.calls == 1
