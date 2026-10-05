import httpx2 as httpx
from openai import RateLimitError

from app.infrastructure.ai.llm import openai as openai_llm
from app.infrastructure.ai.llm.openai import OpenAILLM


class Completions:
    def __init__(self, error):
        self.error, self.calls = error, 0

    def create(self, **_kwargs):
        self.calls += 1
        raise self.error


def llm_failing_with(body: dict) -> tuple[OpenAILLM, Completions]:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    error = RateLimitError("429", response=httpx.Response(429, request=request), body=body)
    llm = OpenAILLM("sk-test", "gpt-5-mini", timeout=1, max_output_tokens=100)
    completions = Completions(error)
    llm._client = type("Client", (), {"chat": type("Chat", (), {"completions": completions})()})()
    return llm, completions


def ask(llm: OpenAILLM):
    return llm.generate_json("s", "u", {}, context={"task": "name", "detected": "Code of Ethics"})


def test_an_account_out_of_credit_is_not_asked_again_for_a_while():
    llm, completions = llm_failing_with({"type": "insufficient_quota", "code": "credit_balance_exhausted"})
    assert ask(llm).content["name"] == "Code of Ethics"  # the local fallback answers
    assert ask(llm).content["name"] == "Code of Ethics"
    assert completions.calls == 1


def test_a_passing_rate_limit_is_retried_on_the_next_call(monkeypatch):
    monkeypatch.setattr(openai_llm.time, "sleep", lambda _seconds: None)
    llm, completions = llm_failing_with({"type": "requests", "code": "rate_limit_exceeded"})
    ask(llm)
    ask(llm)
    # Each call waits and tries again before quoting the documents, and is not paused afterwards.
    assert completions.calls == 2 * (1 + len(openai_llm.TRANSIENT_WAITS))


def test_a_rate_limit_that_passes_still_gets_a_model_answer(monkeypatch):
    waits = []
    monkeypatch.setattr(openai_llm.time, "sleep", waits.append)
    llm, completions = llm_failing_with({"type": "requests", "code": "rate_limit_exceeded"})
    completions.error = RateLimitError(
        "Rate limit reached. Please try again in 2.172s.",
        response=httpx.Response(429, request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions")),
        body={"type": "tokens", "code": "rate_limit_exceeded"},
    )
    answer = type("Response", (), {
        "model": "gpt-5-mini", "usage": None,
        "choices": [type("Choice", (), {"finish_reason": "stop",
                                        "message": type("Message", (), {"content": '{"name": "From the model"}'})()})()],
    })()
    calls = iter([completions.error, answer])

    def create(**_kwargs):
        completions.calls += 1
        outcome = next(calls)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    completions.create = create
    assert ask(llm).content["name"] == "From the model"
    assert waits == [2.672]  # the provider's "try again in 2.172s", plus a margin
